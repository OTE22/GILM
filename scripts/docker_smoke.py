"""Test only a disposable, uniquely named GILM container and volume; never prune user resources."""

import json
import os
import re
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx


def docker(*arguments, environment=None):
    result = subprocess.run(["docker", *arguments], env=environment, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"Docker {arguments[0]} failed: {result.stderr}")
    return (result.stdout + (result.stderr if arguments[0] == "logs" else "")).strip()


def wait_ready(client):
    for _ in range(100):
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Container did not become healthy")


def main():
    suffix = uuid.uuid4().hex
    name, volume = "gilm-verification-" + suffix, "gilm-verification-data-" + suffix
    key = secrets.token_hex(32)
    environment = os.environ.copy()
    environment["GILM_API_KEYS"] = json.dumps(
        {key: {"tenant": "demo", "branches": ["North", "South"], "can_manage": True}}
    )
    container = None
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        host_port = reservation.getsockname()[1]
    try:
        docker("volume", "create", "--label", "gilm.verification=true", volume)
        container = docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "gilm.verification=true",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--mount",
            f"type=volume,source={volume},target=/app/data",
            "--publish",
            f"127.0.0.1:{host_port}:8000",
            "--env",
            "GILM_API_KEYS",
            "gilm:local",
            environment=environment,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", container):
            raise RuntimeError("Docker returned an unexpected container ID")
        port = int(docker("port", container, "8000/tcp").split(":")[-1])
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
            wait_ready(client)
            assert client.get("/api/plans").status_code == 401
            client.headers["Authorization"] = "Bearer " + key
            baseline = client.get("/api/plans").json()[0]
            source = client.get("/api/source").json()["version"]
            candidate = client.post(
                "/api/plans",
                json={
                    "parent_version": baseline["id"],
                    "description": "Disposable container verification",
                    "config": {
                        **baseline["config"],
                        "adapter_mode": "aggregate",
                        "remove_redundant": True,
                        "cache": {"enabled": True},
                    },
                    "source_versions": {"sales": source},
                },
            )
            candidate.raise_for_status()
            plan_id = candidate.json()["id"]
            evaluation = client.post(f"/api/plans/{plan_id}/evaluate")
            evaluation.raise_for_status()
            assert evaluation.json()["passed"] and len(evaluation.json()["rows"]) == 24
            assert client.post(f"/api/plans/{plan_id}/activate").json()["active"]
            report = {"start": "2026-09-01", "end": "2026-10-01", "cache_approved": True}
            result = client.post("/api/reports/sales", json=report).json()
            assert result["answer"]["sales_by_branch_cents"] == {"North": 25100, "South": 30000}
            assert client.post("/api/reports/sales", json=report).json()["cache_status"] == "hit"
            response = client.post(
                "/v1/chat/completions",
                json={"model": "mock-report-v1", "messages": [{"role": "user", "content": "stream"}], "stream": True},
            )
            assert response.content.endswith(b"data: [DONE]\n\n")
            assert client.get("/").status_code == 200
            docker("restart", container)
            wait_ready(client)
            assert client.get(f"/api/plans/{plan_id}").json()["active"]
            assert client.post("/api/reports/sales", json=report).json()["cache_status"] == "hit"
            assert client.post(f"/api/plans/{baseline['id']}/rollback").json()["active"]
            assert client.post("/api/reports/sales", json=report).json()["plan_version"] == baseline["id"]
        assert docker("exec", container, "id", "-u") == "10001"
        assert docker("inspect", "--format", "{{.HostConfig.ReadonlyRootfs}}", container) == "true"
        summary = {
            "image": "gilm:local",
            "authenticated_api": "passed",
            "plan_lifecycle": "passed",
            "fixture_executions": 24,
            "sse": "passed",
            "persistent_restart": "passed",
            "nonroot_uid": 10001,
            "read_only_root": True,
            "synthetic_model": True,
            "sales_by_branch_cents": result["answer"]["sales_by_branch_cents"],
        }
        artifact = Path("artifacts/docker-smoke.json")
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    finally:
        if container and re.fullmatch(r"[0-9a-f]{64}", container):
            log_path = Path("artifacts/docker-smoke.log")
            log_path.parent.mkdir(exist_ok=True)
            log_path.write_text(docker("logs", container), encoding="utf-8")
            docker("rm", "--force", container)
        docker("volume", "rm", volume)


if __name__ == "__main__":
    main()
