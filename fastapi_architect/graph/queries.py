"""Read-only queries over a KnowledgeGraph."""
from fastapi_architect.graph.model import FUNCTION_TYPES, EdgeType, KnowledgeGraph, NodeType


def find_nodes(kg: KnowledgeGraph, name: str, types: set[NodeType] | None = None) -> list[str]:
    """Nodes matching an id, a qualname ("Class.method") or a short name, optionally filtered by type."""
    if name in kg and (types is None or kg.type_of(name) in types):
        return [name]
    return [
        n for n in kg.nodes(*(types or ()))
        if kg.node(n)["name"] == name or kg.node(n)["name"].rsplit(".", 1)[-1] == name
    ]


def short_name(kg: KnowledgeGraph, node_id: str) -> str:
    return kg.node(node_id)["name"]


def route_level_dependencies(kg: KnowledgeGraph, route_id: str) -> list[str]:
    """Dependencies applied to a route outside its handler signature, outermost first:
    parent routers first; for each router, include_router(dependencies=) then APIRouter(dependencies=);
    decorator-level last."""
    ordered: list[str] = []

    def add(node_ids: list[str]) -> None:
        ordered.extend(n for n in node_ids if n not in ordered)

    def router_chain(owner: str, seen: frozenset[str]) -> None:
        for parent in kg.predecessors(owner, EdgeType.INCLUDES):
            if parent not in seen:
                router_chain(parent, seen | {owner})
        deps = kg.out_edges(owner, EdgeType.DEPENDS_ON)
        add([d for d, attrs in deps if attrs.get("via") == "include_router"])  # FastAPI prepends these
        add([d for d, _ in deps])

    for owner in kg.predecessors(route_id, EdgeType.HAS_ROUTE):
        router_chain(owner, frozenset())
    add(kg.successors(route_id, EdgeType.DEPENDS_ON))
    return ordered


def direct_dependencies(kg: KnowledgeGraph, node_id: str) -> list[str]:
    """Route-level dependencies (for handlers) followed by the node's own Depends()."""
    deps: list[str] = []
    for route_id in kg.predecessors(node_id, EdgeType.HANDLED_BY):
        deps.extend(d for d in route_level_dependencies(kg, route_id) if d not in deps)
    deps.extend(d for d in kg.successors(node_id, EdgeType.DEPENDS_ON) if d not in deps)
    return deps


def dependency_tree(kg: KnowledgeGraph, node_id: str, _seen: frozenset[str] = frozenset()) -> dict:
    tree: dict = {"name": short_name(kg, node_id), "id": node_id}
    if node_id in _seen:
        return {**tree, "dependencies": [], "circular": True}
    children = direct_dependencies(kg, node_id)
    tree["dependencies"] = [dependency_tree(kg, c, _seen | {node_id}) for c in children]
    return tree


def handler_nodes(kg: KnowledgeGraph, name: str) -> list[str]:
    """Function nodes for `name`, preferring actual route handlers when the name is ambiguous."""
    matches = find_nodes(kg, name, FUNCTION_TYPES)
    handlers = [m for m in matches if kg.type_of(m) == NodeType.HANDLER]
    return handlers if len(handlers) == 1 else matches


# ─── node lookup ──────────────────────────────────────────────────────────────

def resolve_node(kg: KnowledgeGraph, query: str) -> tuple[str | None, list[str]]:
    """Find a single node for a user query: graph id, "METHOD /path", "/path", qualname or short name.

    Returns (node_id, []) on a unique match, else (None, candidates).
    """
    query = query.strip()
    if query in kg:
        return query, []

    method, _, path = query.partition(" ") if query.split(" ", 1)[0].isupper() else ("", "", query)
    if path.startswith("/"):
        routes = [
            r for r in kg.nodes(NodeType.ROUTE)
            if path in kg.node(r)["full_paths"] and (not method or kg.node(r)["method"] == method.upper())
        ]
        return (routes[0], []) if len(routes) == 1 else (None, sorted(routes))

    matches = find_nodes(kg, query)
    non_modules = [m for m in matches if kg.type_of(m) != NodeType.MODULE]
    if len(non_modules) == 1:
        return non_modules[0], []
    if not non_modules and len(matches) == 1:
        return matches[0], []
    return None, sorted(matches)


def describe(kg: KnowledgeGraph, node_id: str) -> dict:
    node = kg.node(node_id)
    info = {"id": node_id, "type": str(node["type"]), "name": node["name"]}
    for key in ("file", "line", "full_paths", "external"):
        if key in node:
            info[key] = node[key]
    return info


def not_found(kg: KnowledgeGraph, query: str, candidates: list[str]) -> dict:
    if candidates:
        return {"error": f"'{query}' is ambiguous", "candidates": [describe(kg, c) for c in candidates[:20]]}
    return {"error": f"'{query}' not found in the knowledge graph"}


# ─── neighborhood ─────────────────────────────────────────────────────────────

def neighbors(
    kg: KnowledgeGraph,
    node_id: str,
    depth: int = 1,
    edge_types: set[EdgeType] | None = None,
    direction: str = "both",
    limit: int = 200,
) -> dict:
    """Breadth-first neighborhood. DEFINES edges are skipped unless explicitly requested."""
    allowed = edge_types or set(EdgeType) - {EdgeType.DEFINES}
    seen = {node_id}
    frontier = [node_id]
    found: list[dict] = []

    for distance in range(1, depth + 1):
        next_frontier = []
        for current in frontier:
            steps = []
            if direction in ("out", "both"):
                steps += [(t, d, "out") for t, d in kg.out_edges(current) if d["type"] in allowed]
            if direction in ("in", "both"):
                steps += [(s, d, "in") for s, d in kg.in_edges(current) if d["type"] in allowed]
            for other, data, dir_ in steps:
                if other in seen:
                    continue
                seen.add(other)
                next_frontier.append(other)
                found.append({
                    **describe(kg, other),
                    "edge": str(data["type"]),
                    "direction": dir_,
                    "from": current,
                    "distance": distance,
                })
                if len(found) >= limit:
                    return {"node": describe(kg, node_id), "neighbors": found, "truncated": True}
        frontier = next_frontier

    return {"node": describe(kg, node_id), "neighbors": found, "truncated": False}


# ─── impact analysis ──────────────────────────────────────────────────────────

# A ─E→ B means "A relies on B": when B changes, A is impacted.
IMPACT_EDGES = {
    EdgeType.CALLS, EdgeType.USES, EdgeType.ACCEPTS, EdgeType.RETURNS, EdgeType.DEPENDS_ON,
    EdgeType.INHERITS, EdgeType.HANDLED_BY, EdgeType.MIRRORS, EdgeType.MAPS_TO, EdgeType.REFERENCES,
    EdgeType.QUERIES, EdgeType.RENDERS, EdgeType.MIDDLEWARE,
}

_IMPACT_GROUPS = {
    NodeType.ROUTE: "routes",
    NodeType.HANDLER: "handlers",
    NodeType.DEPENDENCY: "dependencies",
    NodeType.MIDDLEWARE: "middlewares",
    NodeType.FUNCTION: "functions",
    NodeType.SCHEMA: "schemas",
    NodeType.ORM_MODEL: "orm_models",
    NodeType.CLASS: "classes",
    NodeType.TABLE: "tables",
    NodeType.ROUTER: "routers",
    NodeType.APP: "apps",
}


def impact(kg: KnowledgeGraph, node_id: str, max_depth: int = 6) -> dict:
    """Everything that (transitively) relies on `node_id`, grouped by kind, with the chain for routes."""
    parent: dict[str, str | None] = {node_id: None}
    distance = {node_id: 0}
    queue = [node_id]

    while queue:
        current = queue.pop(0)
        if distance[current] >= max_depth:
            continue
        impacted = kg.predecessors(current, *IMPACT_EDGES)
        if current != node_id:
            # MIRRORS is a name/field heuristic: only follow it from the queried node itself
            mirrors = set(kg.predecessors(current, EdgeType.MIRRORS)) - set(kg.predecessors(current, *(IMPACT_EDGES - {EdgeType.MIRRORS})))
            impacted = [n for n in impacted if n not in mirrors]
        if kg.type_of(current) in (NodeType.ROUTER, NodeType.APP):
            # router-level dependency changed: every route under it is impacted
            impacted += kg.successors(current, EdgeType.HAS_ROUTE, EdgeType.INCLUDES)
        for other in impacted:
            if other not in parent:
                parent[other] = current
                distance[other] = distance[current] + 1
                queue.append(other)

    groups: dict[str, list[dict]] = {}
    for other in parent:
        if other == node_id or kg.type_of(other) == NodeType.MODULE:
            continue
        item = {**describe(kg, other), "distance": distance[other]}
        if kg.type_of(other) == NodeType.ROUTE:
            chain, cursor = [], other
            while cursor is not None:
                chain.append(kg.node(cursor)["name"])
                cursor = parent[cursor]
            item["via"] = chain
        groups.setdefault(_IMPACT_GROUPS.get(kg.type_of(other), "other"), []).append(item)

    for items in groups.values():
        items.sort(key=lambda i: (i["distance"], i["name"]))
    return {
        "target": describe(kg, node_id),
        "summary": {group: len(items) for group, items in groups.items()},
        **groups,
    }


# ─── paths ────────────────────────────────────────────────────────────────────

def paths(kg: KnowledgeGraph, source: str, target: str, max_paths: int = 3, max_length: int = 12) -> dict:
    """Shortest paths from source to target (directed first, then ignoring direction). DEFINES excluded."""
    import itertools

    import networkx as nx

    simple = nx.DiGraph()
    simple.add_nodes_from(kg.g.nodes)
    for s, t, d in kg.g.edges(data=True):
        if d["type"] == EdgeType.DEFINES:
            continue
        if simple.has_edge(s, t):
            simple.edges[s, t]["types"].append(str(d["type"]))
        else:
            simple.add_edge(s, t, types=[str(d["type"])])

    for directed, graph in ((True, simple), (False, simple.to_undirected(as_view=True))):
        try:
            found = [
                p for p in itertools.islice(nx.shortest_simple_paths(graph, source, target), max_paths)
                if len(p) <= max_length + 1
            ]
        except nx.NetworkXNoPath:
            continue
        if found:
            return {
                "source": describe(kg, source),
                "target": describe(kg, target),
                "directed": directed,
                "paths": [_format_path(kg, simple, p) for p in found],
            }
    return {"source": describe(kg, source), "target": describe(kg, target), "paths": []}


def _format_path(kg: KnowledgeGraph, simple, path: list[str]) -> list[dict]:
    steps = []
    for i, node_id in enumerate(path):
        step = {"id": node_id, "type": str(kg.type_of(node_id)), "name": kg.node(node_id)["name"]}
        if i + 1 < len(path):
            nxt = path[i + 1]
            if simple.has_edge(node_id, nxt):
                step["edge"] = "/".join(simple.edges[node_id, nxt]["types"])
            else:
                step["edge"] = "/".join(simple.edges[nxt, node_id]["types"]) + " (reversed)"
        steps.append(step)
    return steps
