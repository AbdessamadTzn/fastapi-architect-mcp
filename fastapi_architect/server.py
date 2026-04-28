from mcp.server.fastmcp import FastMCP
import jedi
import ast
from pathlib import Path

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
def list_routes(file: str) -> list[dict]:
    """List all FastAPI routes defined in a file, including those on APIRouter instances."""
    tree = _parse(file)
    routes = []

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
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })

    return routes


@mcp.tool()
def get_dependencies(file: str, handler: str) -> dict:
    """Get the full Depends() injection tree for a FastAPI handler."""
    tree = _parse(file)

    dep_map: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        deps = []
        for default in node.args.defaults:
            if (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id == "Depends"
                and default.args
                and isinstance(default.args[0], ast.Name)
            ):
                deps.append(default.args[0].id)
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
        return {"error": f"Handler '{handler}' not found in {file}"}

    return build_tree(handler)


# ─── Pydantic Intelligence (AST) ──────────────────────────────────────────────

@mcp.tool()
def list_models(file: str) -> list[str]:
    """List all Pydantic BaseModel classes defined in a file."""
    tree = _parse(file)
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases)
    ]


@mcp.tool()
def inspect_model(file: str, model: str) -> dict:
    """Inspect a Pydantic model: fields with types/defaults, and validators."""
    tree = _parse(file)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == model):
            continue

        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
        if "BaseModel" not in bases:
            return {"error": f"'{model}' does not inherit from BaseModel"}

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

    for py_file in Path(str(project.path)).rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                if ast.unparse(node.annotation) == model:
                    results.append({"file": str(py_file), "line": node.lineno})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args:
                    if arg.annotation and ast.unparse(arg.annotation) == model:
                        results.append({"file": str(py_file), "line": arg.col_offset, "function": node.name})

    return results


def main():
    mcp.run()


if __name__ == "__main__":
    main()
