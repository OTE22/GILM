"""Exercise the actual CLI server over loopback using a new temporary data directory."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gilm-smoke-") as temporary:
        environment = os.environ.copy()
        environment.update(
            GILM_DATA_DIR=temporary,
            GILM_DEV_MODE="true",
            GILM_HOST="127.0.0.1",
            GILM_DEFAULT_PROVIDER="mock",
            GILM_HTTP_URL="",
            GILM_API_KEYS="{}",
            GILM_HTTP_PRICING="{}",
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        clean_demo = subprocess.run(
            [sys.executable, "-m", "gilm", "demo", "--output", str(artifacts / "clean-demo.json")],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=creation_flags,
        )
        if clean_demo.returncode:
            raise RuntimeError("Clean-data demo failed: " + clean_demo.stderr)
        with (artifacts / "smoke-server.log").open("w", encoding="utf-8") as server_log:
            server = subprocess.Popen(
                [sys.executable, "-m", "gilm", "serve", "--port", str(port)],
                cwd=root,
                env=environment,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
                    for _ in range(100):
                        if server.poll() is not None:
                            raise RuntimeError("Server exited; inspect artifacts/smoke-server.log")
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("Server did not become healthy")
                    report = client.post("/api/reports/sales", json={"start": "2026-09-01", "end": "2026-10-01"})
                    report.raise_for_status()
                    assert report.json()["answer"]["sales_by_branch_cents"] == {"North": 25100, "South": 30000}
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "mock-report-v1",
                            "messages": [{"role": "user", "content": "hello"}],
                            "stream": True,
                        },
                    )
                    response.raise_for_status()
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert response.content.endswith(b"data: [DONE]\n\n")
                    dashboard = client.get("/")
                    assert dashboard.status_code == 200 and "Synthetic reporting dataset" in dashboard.text
                    assert "script-src 'self'" in dashboard.headers["content-security-policy"]
                    assert client.get("/static/dashboard.js").status_code == 200
                    traces = client.get("/api/traces").json()
                    assert any(trace.get("stream") and trace["outcome"] == "success" for trace in traces)
                    print(
                        json.dumps(
                            {
                                "clean_data_demo": "passed",
                                "live_http_report": "passed",
                                "live_sse": "passed",
                                "dashboard_assets": "passed",
                                "sales_by_branch_cents": report.json()["answer"]["sales_by_branch_cents"],
                            },
                            indent=2,
                        )
                    )
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    main()
