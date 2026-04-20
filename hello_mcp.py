from mcp.server.fastmcp import FastMCP
import jedi
import ast

mcp = FastMCP("hello-mcp")


@mcp.tool()
def greet(name: str) -> str:
    """Says hello to someone."""
    return f"Hello, {name}!"


@mcp.tool()
def find_references(file: str, line: int, column: int) -> list[dict]:
    """Find all references to the symbol at the given position in a file."""
    script = jedi.Script(path=file)
    refs = script.get_references(line=line, column=column)
    return [
        {
            "file": str(r.module_path),
            "line": r.line,
            "column": r.column,
            "name": r.name,
        }
        for r in refs
    ]


@mcp.tool()
def rename_symbol(file: str, line: int, column: int, new_name: str) -> dict:
    """Rename the symbol at the given position across all files in the project."""
    script = jedi.Script(path=file)
    refs = script.get_references(line=line, column=column)

    changed_files = {}

    for r in sorted(refs, key=lambda r: (r.module_path, r.line), reverse=True):
        if r.module_path is None:
            continue
        path = str(r.module_path)
        if path not in changed_files:
            with open(path) as f:
                changed_files[path] = f.readlines()
        lines = changed_files[path]
        line_idx = r.line - 1
        col = r.column
        lines[line_idx] = lines[line_idx][:col] + new_name + lines[line_idx][col + len(r.name):]

    for path, lines in changed_files.items():
        with open(path, "w") as f:
            f.writelines(lines)

    return {
        "renamed_to": new_name,
        "files_changed": len(changed_files),
        "references_updated": sum(1 for r in refs if r.module_path is not None),
    }

@mcp.tool()
def list_routes(file: str) -> list[dict]:
    """List all FastAPI routes defined in a file."""
    with open(file) as f:
        tree = ast.parse(f.read())

    routes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    func = decorator.func
                    if isinstance(func, ast.Attribute):
                        routes.append({
                            "method": func.attr.upper(),
                            "path": decorator.args[0].value,
                            "handler": node.name,
                            "line": node.lineno,
                        })

    return routes

@mcp.tool()
def get_dependencies(file: str, handler: str) -> dict:
    """Get the full dependency injection tree for a FastAPI handler."""
    with open(file) as f:
        tree = ast.parse(f.read())

    dep_map = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            deps = []
            for default in node.args.defaults:
                if isinstance(default, ast.Call):
                    func = default.func
                    if isinstance(func, ast.Name) and func.id == "Depends":
                        deps.append(default.args[0].id)
            dep_map[node.name] = deps

    def build_tree(name):
        return {
            "name": name,
            "dependencies": [build_tree(dep) for dep in dep_map.get(name, [])]
        }

    return build_tree(handler)

if __name__ == "__main__":
    mcp.run()

