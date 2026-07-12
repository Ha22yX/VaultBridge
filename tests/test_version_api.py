from __future__ import annotations

from app.main import app


def test_version_detail_and_tree_routes_are_registered() -> None:
    routes = {
        (route.path, ",".join(sorted(route.methods or [])))
        for route in app.routes
        if hasattr(route, "methods")
    }

    assert ("/api/jobs/{job_id}/versions/{commit}", "GET") in routes
    assert ("/api/jobs/{job_id}/versions/{commit}/tree", "GET") in routes


def test_versions_route_allows_numeric_metadata() -> None:
    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/jobs/{job_id}/versions")

    assert route.response_model == dict
