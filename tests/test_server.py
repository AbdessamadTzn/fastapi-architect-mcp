"""Regression tests for the existing MCP tools, run against tests/fixtures/sample_app."""
from pathlib import Path

import pytest

from fastapi_architect import server


def _names(tree: dict) -> set[str]:
    names = {tree["name"]}
    for dep in tree["dependencies"]:
        names |= _names(dep)
    return names


# ─── File discovery ───────────────────────────────────────────────────────────

def test_iter_python_files_skips_virtualenvs(sample_app: Path):
    for d in (".venv/lib", "venv/lib", "node_modules/pkg", "app/__pycache__"):
        (sample_app / d).mkdir(parents=True, exist_ok=True)
        (sample_app / d / "leak.py").write_text("x = 1\n")

    files = {p.relative_to(sample_app).as_posix() for p in server._iter_python_files(sample_app)}

    assert "app/main.py" in files
    assert not any("leak.py" in f for f in files)


def test_list_routes_ignores_routes_in_virtualenv(sample_app: Path):
    leak = sample_app / ".venv" / "lib"
    leak.mkdir(parents=True)
    (leak / "fake.py").write_text('@app.get("/leak")\ndef leak(): ...\n')

    assert "leak" not in {r["handler"] for r in server.list_routes(str(sample_app))}


# ─── Jedi tools ───────────────────────────────────────────────────────────────

def test_go_to_definition_follows_imports(app_dir: Path):
    # users.py line 16: `def create_user(user: UserCreate, ...)` → UserCreate at col 22
    result = server.go_to_definition(str(app_dir / "routes" / "users.py"), 16, 22)

    assert result == {"file": str(app_dir / "schemas.py"), "line": 8, "column": 6, "name": "UserCreate"}


def test_find_references_cross_file(app_dir: Path):
    refs = server.find_references(str(app_dir / "deps.py"), 9, 5)  # def get_db

    files = {Path(r["file"]).name for r in refs}
    assert files == {"deps.py", "users.py"}
    assert all(r["name"] == "get_db" for r in refs)


def test_rename_symbol_updates_all_files(app_dir: Path):
    result = server.rename_symbol(str(app_dir / "deps.py"), 9, 5, "get_session")

    assert result["files_changed"] == 2
    for f in ("deps.py", "routes/users.py"):
        text = (app_dir / f).read_text()
        assert "get_session" in text
        assert "get_db" not in text


def test_get_completions_after_dot(app_dir: Path):
    names = {c["name"] for c in server.get_completions(str(app_dir / "crud.py"), 11, 7)}  # `db.`

    assert names  # untyped `db` still yields object attributes


# ─── FastAPI tools ────────────────────────────────────────────────────────────

def test_list_routes(sample_app: Path):
    routes = {(r["method"], r["handler"]): r for r in server.list_routes(str(sample_app))}

    assert set(routes) == {
        ("GET", "health"), ("GET", "list_users"), ("POST", "create_user"),
        ("DELETE", "delete_user"), ("GET", "list_items"), ("POST", "create_item"),
    }
    assert routes[("DELETE", "delete_user")]["is_async"] is True


def test_list_routes_full_path_with_prefixes(sample_app: Path):
    paths = {r["handler"]: r["path"] for r in server.list_routes(str(sample_app))}

    assert paths["list_users"] == "/api/users/"
    assert paths["delete_user"] == "/api/users/{user_id}"
    assert paths["health"] == "/health"


def test_get_dependencies_all_patterns(sample_app: Path):
    # decorator-level Depends + keyword-only Annotated alias + nested default Depends
    tree = server.get_dependencies(str(sample_app), "delete_user")
    assert _names(tree) == {"delete_user", "require_admin", "get_current_user", "get_db"}

    # Annotated aliases only
    tree = server.get_dependencies(str(sample_app), "create_item")
    assert [d["name"] for d in tree["dependencies"]] == ["get_current_user", "get_db"]


def test_get_dependencies_unknown_handler(sample_app: Path):
    assert "error" in server.get_dependencies(str(sample_app), "nope")


def test_get_dependencies_prefers_route_handler_over_homonym(sample_app: Path):
    # app.crud:create_user and app.routes.users:create_user share a name; only one is a handler
    tree = server.get_dependencies(str(sample_app), "create_user")

    assert tree["id"] == "app.routes.users:create_user"


def test_get_dependencies_ambiguous(sample_app: Path):
    (sample_app / "app" / "other.py").write_text("def get_db():\n    pass\n")

    result = server.get_dependencies(str(sample_app), "get_db")

    assert result["candidates"] == ["app.deps:get_db", "app.other:get_db"]


def test_router_level_dependencies(tmp_path: Path):
    (tmp_path / "main.py").write_text(
        "from fastapi import APIRouter, Depends, FastAPI\n"
        "def verify_token(): ...\n"
        "def audit(): ...\n"
        "def get_db(): ...\n"
        "app = FastAPI()\n"
        "router = APIRouter(prefix='/admin', dependencies=[Depends(verify_token)])\n"
        "@router.get('/stats')\n"
        "def stats(db=Depends(get_db)): ...\n"
        "app.include_router(router, prefix='/v1', dependencies=[Depends(audit)])\n"
    )

    routes = server.list_routes(str(tmp_path))
    tree = server.get_dependencies(str(tmp_path), "stats")

    assert routes[0]["path"] == "/v1/admin/stats"
    assert [d["name"] for d in tree["dependencies"]] == ["audit", "verify_token", "get_db"]


def test_build_dependency_graph(sample_app: Path, app_dir: Path):
    graph = {g["handler"]: g for g in server.build_dependency_graph(str(app_dir / "routes" / "users.py"), str(sample_app))}

    assert graph["list_users"]["dependencies"] == ["get_db"]
    assert graph["list_users"]["response_model"] == "list[UserPublic]"
    assert graph["create_user"]["input_models"] == ["UserCreate"]
    assert graph["delete_user"]["response_model"] is None


def test_build_dependency_graph_decorator_dependencies(sample_app: Path, app_dir: Path):
    graph = {g["handler"]: g for g in server.build_dependency_graph(str(app_dir / "routes" / "users.py"), str(sample_app))}

    assert "require_admin" in graph["delete_user"]["dependencies"]


def test_validate_response_models(app_dir: Path):
    issues = server.validate_response_models(str(app_dir / "routes" / "items.py"))

    assert [(i["method"], i["handler"]) for i in issues] == [("POST", "create_item")]


# ─── Pydantic tools ───────────────────────────────────────────────────────────

def test_list_models_includes_inherited(app_dir: Path):
    assert server.list_models(str(app_dir / "schemas.py")) == [
        "UserBase", "UserCreate", "UserPublic", "ItemCreate", "ItemPublic", "Unused",
    ]


def test_inspect_model(app_dir: Path):
    result = server.inspect_model(str(app_dir / "schemas.py"), "UserCreate")

    assert result["fields"] == [{"name": "password", "type": "str", "default": None}]
    assert result["validators"] == [{"function": "password_length", "field": "password"}]


def test_inspect_model_not_found(app_dir: Path):
    assert "error" in server.inspect_model(str(app_dir / "schemas.py"), "Nope")


def test_find_model_usages(app_dir: Path):
    usages = server.find_model_usages(str(app_dir / "schemas.py"), "UserCreate")

    assert {(Path(u["file"]).name, u["function"]) for u in usages} == {
        ("crud.py", "create_user"), ("users.py", "create_user"),
    }


def test_detect_schema_orm_mismatches(app_dir: Path):
    result = server.detect_schema_orm_mismatches(
        str(app_dir / "database.py"), str(app_dir / "schemas.py"), "User", "UserPublic",
    )

    assert result["in_orm_not_in_schema"] == ["hashed_password"]
    assert result["in_schema_not_in_orm"] == []
    assert set(result["matching_fields"]) == {"id", "email", "items"}
