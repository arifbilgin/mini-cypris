import pytest

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    return app.test_client()


def test_health_creates_schema(client):
    body = client.get("/api/health").get_json()
    assert body["status"] == "ok"
    assert body["tables"] == ["aggregates", "papers", "search_results", "searches"]


def test_hello_default(client):
    assert client.get("/api/hello").get_json()["message"] == "Hello, world! This is Flask."


def test_hello_name(client):
    body = client.get("/api/hello?name=Chris").get_json()
    assert body["name"] == "Chris"
    assert body["message"] == "Hello, Chris! This is Flask."


def test_unknown_api_route_is_json_404(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert res.get_json() == {"error": "not_found", "path": "/api/nope"}


@pytest.fixture
def prod_client(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<app-root></app-root>")
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "FRONTEND_DIST": str(dist)})
    return app.test_client()


def test_prod_serves_app_and_deep_links(prod_client):
    assert b"<app-root>" in prod_client.get("/").data
    assert b"<app-root>" in prod_client.get("/some/route").data


def test_prod_unknown_api_route_is_still_json_404(prod_client):
    res = prod_client.get("/api/nope")
    assert res.status_code == 404
    assert res.get_json() == {"error": "not_found", "path": "/api/nope"}
