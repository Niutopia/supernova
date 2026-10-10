"""Host/Origin guard that keeps other websites out of the local workbench."""

import pytest

from webapp import create_app


@pytest.fixture
def guarded_client(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    return app.test_client()


def test_foreign_host_header_is_rejected(guarded_client):
    reply = guarded_client.get(
        "/api/accounts", headers={"Host": "rebind.evil.example:5001"}
    )
    assert reply.status_code == 403
    assert reply.get_json()["code"] == "origin_forbidden"


def test_cross_site_write_is_rejected(guarded_client):
    reply = guarded_client.post(
        "/api/accounts",
        json={"name": "x", "username": "u", "password": "p"},
        headers={"Origin": "https://evil.example"},
    )
    assert reply.status_code == 403
    assert not guarded_client.get("/api/accounts").get_json()["data"]


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:5001", "http://localhost:3000", None]
)
def test_local_and_non_browser_writes_are_allowed(guarded_client, origin):
    headers = {"Origin": origin} if origin else {}
    reply = guarded_client.post(
        "/api/accounts",
        json={"name": "x", "username": "u", "password": "p"},
        headers=headers,
    )
    assert reply.status_code == 201


def test_configured_host_is_allowed(tmp_path):
    app = create_app(
        {"TESTING": True, "DATA_DIR": tmp_path, "ALLOWED_HOSTS": "study.example"}
    )
    reply = app.test_client().get(
        "/api/health", headers={"Host": "study.example"}
    )
    assert reply.status_code == 200
    assert reply.headers["X-Frame-Options"] == "DENY"
