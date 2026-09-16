from mcp.server.fastmcp import FastMCP
import jedi
import ast
from pathlib import Path

from fastapi_architect.files import iter_python_files as _iter_python_files

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
    defs = script.goto(line=line, column=column)
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
    """List all FastAPI routes across the entire project."""
    routes = []

    for py_file in _iter_python_files(project_root):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in ("get", "post", "put", "patch", "delete", "head", "options"):
                    continue
                path = decorator.args[0].value if decorator.args else "unknown"
                routes.append({
                    "method": func.attr.upper(),
                    "path": path,
                    "handler": node.name,
                    "line": node.lineno,
                    "file": str(py_file),
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                })

    return routes


@mcp.tool()
def get_dependencies(project_root: str, handler: str) -> dict:
    """Get the full Depends() injection tree for a FastAPI handler across the entire project."""
    dep_map: dict[str, list[str]] = {}
    alias_map: dict[str, str] = {}

    for py_file in _iter_python_files(project_root):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue

        # collect Annotated aliases: SessionDep = Annotated[X, Depends(func)]
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Subscript)
                and isinstance(node.targets[0], ast.Name)
                and ast.unparse(node.value).startswith("Annotated[")
            ):
                slc = node.value.slice
                if isinstance(slc, ast.Tuple):
                    for elt in slc.elts:
                        if (
                            isinstance(elt, ast.Call)
                            and isinstance(elt.func, ast.Name)
                            and elt.func.id == "Depends"
                            and elt.args
                            and isinstance(elt.args[0], ast.Name)
                        ):
                            alias_map[node.targets[0].id] = elt.args[0].id

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            deps = []

            # pattern 1: def handler(x=Depends(func))
            for default in node.args.defaults:
                if (
                    isinstance(default, ast.Call)
                    and isinstance(default.func, ast.Name)
                    and default.func.id == "Depends"
                    and default.args
                    and isinstance(default.args[0], ast.Name)
                ):
                    deps.append(default.args[0].id)

            # pattern 2: @router.get("/", dependencies=[Depends(func)])
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                for kw in decorator.keywords:
                    if kw.arg == "dependencies" and isinstance(kw.value, ast.List):
                        for elt in kw.value.elts:
                            if (
                                isinstance(elt, ast.Call)
                                and isinstance(elt.func, ast.Name)
                                and elt.func.id == "Depends"
                                and elt.args
                                and isinstance(elt.args[0], ast.Name)
                            ):
                                deps.append(elt.args[0].id)

            # pattern 3: def handler(x: SessionDep) via Annotated alias
            # covers both regular args and keyword-only args (after *)
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.annotation and isinstance(arg.annotation, ast.Name):
                    if arg.annotation.id in alias_map:
                        deps.append(alias_map[arg.annotation.id])

            dep_map[node.name] = deps

    def build_tree(name: str, seen: set[str] | None = None) -> dict:
        seen = seen or set()
        if name in seen:
            return {"name": name, "dependencies": [], "circular": True}
        seen.add(name)
        return {
            "name": name,
            "dependencies": [build_tree(dep, seen.copy()) for dep in dep_map.get(name, [])],
        }

    if handler not in dep_map:
        return {"error": f"Handler '{handler}' not found in project"}

    return build_tree(handler)


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
    """Build a full dependency graph: route → handler → dependencies → models."""
    tree = _parse(file)

    _primitives = {"int", "str", "float", "bool", "bytes", "Any", "None"}

    # collect Annotated aliases across the whole project
    alias_map: dict[str, str] = {}
    for py_file in _iter_python_files(project_root):
        try:
            t = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(t):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Subscript)
                and isinstance(node.targets[0], ast.Name)
                and ast.unparse(node.value).startswith("Annotated[")
            ):
                slc = node.value.slice
                if isinstance(slc, ast.Tuple):
                    for elt in slc.elts:
                        if (
                            isinstance(elt, ast.Call)
                            and isinstance(elt.func, ast.Name)
                            and elt.func.id == "Depends"
                        ):
                            alias_map[node.targets[0].id] = ast.unparse(elt)

    graph = []
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

            response_model = None
            for kw in decorator.keywords:
                if kw.arg == "response_model":
                    response_model = ast.unparse(kw.value)

            all_args = node.args.args + node.args.kwonlyargs
            defaults_with_depends = {
                d.args[0].id
                for d in node.args.defaults + node.args.kw_defaults
                if d and isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name)
                and d.func.id == "Depends"
                and d.args
                and isinstance(d.args[0], ast.Name)
            }

            input_models = [
                ast.unparse(a.annotation)
                for a in all_args
                if a.annotation
                and ast.unparse(a.annotation) not in _primitives
                and ast.unparse(a.annotation) not in alias_map
                and a.arg not in defaults_with_depends
                and isinstance(a.annotation, ast.Name)
                and a.annotation.id[0].isupper()
            ]

            dependencies = list(defaults_with_depends) + [
                alias_map[ast.unparse(a.annotation)].split("(")[1].rstrip(")")
                for a in all_args
                if a.annotation
                and isinstance(a.annotation, ast.Name)
                and a.annotation.id in alias_map
            ]

            graph.append({
                "method": func.attr.upper(),
                "path": decorator.args[0].value if decorator.args else "unknown",
                "handler": node.name,
                "input_models": input_models,
                "dependencies": dependencies,
                "response_model": response_model,
            })

    return graph

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



def main():
    mcp.run()


if __name__ == "__main__":
    main()
