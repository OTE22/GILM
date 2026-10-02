from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

from .config import Principal, Settings
from .models import Candidate, LiveEvaluation, PlanConfig, ReportRequest
from .report_contract import REPORT_RESPONSE_FORMAT


async def demo(settings, output: Path, benchmark_only=False):
    if settings.default_provider != "mock":
        raise SystemExit("Demo and benchmark are mock-only; no paid calls are made")
    from .app import create_app

    app = create_app(settings)
    principal = Principal("demo", ("North", "South"), True)
    async with app.router.lifespan_context(app):
        engine = app.state.engine
        store = engine.store
        baseline = store.baseline(principal, "sales_report")
        source = engine.adapter.snapshot(principal)
        candidate = store.create(
            principal,
            Candidate(
                parent_version=baseline["id"],
                description="Approved aggregation plus removal of explicit duplicate definitions and exact response cache",
                config=PlanConfig.model_validate(
                    {
                        **baseline["config"],
                        "adapter_mode": "aggregate",
                        "remove_redundant": True,
                        "cache": {"enabled": True, "ttl_seconds": 60},
                    }
                ),
                source_versions={"sales": source},
            ),
        )
        report = ReportRequest(start="2026-09-01", end="2026-10-01", branches=["North", "South"], cache_approved=True)
        original = await engine.report(
            principal, report.model_copy(update={"plan_version": baseline["id"]}), "demo_" + uuid.uuid4().hex
        )
        evaluation = await app.state.evaluator.evaluate(principal, candidate["id"])
        if not evaluation["passed"]:
            raise SystemExit("Candidate failed offline evaluation; no activation")
        result = {"mock_only": True, "evaluation": evaluation, "baseline_answer": original["answer"]}
        if not benchmark_only:
            store.activate(principal, candidate["id"], source)
            optimized = await engine.report(principal, report, "demo_" + uuid.uuid4().hex)
            warm = await engine.report(principal, report, "demo_" + uuid.uuid4().hex)
            store.activate(principal, baseline["id"], source, rollback=True)
            rolled_back = await engine.report(principal, report, "demo_" + uuid.uuid4().hex)
            result.update(
                candidate_answer=optimized["answer"],
                warm_cache_status=warm["cache_status"],
                rollback_plan_version=rolled_back["plan_version"],
                baseline_plan_version=baseline["id"],
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        export = output.with_suffix(".jsonl")
        export.write_text("".join(json.dumps(row) + "\n" for row in evaluation["rows"]), encoding="utf-8")
        print(
            json.dumps(
                {
                    "artifact": str(output),
                    "export": str(export),
                    "mock_only": True,
                    "answer": original["answer"],
                    "passed": evaluation["passed"],
                    "metrics": evaluation["metrics"],
                    "warm_cache_status": result.get("warm_cache_status"),
                },
                indent=2,
            )
        )


async def benchmark_live(settings, args):
    if settings.default_provider != "http":
        raise SystemExit("Configure an HTTP provider, rate card and fresh HTTP baseline before live evaluation")
    key = os.getenv("GILM_BENCHMARK_KEY", "")
    principal = (
        settings.keys.get(key) if key else Principal("demo", ("North", "South"), True) if settings.dev_mode else None
    )
    if principal is None or not principal.can_manage:
        raise SystemExit("Authenticated live evaluation requires GILM_BENCHMARK_KEY with management permission")
    from .app import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = app.state.engine
        if args.plan_version:
            plan = engine.store.plan(principal, args.plan_version)
        else:
            baseline = engine.store.baseline(principal, "sales_report")
            generation = baseline["config"]["generation"]
            if args.structured_report:
                response_format = (
                    REPORT_RESPONSE_FORMAT if args.report_format == "json_schema" else {"type": "json_object"}
                )
                generation = {**generation, "temperature": 0, "response_format": response_format}
            if args.disable_reasoning:
                generation = {**generation, "reasoning": {"enabled": False}}
            plan = engine.store.create(
                principal,
                Candidate(
                    parent_version=baseline["id"],
                    description="Live candidate: approved aggregation, explicit duplicate removal, exact cache"
                    + (f", {args.report_format} and temperature zero" if args.structured_report else "")
                    + (", reasoning disabled" if args.disable_reasoning else ""),
                    config=PlanConfig.model_validate(
                        {
                            **baseline["config"],
                            "adapter_mode": "aggregate",
                            "remove_redundant": True,
                            "cache": {"enabled": True},
                            "generation": generation,
                        }
                    ),
                    source_versions={"sales": engine.adapter.snapshot(principal)},
                ),
            )
        authorization = LiveEvaluation(
            allow_paid=True,
            max_estimated_usd=args.max_estimated_usd,
            max_provider_attempts=args.max_provider_attempts,
            max_output_tokens=args.max_output_tokens,
        )
        result = await app.state.evaluator.evaluate(principal, plan["id"], live=authorization)
        output = args.output or Path("artifacts/live-benchmark.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        output.with_suffix(".jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in result["rows"]), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "artifact": str(output),
                    "plan_version": plan["id"],
                    "passed": result["passed"],
                    "activated": False,
                    "mock_only": False,
                    "spending_guard": result["spending_guard"],
                    "metrics": result["metrics"],
                },
                indent=2,
            )
        )
        return result["passed"]


def main():
    parser = argparse.ArgumentParser(description="GILM local mock demo, benchmark, and server")
    parser.add_argument("command", choices=["serve", "demo", "benchmark", "benchmark-live", "prune"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--allow-free", action="store_true")
    parser.add_argument("--max-estimated-usd", type=Decimal)
    parser.add_argument("--max-provider-attempts", type=int, default=16)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--plan-version")
    parser.add_argument("--structured-report", action="store_true")
    parser.add_argument("--report-format", choices=["json_schema", "json_object"], default="json_schema")
    parser.add_argument("--disable-reasoning", action="store_true")
    args = parser.parse_args()
    if args.allow_free and args.allow_paid:
        parser.error("Choose --allow-free or --allow-paid")
    if (
        args.command == "benchmark-live"
        and not args.allow_free
        and (not args.allow_paid or args.max_estimated_usd is None)
    ):
        parser.error("benchmark-live requires --allow-free, or --allow-paid with --max-estimated-usd")
    if args.plan_version and (args.structured_report or args.disable_reasoning):
        parser.error("Generation flags create a new version and cannot modify --plan-version")
    settings = Settings.from_env()
    if args.allow_free:
        if not settings.openrouter_free_only or args.max_estimated_usd not in (None, Decimal(0)):
            parser.error("--allow-free requires OpenRouter free-only configuration and a zero budget")
        args.max_estimated_usd = Decimal(0)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("gilm.app:app", host=settings.host, port=args.port, access_log=False)
    elif args.command == "benchmark-live":
        if not asyncio.run(benchmark_live(settings, args)):
            raise SystemExit(2)
    elif args.command == "prune":
        from .store import Store

        Store(settings.data_dir / "state.sqlite").prune(settings.retention_days)
        print("Expired response cache entries and old metadata traces pruned")
    else:
        asyncio.run(
            demo(settings, args.output or Path("artifacts") / (args.command + ".json"), args.command == "benchmark")
        )


if __name__ == "__main__":
    main()
