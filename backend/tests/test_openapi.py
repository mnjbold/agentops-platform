"""OpenAPI customisation tests (issue #21).

Coverage:
* /v1/openapi.json returns the public spec
* Admin /api/admin/* paths are stripped from the public spec
* /v1/docs returns a 200 HTML page (Swagger UI)
* /v1/docs/postman.json returns a v2.1 collection
* Bearer auth scheme is present in the public spec
* The raw spec at /api/openapi.json still includes the admin paths
  (so internal tooling isn't broken)
"""
from __future__ import annotations


def test_v1_openapi_returns_spec(client):
    r = client.get("/v1/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "agentops API"
    assert "paths" in spec
    # Bearer scheme added.
    assert "bearerAuth" in spec["components"]["securitySchemes"]


def test_v1_openapi_hides_admin_paths(client):
    """The public spec must NOT include /api/admin/* routes."""
    r = client.get("/v1/openapi.json")
    spec = r.json()
    paths = list((spec.get("paths") or {}).keys())
    for p in paths:
        assert not p.startswith("/api/admin"), f"admin path leaked: {p}"
    # But the public routes are present.
    assert any(p.startswith("/v1/") for p in paths), "v1 routes should be in the spec"


def test_v1_docs_returns_html(client):
    r = client.get("/v1/docs")
    assert r.status_code == 200
    body = r.text
    assert "swagger" in body.lower() or "swagger-ui" in body.lower()
    # Brand banner injected.
    assert "agentops" in body


def test_v1_docs_postman_returns_collection(client):
    r = client.get("/v1/docs/postman.json")
    assert r.status_code == 200
    coll = r.json()
    assert coll["info"]["schema"].endswith("collection.json")
    # Item list is non-empty and has the v1 routes.
    assert len(coll["item"]) > 0
    names = [it["name"] for it in coll["item"]]
    assert any("/v1/" in n for n in names)


def test_internal_openapi_unaffected(client):
    """The raw /openapi.json (the FastAPI default) still includes the
    admin paths so internal scripts that import it aren't broken by the
    public-only rebrand."""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    paths = list((spec.get("paths") or {}).keys())
    assert any(p.startswith("/api/admin") for p in paths), \
        "internal spec should still include admin paths"
