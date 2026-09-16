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
