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
| `list_routes` | List all FastAPI routes with method, path, and handler |
| `get_dependencies` | Get the full `Depends()` injection tree for a handler |
| `list_models` | List all Pydantic models in a file |
| `inspect_model` | Inspect a Pydantic model's fields, types, and validators |
| `find_model_usages` | Find all usages of a Pydantic model across the project |

## Installation

```bash
pip install fastapi-architect-mcp
```

## Configuration

Add this to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "fastapi-architect": {
      "command": "fastapi-architect-mcp"
    }
  }
}
```

Then restart Claude Code. You can verify it's connected by asking:

> "What MCP tools do you have access to?"

## Usage examples

Once connected, you can ask Claude Code things like:

- *"List all routes in `main.py`"*
- *"What are the dependencies of the `get_users` handler?"*
- *"Find all references to `get_db` across the project"*
- *"Rename `get_db` to `get_session` everywhere"*
- *"Inspect the `UserCreate` model in `schemas.py`"*

## Requirements

- Python 3.11+
- Claude Code (VS Code extension or CLI)

## Author

**Abdessamad Touzani**
- GitHub: [@AbdessamadTzn](https://github.com/AbdessamadTzn)
- LinkedIn: [abdessamadtouzani](https://linkedin.com/in/abdessamadtouzani)
