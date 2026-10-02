import pytest
from fastapi.testclient import TestClient

from gilm.app import create_app
from gilm.config import Principal, Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "gilm",
        keys={
            "admin-key": Principal("demo", ("North", "South"), True),
            "north-key": Principal("demo", ("North",), False),
            "other-key": Principal("other", ("Central",), True),
        },
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@pytest.fixture
def report():
    return {"start": "2026-09-01", "end": "2026-10-01", "cache_approved": True}


def make_candidate(client, **changes):
    baseline = next(p for p in client.get("/api/plans").json() if p["baseline"])
    config = {
        **baseline["config"],
        "adapter_mode": "aggregate",
        "remove_redundant": True,
        "cache": {"enabled": True, "ttl_seconds": 60},
        **changes,
    }
    source = client.get("/api/source").json()["version"]
    response = client.post(
        "/api/plans",
        json={
            "parent_version": baseline["id"],
            "description": "Test complete aggregate plan",
            "config": config,
            "source_versions": {"sales": source},
        },
    )
    assert response.status_code == 201, response.text
    return baseline, response.json()
