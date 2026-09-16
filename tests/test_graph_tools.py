"""Knowledge graph MCP tools (phase 3), run against a copy of tests/fixtures/sample_app."""
from pathlib import Path

import pytest

from fastapi_architect import server
from fastapi_architect.graph.cache import clear_memory


@pytest.fixture(autouse=True)
def _fresh_memory():
    clear_memory()
    yield
    clear_memory()


def test_build_knowledge_graph(sample_app: Path):
    first = server.build_knowledge_graph(str(sample_app))
    second = server.build_knowledge_graph(str(sample_app))

    assert first["reparsed_files"] == 9
    assert first["node_types"]["Route"] == 6
    assert first["parse_errors"] == []
    assert second["reparsed_files"] == 0
    assert Path(first["cache"]).exists()


# ─── graph_neighbors ──────────────────────────────────────────────────────────

def test_graph_neighbors_of_route(sample_app: Path):
    result = server.graph_neighbors(str(sample_app), "DELETE /api/users/{user_id}", direction="out")

    found = {(n["name"], n["edge"]) for n in result["neighbors"]}
    assert found == {("delete_user", "HANDLED_BY"), ("require_admin", "DEPENDS_ON")}


def test_graph_neighbors_edge_filter_and_depth(sample_app: Path):
    result = server.graph_neighbors(str(sample_app), "require_admin", depth=2, edge_types=["depends_on"], direction="out")

    assert [(n["name"], n["distance"]) for n in result["neighbors"]] == [("get_current_user", 1), ("get_db", 2)]


def test_graph_neighbors_errors(sample_app: Path):
    root = str(sample_app)

    assert "candidates" in server.graph_neighbors(root, "create_user")  # handler and crud function
    assert "not found" in server.graph_neighbors(root, "nope")["error"]
    assert "valid_edge_types" in server.graph_neighbors(root, "get_db", edge_types=["WHATEVER"])


# ─── impact_analysis ──────────────────────────────────────────────────────────

def test_impact_of_orm_model(sample_app: Path):
    result = server.impact_analysis(str(sample_app), "User")

    routes = {r["name"]: r for r in result["routes"]}
    assert set(routes) == {"GET /api/users/", "POST /api/users/", "DELETE /api/users/{user_id}", "POST /api/items/"}
    assert routes["GET /api/users/"]["via"] == ["GET /api/users/", "list_users", "get_users", "User"]
    assert {s["name"] for s in result["schemas"]} == {"UserBase", "UserCreate", "UserPublic"}  # MIRRORS


def test_impact_of_table_through_raw_sql(sample_app: Path):
    result = server.impact_analysis(str(sample_app), "items")

    assert {r["name"] for r in result["routes"]} >= {"GET /api/items/", "POST /api/items/"}
    assert {m["name"] for m in result["orm_models"]} == {"Item"}


def test_impact_of_router_dependency_reaches_all_its_routes(tmp_path: Path):
    (tmp_path / "main.py").write_text(
        "from fastapi import APIRouter, Depends\n"
        "def verify_token(): ...\n"
        "router = APIRouter(dependencies=[Depends(verify_token)])\n"
        "@router.get('/a')\ndef a(): ...\n"
        "@router.post('/b')\ndef b(): ...\n"
    )

    result = server.impact_analysis(str(tmp_path), "verify_token")

    assert {r["name"] for r in result["routes"]} == {"GET /a", "POST /b"}


# ─── find_path ────────────────────────────────────────────────────────────────

def test_find_path_route_to_table(sample_app: Path):
    result = server.find_path(str(sample_app), "DELETE /api/users/{user_id}", "users")

    assert result["directed"] is True
    shortest = result["paths"][0]
    assert [s["name"] for s in shortest] == ["DELETE /api/users/{user_id}", "delete_user", "delete_user", "users"]
    assert [s.get("edge") for s in shortest] == ["HANDLED_BY", "CALLS", "QUERIES", None]


def test_find_path_falls_back_to_undirected(sample_app: Path):
    result = server.find_path(str(sample_app), "users", "GET /api/users/")

    assert result["directed"] is False
    assert result["paths"]


# ─── audit_graph ──────────────────────────────────────────────────────────────

def test_audit_graph(sample_app: Path):
    result = server.audit_graph(str(sample_app))

    assert [r["name"] for r in result["unprotected_write_routes"]] == ["POST /api/users/"]
    assert [s["name"] for s in result["unused_schemas"]] == ["Unused"]
    assert result["unreferenced_orm_models"] == []  # Item is only used through raw SQL on its table
    assert result["summary"]["route_auth"] == {"none": 4, "dependency": 2}


def test_audit_custom_auth_dependency(sample_app: Path):
    result = server.audit_graph(str(sample_app), auth_dependencies=["get_db"])

    assert result["unprotected_write_routes"] == []


def test_audit_detects_header_and_manual_auth(tmp_path: Path):
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI, Header, Request\n"
        "app = FastAPI()\n"
        "def _is_admin_authenticated(request): ...\n"
        "@app.post('/scrape')\ndef scrape(x_api_key: str = Header(None)): ...\n"
        "@app.delete('/admin/data')\ndef wipe(request: Request):\n    _is_admin_authenticated(request)\n"
        "@app.post('/open')\ndef open_(): ...\n"
        "@app.post('/open')\ndef open_again(): ...\n"
    )

    result = server.audit_graph(str(tmp_path))

    assert result["summary"]["route_auth"] == {"header": 1, "manual_call": 1, "none": 2}
    assert [r["name"] for r in result["unprotected_write_routes"]] == ["POST /open", "POST /open"]
    assert result["duplicate_routes"][0]["path"] == "/open"


def test_audit_unmounted_router_and_dependency_cycle(tmp_path: Path):
    (tmp_path / "main.py").write_text(
        "from fastapi import APIRouter, Depends\n"
        "def a(x=Depends(lambda: None)): ...\n"
        "def dep_a(b=Depends('dep_b')): ...\n"
        "def dep_b(a=Depends(dep_a)): ...\n"
        "def dep_a2(b=Depends(dep_b)): ...\n"
        "router = APIRouter(prefix='/orphan')\n"
        "@router.get('/x')\ndef x(): ...\n"
    )
    (tmp_path / "cycle.py").write_text(
        "from fastapi import Depends\n"
        "def first(s=Depends(lambda: second)): ...\n"
        "def one(t=Depends(two)): ...\n"
        "def two(o=Depends(one)): ...\n"
    )

    result = server.audit_graph(str(tmp_path))

    assert [r["name"] for r in result["unmounted_routes"]] == ["GET /orphan/x"]
    assert sorted(result["dependency_cycles"][0]) == ["one", "two"]


# ─── graph_report ─────────────────────────────────────────────────────────────

def test_graph_report(sample_app: Path):
    report = server.graph_report(str(sample_app))

    assert report.startswith("# FastAPI knowledge graph — sample_app")
    assert "| DELETE | `/api/users/{user_id}` | `app.routes.users:delete_user` | dependency: `require_admin` |" in report
    assert "- `users` ← model User, raw SQL in 1 place(s)" in report
    assert "`Unused`" in report
