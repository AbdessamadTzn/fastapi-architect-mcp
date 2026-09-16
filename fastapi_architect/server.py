from mcp.server.fastmcp import FastMCP
import jedi
import ast
from pathlib import Path

from fastapi_architect.files import iter_python_files as _iter_python_files
from fastapi_architect.graph import EdgeType, NodeType, load_graph
from fastapi_architect.graph.audit import audit
from fastapi_architect.graph.cache import CACHE_DIR, CACHE_FILE
from fastapi_architect.graph.export import render_html
from fastapi_architect.graph.report import render_report
from fastapi_architect.graph.queries import (
    dependency_tree,
    direct_dependencies,
    handler_nodes,
    impact,
    neighbors,
    not_found,
    paths,
    resolve_node,
    short_name,
)

mcp = FastMCP("fastapi-architect")


def _project(file: str) -> jedi.Project:
    """Walk up from file to find the project root."""
    path = Path(file).resolve()
    for parent in [path.parent, *path.parents]:
        if any((parent / f).exists() for f in ("pyproject.toml", "requirements.txt", "setup.py")):
            return jedi.Project(path=str(parent))
    return jedi.Project(path=str(path.parent))


def _parse(file: str) -> ast.Module:
    with open(file) as f:
        return ast.parse(f.read())


# ─── Python Intelligence (Jedi) ───────────────────────────────────────────────

@mcp.tool()
def find_references(file: str, line: int, column: int) -> list[dict]:
    """Find all references to the symbol at the given position across the project."""
    script = jedi.Script(path=file, project=_project(file))
    return [
        {
            "file": str(r.module_path),
            "line": r.line,
            "column": r.column,
            "name": r.name,
        }
        for r in script.get_references(line=line, column=column)
        if r.module_path is not None
    ]


@mcp.tool()
def rename_symbol(file: str, line: int, column: int, new_name: str) -> dict:
    """Rename the symbol at the given position across all files in the project."""
    script = jedi.Script(path=file, project=_project(file))
    refs = [r for r in script.get_references(line=line, column=column) if r.module_path is not None]

    changed_files: dict[str, list[str]] = {}

    for r in sorted(refs, key=lambda r: (str(r.module_path), r.line), reverse=True):
        path = str(r.module_path)
        if path not in changed_files:
            with open(path) as f:
                changed_files[path] = f.readlines()
        lines = changed_files[path]
        col = r.column
        idx = r.line - 1
        lines[idx] = lines[idx][:col] + new_name + lines[idx][col + len(r.name):]

    for path, lines in changed_files.items():
        with open(path, "w") as f:
            f.writelines(lines)

    return {
        "renamed_to": new_name,
        "files_changed": len(changed_files),
        "references_updated": len(refs),
    }


@mcp.tool()
def go_to_definition(file: str, line: int, column: int) -> dict | None:
    """Return the file and line where the symbol at the given position is defined."""
    script = jedi.Script(path=file, project=_project(file))
    defs = script.goto(line=line, column=column, follow_imports=True)
    for d in defs:
        if d.module_path:
            return {"file": str(d.module_path), "line": d.line, "column": d.column, "name": d.name}
    return None


@mcp.tool()
def get_completions(file: str, line: int, column: int) -> list[dict]:
    """Return completion suggestions at the given cursor position."""
    script = jedi.Script(path=file, project=_project(file))
    return [
        {"name": c.name, "type": c.type, "description": c.docstring(raw=True)[:120]}
        for c in script.complete(line=line, column=column)
    ]


# ─── FastAPI Intelligence (AST) ───────────────────────────────────────────────

@mcp.tool()
def list_routes(project_root: str) -> list[dict]:
    """List all FastAPI routes across the entire project, with full paths (router prefixes applied)."""
    kg = load_graph(project_root).graph
    root = Path(project_root).resolve()
    routes = []
    for route_id in kg.nodes(NodeType.ROUTE):
        route = kg.node(route_id)
        handler = kg.node(kg.successors(route_id, EdgeType.HANDLED_BY)[0])
        routes.append({
            "method": route["method"],
            "path": route["full_paths"][0],
            "full_paths": route["full_paths"],
            "handler": handler["name"],
            "line": handler["line"],
            "file": str(root / route["file"]),
            "is_async": handler["is_async"],
            "mounted": route["mounted"],
        })
    return sorted(routes, key=lambda r: (r["file"], r["line"], r["method"]))


@mcp.tool()
def get_dependencies(project_root: str, handler: str) -> dict:
    """Get the full Depends() injection tree for a FastAPI handler across the entire project.

    Includes router-level, include_router, decorator-level and signature dependencies.
    `handler` may be a function name, a "Class.method" qualname, or a graph id ("app.routes.users:list_users").
    """
    kg = load_graph(project_root).graph
    matches = handler_nodes(kg, handler)
    if not matches:
        return {"error": f"Handler '{handler}' not found in project"}
    if len(matches) > 1:
        return {"error": f"Handler '{handler}' is ambiguous", "candidates": sorted(matches)}
    return dependency_tree(kg, matches[0])


# ─── Pydantic Intelligence (AST) ──────────────────────────────────────────────

@mcp.tool()
def list_models(file: str) -> list[str]:
    """List all Pydantic BaseModel classes defined in a file."""
    tree = _parse(file)
    _model_bases = {"BaseModel", "SQLModel", "BaseSettings", "RootModel"}

    class_bases: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]

    def is_model(name: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if name in seen:
            return False
        seen.add(name)
        if name in _model_bases:
            return True
        return any(is_model(b, seen) for b in class_bases.get(name, []))

    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and is_model(node.name)
    ]


@mcp.tool()
def inspect_model(file: str, model: str) -> dict:
    """Inspect a Pydantic model: fields with types/defaults, and validators."""
    tree = _parse(file)
    _model_bases = {"BaseModel", "SQLModel", "BaseSettings", "RootModel"}

    # build full class hierarchy map
    class_bases: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]

    def is_model(name: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if name in seen:
            return False
        seen.add(name)
        if name in _model_bases:
            return True
        return any(is_model(b, seen) for b in class_bases.get(name, []))

    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == model):
            continue

        if not is_model(model):
            return {"error": f"'{model}' does not inherit from a known model base class"}

        fields = []
        validators = []

        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append({
                    "name": item.target.id,
                    "type": ast.unparse(item.annotation),
                    "default": ast.unparse(item.value) if item.value else None,
                })
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in item.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Name)
                        and dec.func.id in ("validator", "field_validator")
                        and dec.args
                    ):
                        validators.append({
                            "function": item.name,
                            "field": dec.args[0].value if isinstance(dec.args[0], ast.Constant) else ast.unparse(dec.args[0]),
                        })

        return {"model": model, "fields": fields, "validators": validators}

    return {"error": f"Model '{model}' not found in {file}"}


@mcp.tool()
def find_model_usages(file: str, model: str) -> list[dict]:
    """Find all places a Pydantic model is used as a type annotation across the project."""
    project = _project(file)
    results = []

    for py_file in _iter_python_files(str(project.path)):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                if ast.unparse(node.annotation) == model:
                    results.append({"file": str(py_file), "line": node.lineno})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.kwonlyargs:
                    if arg.annotation and ast.unparse(arg.annotation) == model:
                        results.append({"file": str(py_file), "line": node.lineno, "function": node.name})

    return results

@mcp.tool()
def validate_response_models(file: str) -> list[dict]:
    """Detect FastAPI routes missing a response_model declaration."""
    tree = _parse(file)
    issues = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("get", "post", "put", "patch", "delete"):
                continue
            keywords = [kw.arg for kw in decorator.keywords]
            if "response_model" not in keywords:
                path = decorator.args[0].value if decorator.args else "unknown"
                issues.append({
                    "method": func.attr.upper(),
                    "path": path,
                    "handler": node.name,
                    "line": node.lineno,
                })

    return issues

@mcp.tool()
def build_dependency_graph(file: str, project_root: str) -> list[dict]:
    """Build a full dependency graph for the routes of a file: route → handler → dependencies → models."""
    kg = load_graph(project_root).graph
    root = Path(project_root).resolve()
    rel = Path(file).resolve().relative_to(root).as_posix()

    graph = []
    for route_id in kg.nodes(NodeType.ROUTE):
        route = kg.node(route_id)
        if route["file"] != rel:
            continue
        handler_id = kg.successors(route_id, EdgeType.HANDLED_BY)[0]
        graph.append({
            "method": route["method"],
            "path": route["full_paths"][0],
            "local_path": route["path"],
            "handler": short_name(kg, handler_id),
            "input_models": [short_name(kg, m) for m in kg.successors(handler_id, EdgeType.ACCEPTS)],
            "dependencies": [short_name(kg, d) for d in direct_dependencies(kg, handler_id)],
            "response_model": route["response_model"],
            "line": route["line"],
        })
    return sorted(graph, key=lambda r: (r["line"], r["method"]))

@mcp.tool()
def detect_schema_orm_mismatches(orm_file: str, schema_file: str, orm_model: str, schema_model: str) -> dict:
    """Detect field mismatches between a SQLAlchemy ORM model and a Pydantic schema."""
    orm_tree = _parse(orm_file)
    schema_tree = _parse(schema_file)

    def _extract_fields(tree: ast.Module, class_name: str) -> set[str]:
        # build class hierarchy to include inherited fields
        class_bodies: dict[str, list] = {}
        class_bases_map: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_bodies[node.name] = node.body
                class_bases_map[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]

        def get_all_fields(name: str, seen: set[str] | None = None) -> set[str]:
            seen = seen or set()
            if name in seen:
                return set()
            seen.add(name)
            fields = set()
            for item in class_bodies.get(name, []):
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    # include all annotated fields (mapped_column, Field, or plain)
                    fields.add(item.target.id)
            for base in class_bases_map.get(name, []):
                fields |= get_all_fields(base, seen)
            return fields

        return get_all_fields(class_name)

    orm_fields = _extract_fields(orm_tree, orm_model)
    schema_fields = _extract_fields(schema_tree, schema_model)

    return {
        "orm_model": orm_model,
        "schema_model": schema_model,
        "in_schema_not_in_orm": list(schema_fields - orm_fields),
        "in_orm_not_in_schema": list(orm_fields - schema_fields),
        "matching_fields": list(orm_fields & schema_fields),
    }



# ─── Knowledge Graph ──────────────────────────────────────────────────────────

@mcp.tool()
def build_knowledge_graph(project_root: str, force: bool = False) -> dict:
    """Build or refresh the project's FastAPI knowledge graph and return its statistics.

    The graph links routes, handlers, dependencies, schemas, ORM models, SQL tables, templates and
    function calls. It is cached in <project_root>/.fastapi-architect/ and only changed files are
    re-parsed, so other graph tools are fast. Use force=True to rebuild from scratch.
    """
    result = load_graph(project_root, force=force)
    kg = result.graph
    errors = [
        {"file": kg.node(m)["file"], "error": kg.node(m)["error"]}
        for m in kg.nodes(NodeType.MODULE) if "error" in kg.node(m)
    ]
    return {
        **kg.stats(),
        "reparsed_files": len(result.reparsed),
        "reused_files": result.reused,
        "removed_files": result.removed,
        "parse_errors": errors,
        "cache": str(Path(project_root).resolve() / CACHE_DIR / CACHE_FILE),
    }


@mcp.tool()
def graph_neighbors(
    project_root: str,
    node: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    direction: str = "both",
) -> dict:
    """Explore the knowledge graph around a node.

    `node` can be a graph id, a route ("GET /api/users/" or "/api/users/"), a table name, or a
    function/class name. `direction` is "out" (what it uses), "in" (what uses it) or "both".
    `edge_types` filters edges, e.g. ["DEPENDS_ON", "CALLS"]. Valid types: INCLUDES, HAS_ROUTE,
    HANDLED_BY, DEPENDS_ON, ACCEPTS, RETURNS, CALLS, USES, QUERIES, RENDERS, MIDDLEWARE, INHERITS,
    MAPS_TO, REFERENCES, RELATES_TO, MIRRORS, DEFINES (excluded by default).
    """
    kg = load_graph(project_root).graph
    node_id, candidates = resolve_node(kg, node)
    if node_id is None:
        return not_found(kg, node, candidates)
    if direction not in ("in", "out", "both"):
        return {"error": "direction must be 'in', 'out' or 'both'"}
    try:
        types = {EdgeType(t.upper()) for t in edge_types} if edge_types else None
    except ValueError:
        return {"error": f"Unknown edge type in {edge_types}", "valid_edge_types": list(EdgeType)}
    return neighbors(kg, node_id, depth=max(1, min(depth, 5)), edge_types=types, direction=direction)


@mcp.tool()
def impact_analysis(project_root: str, symbol: str, max_depth: int = 6) -> dict:
    """What is affected if `symbol` changes: routes, handlers, dependencies, schemas, ORM models...

    Follows reverse edges (callers, users, dependents, subclasses, mirroring schemas, tables → models).
    Each impacted route includes a `via` chain explaining why. `symbol` accepts the same forms as
    graph_neighbors (e.g. "User", "get_db", "users" for a table, "app.models.user:User").
    """
    kg = load_graph(project_root).graph
    node_id, candidates = resolve_node(kg, symbol)
    if node_id is None:
        return not_found(kg, symbol, candidates)
    return impact(kg, node_id, max_depth=max_depth)


@mcp.tool()
def find_path(project_root: str, source: str, target: str, max_paths: int = 3) -> dict:
    """Shortest paths between two nodes, e.g. from a route to a table ("POST /chat" → "chat_logs").

    Directed paths are tried first; if none exists, direction is ignored and `directed` is false.
    """
    kg = load_graph(project_root).graph
    source_id, source_candidates = resolve_node(kg, source)
    if source_id is None:
        return not_found(kg, source, source_candidates)
    target_id, target_candidates = resolve_node(kg, target)
    if target_id is None:
        return not_found(kg, target, target_candidates)
    return paths(kg, source_id, target_id, max_paths=max(1, min(max_paths, 10)))


@mcp.tool()
def audit_graph(project_root: str, auth_dependencies: list[str] | None = None) -> dict:
    """Project-wide checks: write routes without auth, duplicate routes, routers never mounted,
    unused schemas, unreferenced ORM models, dependency cycles and parse errors.

    Auth is inferred from dependency names, auth Header params and calls to auth-like functions.
    Pass `auth_dependencies` (function names) to declare custom guards the heuristics miss.
    """
    return audit(load_graph(project_root).graph, auth_dependencies)


@mcp.tool()
def graph_report(project_root: str, save: bool = False) -> str:
    """A concise Markdown overview of the project: stats, routes with their auth, hubs and audit findings.
    Good first call to understand an unfamiliar FastAPI codebase.
    With save=True the report is also written to <project_root>/.fastapi-architect/GRAPH_REPORT.md."""
    root = Path(project_root).resolve()
    report = render_report(load_graph(root).graph, root.name)
    if save:
        (root / CACHE_DIR).mkdir(exist_ok=True)
        (root / CACHE_DIR / "GRAPH_REPORT.md").write_text(report)
    return report

@mcp.tool()
def export_graph_html(project_root: str, output_path: str | None = None) -> dict:
    """Export the knowledge graph as a standalone interactive HTML page (open it in a browser).

    Nodes are colored by type (Route, Handler, Dependency, Schema, ORMModel, Table...), with search,
    type filters and a details panel listing each node's connections. Defaults to
    <project_root>/.fastapi-architect/graph.html. Rendering loads vis-network from a CDN.
    """
    root = Path(project_root).resolve()
    kg = load_graph(root).graph
    target = Path(output_path).resolve() if output_path else root / CACHE_DIR / "graph.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(kg, f"{root.name} — FastAPI knowledge graph"))
    return {"path": str(target), "nodes": kg.stats()["nodes"], "edges": kg.stats()["edges"]}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
