"""Knowledge graph extraction tests, run against tests/fixtures/sample_app."""
import json
from pathlib import Path

import pytest

from fastapi_architect.graph import EdgeType, KnowledgeGraph, NodeType, build_graph
from fastapi_architect.graph.extract import extract_module, sql_tables


@pytest.fixture(scope="module")
def kg() -> KnowledgeGraph:
    return build_graph(Path(__file__).parent / "fixtures" / "sample_app")


def edges(kg: KnowledgeGraph, type: EdgeType) -> set[tuple[str, str]]:
    return {(s, t) for s, t, d in kg.g.edges(data=True) if d["type"] == type}


def names(kg: KnowledgeGraph, type: NodeType) -> set[str]:
    return {kg.node(n)["name"] for n in kg.nodes(type)}


# ─── node classification ──────────────────────────────────────────────────────

def test_node_types(kg: KnowledgeGraph):
    assert kg.nodes(NodeType.APP) == ["app.main:app"]
    assert set(kg.nodes(NodeType.ROUTER)) == {"app.routes.users:router", "app.routes.items:router"}
    assert names(kg, NodeType.SCHEMA) == {"UserBase", "UserCreate", "UserPublic", "ItemCreate", "ItemPublic", "Unused"}
    assert names(kg, NodeType.ORM_MODEL) == {"User", "Item"}
    assert kg.type_of("app.database:Base") == NodeType.CLASS
    assert set(kg.nodes(NodeType.DEPENDENCY)) == {"app.deps:get_db", "app.deps:get_current_user", "app.deps:require_admin"}
    assert len(kg.nodes(NodeType.HANDLER)) == 6
    assert kg.type_of("app.crud:get_users") == NodeType.FUNCTION


def test_route_full_paths_include_router_prefixes(kg: KnowledgeGraph):
    paths = {kg.node(r)["name"] for r in kg.nodes(NodeType.ROUTE)}

    assert paths == {
        "GET /health", "GET /api/users/", "POST /api/users/", "DELETE /api/users/{user_id}",
        "GET /api/items/", "POST /api/items/",
    }
    assert all(kg.node(r)["mounted"] for r in kg.nodes(NodeType.ROUTE))


# ─── edges ────────────────────────────────────────────────────────────────────

def test_routing_edges(kg: KnowledgeGraph):
    assert edges(kg, EdgeType.INCLUDES) == {
        ("app.main:app", "app.routes.users:router"), ("app.main:app", "app.routes.items:router"),
    }
    assert kg.g.edges["app.main:app", "app.routes.users:router", EdgeType.INCLUDES]["prefix"] == "/api"
    assert ("route:DELETE:app.routes.users:delete_user", "app.routes.users:delete_user") in edges(kg, EdgeType.HANDLED_BY)
    assert ("app.routes.users:router", "route:DELETE:app.routes.users:delete_user") in edges(kg, EdgeType.HAS_ROUTE)


def test_dependency_edges_cover_all_patterns(kg: KnowledgeGraph):
    deps = edges(kg, EdgeType.DEPENDS_ON)

    assert ("app.routes.users:create_user", "app.deps:get_db") in deps              # default Depends()
    assert ("app.routes.users:delete_user", "app.deps:get_db") in deps              # keyword-only Annotated alias
    assert ("route:DELETE:app.routes.users:delete_user", "app.deps:require_admin") in deps  # decorator dependencies=[]
    assert ("app.deps:require_admin", "app.deps:get_current_user") in deps          # alias inside a dependency
    assert ("app.deps:get_current_user", "app.deps:get_db") in deps


def test_model_edges(kg: KnowledgeGraph):
    assert ("app.routes.users:create_user", "app.schemas:UserCreate") in edges(kg, EdgeType.ACCEPTS)
    assert ("app.routes.users:list_users", "app.schemas:UserPublic") in edges(kg, EdgeType.RETURNS)  # list[UserPublic]
    assert ("app.deps:get_current_user", "app.database:User") in edges(kg, EdgeType.RETURNS)          # return annotation
    # dependency-typed params are not "accepted" models
    assert ("app.routes.items:create_item", "app.database:User") not in edges(kg, EdgeType.ACCEPTS)
    assert ("app.schemas:UserCreate", "app.schemas:UserBase") in edges(kg, EdgeType.INHERITS)


def test_call_and_usage_edges(kg: KnowledgeGraph):
    assert ("app.routes.users:list_users", "app.crud:get_users") in edges(kg, EdgeType.CALLS)  # module attribute call
    assert ("app.crud:create_user", "app.database:User") in edges(kg, EdgeType.USES)
    assert ("app.crud:get_users", "app.database:User") in edges(kg, EdgeType.USES)


def test_orm_edges(kg: KnowledgeGraph):
    assert edges(kg, EdgeType.MAPS_TO) == {("app.database:User", "table:users"), ("app.database:Item", "table:items")}
    assert edges(kg, EdgeType.REFERENCES) == {("app.database:Item", "table:users")}
    assert edges(kg, EdgeType.RELATES_TO) == {
        ("app.database:User", "app.database:Item"), ("app.database:Item", "app.database:User"),
    }


def test_raw_sql_query_edges(kg: KnowledgeGraph):
    queries = {(s, t): d["ops"] for s, t, d in kg.g.edges(data=True) if d["type"] == EdgeType.QUERIES}

    assert queries == {
        ("app.routes.items:list_items", "table:items"): ["read"],
        ("app.routes.items:create_item", "table:items"): ["write"],
        ("app.crud:delete_user", "table:users"): ["write"],
    }


def test_mirrors(kg: KnowledgeGraph):
    mirrors = {(s, t): d["confidence"] for s, t, d in kg.g.edges(data=True) if d["type"] == EdgeType.MIRRORS}

    assert mirrors[("app.schemas:UserPublic", "app.database:User")] == 1.0
    assert mirrors[("app.schemas:UserCreate", "app.database:User")] == 0.5  # password is not a column
    assert not any(s == "app.schemas:Unused" for s, _ in mirrors)


def test_serialization_roundtrip(kg: KnowledgeGraph):
    data = json.loads(json.dumps(kg.to_dict()))

    restored = KnowledgeGraph.from_dict(data)

    assert restored.stats() == kg.stats()
    assert restored.node("route:GET:app.routes.users:list_users")["full_paths"] == ["/api/users/"]


# ─── extraction units ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT a FROM users u JOIN bookmarks b ON b.uid = u.id", [("bookmarks", "read"), ("users", "read")]),
    ("DELETE FROM users WHERE id = %s", [("users", "write")]),
    ("INSERT INTO items(title) VALUES (%s)", [("items", "write")]),
    ("UPDATE subscribers SET active = false", [("subscribers", "write")]),
    ("CREATE TABLE IF NOT EXISTS techfi24(id int) -- GUID from RSS", [("techfi24", "ddl")]),
    ("WITH recent AS (SELECT * FROM posts) SELECT * FROM recent, unnest(x)", [("posts", "read")]),
    ("SELECT EXTRACT(EPOCH FROM now()) FROM t WHERE x = 'text from the user'", [("t", "read")]),
    (f"SELECT * FROM {'?'} WHERE 1=1", []),
    ("select the best one from the list", []),
    ("Update the cache from settings", []),
])
def test_sql_tables(sql: str, expected: list):
    assert sql_tables(sql) == expected


def test_extract_ignores_docstrings_and_relative_imports(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        '"""create users table\n\nLa table des (utilisateurs) = vide"""\n'
        "from .db import ops\n"
        "from ..other import thing\n"
    )

    facts = extract_module(pkg / "mod.py", tmp_path)

    assert facts.sql == []
    assert facts.imports == {"ops": ("pkg.db", "ops"), "thing": ("other", "thing")}


def test_syntax_errors_are_reported_not_raised(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def oops(:\n")

    kg = build_graph(tmp_path)

    assert "SyntaxError" in kg.node("module:broken")["error"]
