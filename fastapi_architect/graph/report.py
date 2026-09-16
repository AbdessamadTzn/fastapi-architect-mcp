"""Markdown overview of a knowledge graph."""
from fastapi_architect.graph.audit import audit, hubs, route_auth
from fastapi_architect.graph.model import EdgeType, KnowledgeGraph, NodeType
from fastapi_architect.graph.queries import short_name


def render_report(kg: KnowledgeGraph, project_name: str) -> str:
    stats = kg.stats()
    findings = audit(kg)
    lines = [
        f"# FastAPI knowledge graph — {project_name}",
        "",
        f"{stats['nodes']} nodes, {stats['edges']} edges. "
        + ", ".join(f"{count} {kind}" for kind, count in sorted(stats["node_types"].items()) if kind != "Module"),
        "",
        "## Routes",
        "",
        "| Method | Path | Handler | Auth |",
        "|---|---|---|---|",
    ]
    for route_id in sorted(kg.nodes(NodeType.ROUTE), key=lambda r: (kg.node(r)["full_paths"][0], kg.node(r)["method"])):
        route = kg.node(route_id)
        handler = kg.successors(route_id, EdgeType.HANDLED_BY)[0]
        auth = route_auth(kg, route_id)
        lines.append(
            f"| {route['method']} | `{route['full_paths'][0]}` | `{handler}` | "
            + (f"{auth['kind']}: `{auth['by']}`" if auth else "—") + " |"
        )

    lines += ["", "## Most connected symbols", ""]
    lines += [f"- `{h['id']}` ({h['type']}, degree {h['degree']})" for h in hubs(kg)]

    tables = kg.nodes(NodeType.TABLE)
    if tables:
        lines += ["", "## Tables", ""]
        for table in sorted(tables):
            models = [short_name(kg, m) for m in kg.predecessors(table, EdgeType.MAPS_TO)]
            queried_by = kg.predecessors(table, EdgeType.QUERIES)
            lines.append(
                f"- `{kg.node(table)['name']}`"
                + (f" ← model {', '.join(models)}" if models else "")
                + (f", raw SQL in {len(queried_by)} place(s)" if queried_by else "")
            )

    summary = findings["summary"]
    lines += ["", "## Audit", ""]
    lines.append(f"- Write routes without detected auth: {summary['unprotected_write_routes']}")
    lines += [
        f"  - {r['name']}" + (" (likely public)" if r["likely_public"] else "")
        for r in findings["unprotected_write_routes"]
    ]
    for key, label in [
        ("duplicate_routes", "Duplicate routes"),
        ("unmounted_routes", "Routes on routers never included"),
        ("unused_schemas", "Unused schemas"),
        ("unreferenced_orm_models", "Unreferenced ORM models"),
        ("dependency_cycles", "Dependency cycles"),
        ("parse_errors", "Files with syntax errors"),
    ]:
        lines.append(f"- {label}: {summary[key]}")
        if key in ("unused_schemas", "unreferenced_orm_models") and findings[key]:
            lines.append("  - " + ", ".join(f"`{item['name']}`" for item in findings[key]))
    return "\n".join(lines) + "\n"
