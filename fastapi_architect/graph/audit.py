"""Project-wide checks over the knowledge graph."""
import re
from collections import Counter, defaultdict

import networkx as nx

from fastapi_architect.graph.model import FUNCTION_TYPES, EdgeType, KnowledgeGraph, NodeType
from fastapi_architect.graph.queries import describe, direct_dependencies

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
AUTH_NAME = re.compile(
    r"auth|current_user|current_active|token|jwt|oauth|admin|permission|scope|api_?key|bearer|credential"
    r"|login|verify|require_|security|principal|access_code|signature",
    re.I,
)
AUTH_HEADERS = re.compile(r"authorization|api_?key|token|signature|secret", re.I)
LIKELY_PUBLIC = re.compile(
    r"auth|login|logout|register|signup|sign_up|subscribe|webhook|callback|token|health|password_reset|forgot|verify",
    re.I,
)


def route_auth(kg: KnowledgeGraph, route_id: str, extra_auth: set[str] = frozenset(), call_depth: int = 2) -> dict | None:
    """How a route is protected, or None. Checks, in order: dependency tree, auth headers, manual auth calls."""
    handler = kg.successors(route_id, EdgeType.HANDLED_BY)[0]

    def is_auth(node_id: str) -> bool:
        name = kg.node(node_id)["name"].rsplit(".", 1)[-1]
        return name in extra_auth or bool(AUTH_NAME.search(name))

    seen, stack = set(), list(direct_dependencies(kg, handler))
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        if is_auth(dep):
            return {"kind": "dependency", "by": kg.node(dep)["name"]}
        stack.extend(direct_dependencies(kg, dep))

    for param, source in kg.node(handler).get("param_sources", {}).items():
        if source == "Header" and AUTH_HEADERS.search(param):
            return {"kind": "header", "by": param}

    frontier, visited = [handler], {handler}
    for _ in range(call_depth):
        next_frontier = []
        for fn in frontier:
            for callee in kg.successors(fn, EdgeType.CALLS):
                if callee in visited:
                    continue
                visited.add(callee)
                if is_auth(callee):
                    return {"kind": "manual_call", "by": kg.node(callee)["name"]}
                next_frontier.append(callee)
        frontier = next_frontier
    return None


def audit(kg: KnowledgeGraph, auth_dependencies: list[str] | None = None) -> dict:
    extra_auth = set(auth_dependencies or [])
    routes = kg.nodes(NodeType.ROUTE)

    auth_kinds: Counter[str] = Counter()
    unprotected_writes = []
    for route_id in routes:
        auth = route_auth(kg, route_id, extra_auth)
        auth_kinds[auth["kind"] if auth else "none"] += 1
        route = kg.node(route_id)
        if auth is None and route["method"] in WRITE_METHODS:
            handler = kg.successors(route_id, EdgeType.HANDLED_BY)[0]
            unprotected_writes.append({
                **describe(kg, route_id),
                "handler": handler,
                "likely_public": bool(LIKELY_PUBLIC.search(route["name"]) or LIKELY_PUBLIC.search(handler)),
            })

    by_endpoint = defaultdict(list)
    for route_id in routes:
        route = kg.node(route_id)
        for path in route["full_paths"]:
            by_endpoint[(route["method"], path)].append(route_id)
    duplicates = [
        {"method": method, "path": path, "routes": [describe(kg, r) for r in ids]}
        for (method, path), ids in sorted(by_endpoint.items()) if len(ids) > 1
    ]

    unused_schemas = [
        describe(kg, n) for n in kg.nodes(NodeType.SCHEMA)
        if not [s for s, d in kg.in_edges(n) if d["type"] != EdgeType.DEFINES]
    ]
    unreferenced_orm = [
        describe(kg, n) for n in kg.nodes(NodeType.ORM_MODEL)
        if not [s for s, d in kg.in_edges(n) if d["type"] not in (EdgeType.DEFINES, EdgeType.RELATES_TO, EdgeType.MIRRORS)]
        and not any(kg.predecessors(t, EdgeType.QUERIES) for t in kg.successors(n, EdgeType.MAPS_TO))  # used via raw SQL
    ]

    depends = nx.DiGraph([(s, t) for s, t, d in kg.g.edges(data=True) if d["type"] == EdgeType.DEPENDS_ON])
    cycles = [[kg.node(n)["name"] for n in cycle] for cycle in nx.simple_cycles(depends)][:20]

    return {
        "summary": {
            "routes": len(routes),
            "route_auth": dict(auth_kinds),
            "unprotected_write_routes": len(unprotected_writes),
            "duplicate_routes": len(duplicates),
            "unmounted_routes": sum(not kg.node(r)["mounted"] for r in routes),
            "unused_schemas": len(unused_schemas),
            "unreferenced_orm_models": len(unreferenced_orm),
            "dependency_cycles": len(cycles),
            "parse_errors": sum("error" in kg.node(m) for m in kg.nodes(NodeType.MODULE)),
        },
        "unprotected_write_routes": unprotected_writes,
        "duplicate_routes": duplicates,
        "unmounted_routes": [describe(kg, r) for r in routes if not kg.node(r)["mounted"]],
        "unused_schemas": unused_schemas,
        "unreferenced_orm_models": unreferenced_orm,
        "dependency_cycles": cycles,
        "parse_errors": [
            {"module": kg.node(m)["name"], "file": kg.node(m)["file"], "error": kg.node(m)["error"]}
            for m in kg.nodes(NodeType.MODULE) if "error" in kg.node(m)
        ],
        "notes": [
            "Auth is inferred from names (dependencies, Header params, called functions); "
            "middleware-based auth is not detected. Pass auth_dependencies to declare custom guards.",
        ],
    }


def is_test_file(file: str) -> bool:
    parts = file.split("/")
    return any(p in ("tests", "test") for p in parts[:-1]) or parts[-1].startswith("test_") or parts[-1] == "conftest.py"


def hubs(kg: KnowledgeGraph, limit: int = 10) -> list[dict]:
    """Most connected project symbols (DEFINES edges, modules, tables and test files excluded)."""
    degree: Counter[str] = Counter()
    for s, t, d in kg.g.edges(data=True):
        if d["type"] != EdgeType.DEFINES:
            degree[s] += 1
            degree[t] += 1
    ranked = [
        n for n, _ in degree.most_common()
        if kg.type_of(n) in FUNCTION_TYPES | {NodeType.SCHEMA, NodeType.ORM_MODEL, NodeType.CLASS}
        and not kg.node(n).get("external")
        and not is_test_file(kg.node(n).get("file", ""))
    ]
    return [{**describe(kg, n), "degree": degree[n]} for n in ranked[:limit]]
