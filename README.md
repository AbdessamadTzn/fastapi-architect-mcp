# fastapi-architect-mcp

An MCP server that gives Claude Code IDE-level intelligence for FastAPI projects — semantic code navigation, safe renaming, route inspection, dependency trees, and Pydantic model analysis.

Instead of Claude reading files blindly, it calls structured tools backed by [Jedi](https://jedi.readthedocs.io/) (Python language server) and Python's AST.

## Tools

| Tool | Description |
|---|---|
| `find_references` | Find all usages of a symbol across the project |
| `rename_symbol` | Safely rename a symbol across all files |
| `go_to_definition` | Jump to where a symbol is defined |
| `get_completions` | Get completion suggestions at a cursor position |
| `list_routes` | List all FastAPI routes across the entire project |
| `get_dependencies` | Get the full `Depends()` injection tree for a handler |
| `build_dependency_graph` | Full graph: route → input models → dependencies → response model |
| `validate_response_models` | Detect routes missing a `response_model` declaration |
| `list_models` | List all Pydantic/SQLModel models in a file |
| `inspect_model` | Inspect a model's fields, types, defaults, and validators |
| `find_model_usages` | Find everywhere a model is used across the project |
| `detect_schema_orm_mismatches` | Detect field mismatches between ORM model and Pydantic schema |

## Installation

```bash
pip install fastapi-architect-mcp
```

## Configuration

### Option 1 — Global (available in all projects)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "fastapi-architect": {
      "command": "fastapi-architect-mcp"
    }
  }
}
```

### Option 2 — Per project (recommended for best results)

Create `.mcp.json` at your project root:

```json
{
  "mcpServers": {
    "fastapi-architect": {
      "command": "/path/to/your/project/venv/bin/fastapi-architect-mcp"
    }
  }
}
```

Using your project's own venv gives Jedi access to all your project's dependencies, which enables full cross-file `find_references` and `rename_symbol` support.

Then restart Claude Code.

## Usage examples

Once connected, you can ask Claude Code things like:

- *"List all routes in this project"*
- *"What are the dependencies of the `get_users` handler?"*
- *"Find all references to `get_db` across the project"*
- *"Rename `get_db` to `get_session` everywhere"*
- *"Inspect the `UserCreate` model"*
- *"Are there any routes missing a response_model?"*
- *"Show me the full dependency graph for the users router"*
- *"Are there any mismatches between my `User` ORM model and `UserPublic` schema?"*

## Supported patterns

### Dependency injection
- `def handler(x=Depends(func))` — standard FastAPI
- `def handler(*, x=Depends(func))` — keyword-only args
- `@router.get("/", dependencies=[Depends(func)])` — decorator-level deps
- `SessionDep = Annotated[Session, Depends(get_db)]` — Annotated aliases (modern FastAPI)

### Models
- `BaseModel` — Pydantic
- `SQLModel` — SQLModel
- `BaseSettings` — Pydantic settings
- `RootModel` — Pydantic v2
- Inherited models (e.g. `class UserCreate(UserBase)`)

### Validators
- `@validator` — Pydantic v1
- `@field_validator` — Pydantic v2

## Known limitations

- **`find_references` and `rename_symbol`** work cross-file only when the MCP is configured to use the project's own venv (Option 2 above). With a global install, they only operate on the file where the symbol is defined.
- **`get_dependencies`** supports native FastAPI `Depends()` only. Other DI frameworks (Dishka, dependency-injector, etc.) are not supported.
- **Runtime dependencies** (middleware, lifespan events, startup hooks) are not visible to static analysis and won't appear in dependency trees.
- **`get_completions`** is most useful at attribute access positions (e.g. after a `.`). On empty lines it returns all Python builtins.

## Requirements

- Python 3.11+
- Claude Code (VS Code extension or CLI)

## Author

**Abdessamad Touzani**
- GitHub: [@AbdessamadTzn](https://github.com/AbdessamadTzn)
- LinkedIn: [abdessamadtouzani](https://linkedin.com/in/abdessamadtouzani)
