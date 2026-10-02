from __future__ import annotations

import json
import uuid
from pathlib import Path

from .budget import BudgetedProvider
from .context import transform
from .engine import Engine, QualityError, applicability
from .models import (
    Block,
    Candidate,
    ChatRequest,
    Extension,
    LiveEvaluation,
    Message,
    PlanConfig,
    ReportRequest,
    content_hash,
)
from .providers import ProviderError
from .store import PlanConflict


def summarize(rows):
    successes = sum(row["success"] for row in rows)
    costs = [row["provider_rate_card_estimate_usd"] for row in rows]
    total = sum(costs) if all(c is not None for c in costs) else None
    inputs = [row.get("input_tokens") for row in rows]
    input_total = sum(inputs) if all(value is not None for value in inputs) else None
    sources = sorted({row["usage_source"] for row in rows})
    return {
        "tasks": len(rows),
        "successes": successes,
        "success_rate": successes / len(rows) if rows else None,
        "critical_failures": sum(row.get("critical_failure", False) for row in rows),
        "provider_rate_card_estimate_usd": total,
        "invoice_reconciled_usd": None,
        "rate_card_estimate_per_success_usd": total / successes if total is not None and successes else None,
        "mean_latency_ms": sum(row["latency_ms"] for row in rows) / len(rows) if rows else None,
        "input_tokens_total": input_total,
        "input_token_estimates": input_total if sources == ["mock_character_estimate"] else None,
        "provider_reported_input_tokens": input_total if sources == ["provider_reported"] else None,
        "token_source": sources[0] if len(sources) == 1 else "mixed",
        "local_compute_cost_usd": None,
    }


def context_fixture_checks():
    messages = [
        Message(role="system", content="Required policy"),
        Message(role="user", content="same data"),
        Message(role="user", content="same data"),
        Message(role="user", content="current request"),
    ]
    base = dict(
        content_hash=content_hash("same data"),
        source="fixture",
        source_version="v1",
        provenance="application annotation",
        role="user",
    )
    a = Block(id="a", message_index=1, **base)
    b = Block(id="b", message_index=2, required=False, eligible=True, retention="removable", redundant_with="a", **base)
    request = ChatRequest(model="mock-report-v1", messages=messages, gilm=Extension(blocks=[a, b]))
    edited, removed, reason = transform(request, True)
    checks = {"approved_duplicate_removed": reason is None and removed == ["b"] and len(edited.messages) == 3}
    for label, change in [
        ("required_context_preserved", {"required": True}),
        ("protected_context_preserved", {"protected": True}),
        ("bad_hash_fallback", {"content_hash": "0" * 64}),
        ("missing_dependency_fallback", {"dependencies": ["missing"]}),
    ]:
        invalid = request.model_copy(deep=True)
        invalid.gilm.blocks[1] = b.model_copy(update=change)
        original, dropped, failure = transform(invalid, True)
        checks[label] = failure is not None and not dropped and original == invalid
    dependent = request.model_copy(deep=True)
    dependent.gilm.blocks[0].dependencies = ["b"]
    checks["dependency_preserved"] = transform(dependent, True)[2] == "dependency_would_be_removed"
    return checks


class Evaluator:
    def __init__(self, engine):
        self.engine = engine

    async def evaluate(self, principal, ident, live: LiveEvaluation | None = None):
        engine = self.engine
        store, adapter = engine.store, engine.adapter
        plan = store.plan(principal, ident)
        guard = None
        if live:
            if (
                plan["workflow"] != "sales_report"
                or plan["config"]["provider"] != "http"
                or store.default_provider != "http"
            ):
                raise PlanConflict("Live evaluation requires an HTTP reporting baseline and candidate")
            guard = BudgetedProvider(engine.providers["http"], engine.settings.http_pricing, live)
            baseline = store.baseline(principal, "sales_report")
            for selected in (baseline, plan):
                guard.validate_model(selected["config"]["model"])
                maximum = selected["config"]["generation"].get("max_tokens")
                if not maximum or maximum > live.max_output_tokens:
                    raise PlanConflict("Both complete plans need max_tokens within the authorized output limit")
            engine = Engine(engine.settings, store, adapter, {**engine.providers, "http": guard})
        elif plan["config"]["provider"] != "mock" or store.default_provider != "mock":
            raise PlanConflict("Use explicit live evaluation authorization for HTTP providers")
        checks = context_fixture_checks()
        if plan["workflow"] == "chat":
            for split in ("development", "held_out"):
                request = ChatRequest(
                    model="mock-report-v1",
                    messages=[Message(role="user", content=split + " fixture")],
                    gilm=Extension(plan_version=ident),
                )
                result = await engine.chat(principal, request, "eval_" + uuid.uuid4().hex)
                checks[split + "_response"] = (
                    result["choices"][0]["message"]["content"] == "Deterministic GILM mock response."
                )
            result = {
                "passed": all(checks.values()),
                "checks": checks,
                "mock_only": True,
                "limitations": "Invariant and fixture mechanics only; no general semantic equivalence claim",
                "evaluation_rate_card_estimate_usd": None,
                "rows": [],
            }
            return store.save_evaluation(principal, plan, "none", result)
        if principal.tenant != "demo":
            raise PlanConflict("Bundled offline reporting fixtures are for the synthetic demo tenant")
        source = adapter.snapshot(principal)
        if guard and (reason := applicability(plan, "sales_report", {"sales": source})):
            raise PlanConflict("Live evaluation preflight rejected the plan: " + reason)
        baseline = store.baseline(principal, "sales_report")
        manual = store.create(
            principal,
            Candidate(
                parent_version=baseline["id"],
                description="Manual control: approved database aggregate with exact cache",
                config=PlanConfig.model_validate(
                    {**baseline["config"], "adapter_mode": "aggregate", "cache": {"enabled": True}}
                ),
                source_versions={"sales": source},
            ),
        )
        fixtures = json.loads((Path(__file__).parent / "fixtures" / "evaluation.json").read_text())
        rows = []
        aborted = None
        for split, cases in fixtures.items():
            if aborted:
                break
            for case in cases:
                if aborted:
                    break
                allowed = sorted(set(case["branches"]) & set(principal.branches))
                if not allowed:
                    continue
                expected = {key: value for key, value in case["expected"].items() if key in allowed}
                for label, selected in [("baseline", baseline), ("manual", manual), ("candidate", plan)]:
                    if aborted:
                        break
                    store.invalidate(principal)
                    for condition in ("cold", "warm"):
                        rid = "eval_" + uuid.uuid4().hex
                        report = ReportRequest(
                            start=case["start"], end=case["end"], branches=allowed, cache_approved=True
                        )
                        success = False
                        try:
                            result = await engine.report(principal, report, rid, override=selected)
                            success = result["answer"]["sales_by_branch_cents"] == expected
                            # Candidate must actually run, not pass by falling back to baseline.
                            if label == "candidate":
                                success = success and result["fallback_reason"] is None
                        except QualityError:
                            # A completed text response with known usage is a scored
                            # failure, not an ambiguous provider failure or a fallback retry.
                            pass
                        except ProviderError as exc:
                            if guard:
                                aborted = guard.aborted_reason = guard.aborted_reason or exc.code
                        trace = store.trace_by_id(principal, rid)
                        token_values = [(a["usage"] or {}).get("prompt_tokens") for a in trace["attempts"]]
                        input_tokens = sum(token_values) if all(value is not None for value in token_values) else None
                        usage_sources = {a["usage_source"] for a in trace["attempts"]}
                        usage_source = next(iter(usage_sources)) if len(usage_sources) == 1 else "unknown"
                        if not trace["attempts"]:
                            usage_source = "cache_no_provider_call"
                        row = {
                            "split": split,
                            "fixture": case["id"],
                            "variant": label,
                            "condition": condition,
                            "plan_version": selected["id"],
                            "success": success,
                            "critical_failure": trace["outcome"] != "success" or not success,
                            "cache_status": trace["cache_status"],
                            "quality_checks": trace["quality_checks"],
                            "outcome": trace["outcome"],
                            "fallback_reason": trace["fallback_reason"],
                            "latency_ms": trace["latency_ms"],
                            "adapter_ms": trace["adapter_ms"],
                            "optimization_ms": trace["optimization_ms"],
                            "provider_rate_card_estimate_usd": trace["provider_rate_card_estimate_usd"],
                            "input_tokens": input_tokens,
                            "usage_source": usage_source,
                            "invoice_reconciled_usd": None,
                        }
                        rows.append(row)
                        if guard:
                            aborted = guard.aborted_reason
                            if aborted:
                                guard.aborted_reason = aborted
                                break
        checks["development_and_held_out_present"] = {r["split"] for r in rows} == {"development", "held_out"}
        candidate_rows = [row for row in rows if row["variant"] == "candidate"]
        checks["candidate_workflows_correct"] = bool(candidate_rows) and all(row["success"] for row in candidate_rows)
        checks["candidate_source_current"] = not plan["sources"] or plan["sources"] == {"sales": source}
        checks["source_stable_during_evaluation"] = adapter.snapshot(principal) == source
        if guard:
            checks["spending_guard_completed"] = aborted is None
        metrics = {
            label: {
                condition: summarize([r for r in rows if r["variant"] == label and r["condition"] == condition])
                for condition in ("cold", "warm")
            }
            for label in ("baseline", "manual", "candidate")
        }
        costs = [r["provider_rate_card_estimate_usd"] for r in rows]
        result = {
            "passed": all(checks.values()),
            "checks": checks,
            "mock_only": guard is None,
            "metrics": metrics,
            "rows": rows,
            "evaluation_rate_card_estimate_usd": sum(costs) if all(c is not None for c in costs) else None,
            "limitations": "Synthetic fixtures with a configured HTTP model; rate-card estimates are not invoice charges"
            if guard
            else "Synthetic fixture interpreter and hypothetical rates; no actual billing or competitor measurements",
        }
        if guard:
            result["spending_guard"] = guard.summary()
        store.invalidate(principal)
        return store.save_evaluation(principal, plan, source, result)
