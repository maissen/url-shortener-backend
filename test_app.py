"""
API tests for app.py

Run:
    pytest -v
"""

import importlib
import pytest


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APP_NAME", "test-app")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("PORT", "8080")

    import app

    importlib.reload(app)
    app.app.config["TESTING"] = True

    with app.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


def test_health_returns_200(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# /health — method enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_health_disallows_non_get_methods(client, method):
    assert getattr(client, method)("/health").status_code == 405


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


def test_unknown_route_returns_404(client):
    assert client.get("/does-not-exist").status_code == 404


def test_root_returns_404(client):
    assert client.get("/").status_code == 404
