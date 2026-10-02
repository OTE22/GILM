"""Explicit real-provider lifecycle verification against a local GILM server."""

import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--model", default="qwen2.5:1.5b")
    parser.add_argument("--output", type=Path, default=Path("artifacts/real-lifecycle.json"))
    args = parser.parse_args()
    evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
    assert evaluation["passed"] and not evaluation["mock_only"]
    plan_id = evaluation["plan_id"]
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=180) as client:
        plan_response = client.get(f"/api/plans/{plan_id}")
        plan_response.raise_for_status()
        plan = plan_response.json()
        assert plan["config"]["provider"] == "http"
        assert plan["config"]["model"] == args.model
        baseline_id = plan["parent"]
        activation = client.post(f"/api/plans/{plan_id}/activate")
        activation.raise_for_status()
        assert activation.json()["active"]
        report = {"start": "2026-09-01", "end": "2026-10-01", "cache_approved": True}
        cold = client.post("/api/reports/sales", json=report)
        cold.raise_for_status()
        result = cold.json()
        assert result["plan_version"] == plan_id and result["cache_status"] != "hit"
        assert result["answer"]["sales_by_branch_cents"] == {"North": 25100, "South": 30000}
        warm = client.post("/api/reports/sales", json=report)
        warm.raise_for_status()
        assert warm.json()["cache_status"] == "hit" and warm.json()["answer"] == result["answer"]
        rollback = client.post(f"/api/plans/{baseline_id}/rollback")
        rollback.raise_for_status()
        assert rollback.json()["active"]
        assert not client.get(f"/api/plans/{plan_id}").json()["active"]
        reactivation = client.post(f"/api/plans/{plan_id}/activate")
        reactivation.raise_for_status()
        assert reactivation.json()["active"]
        chunks = []
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": args.model,
                "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
                "max_tokens": 128,
                "stream": True,
                **(
                    {"reasoning": plan["config"]["generation"]["reasoning"]}
                    if plan["config"]["generation"].get("reasoning")
                    else {}
                ),
            },
        ) as response:
            response.raise_for_status()
            assert response.headers["content-type"].startswith("text/event-stream")
            chunks.extend(chunk for chunk in response.iter_bytes() if chunk)
        assert b"data: [DONE]" in b"".join(chunks)
        traces = client.get("/api/traces").json()
        cold_trace = next(trace for trace in traces if trace["request_id"] == result["request_id"])
        warm_trace = next(trace for trace in traces if trace["request_id"] == warm.json()["request_id"])
        assert len(cold_trace["attempts"]) == 1 and len(warm_trace["attempts"]) == 0
        summary = {
            "model": args.model,
            "provider": result["completion"].get("provider"),
            "provider_reported_cost_usd": result["completion"].get("usage", {}).get("cost"),
            "synthetic_dataset": True,
            "mock_model": False,
            "active_plan": plan_id,
            "baseline_plan": baseline_id,
            "activation": "passed",
            "rollback": "passed",
            "reactivation": "passed",
            "cold_provider_attempts": len(cold_trace["attempts"]),
            "warm_provider_attempts": len(warm_trace["attempts"]),
            "warm_cache_status": warm.json()["cache_status"],
            "answer": result["answer"],
            "real_sse": "passed",
            "sse_received_chunks": len(chunks),
        }
        output = args.output
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
