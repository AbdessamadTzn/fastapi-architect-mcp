"""Incremental graph loading.

Per-file extraction facts are persisted in `<project>/.fastapi-architect/graph.json`, keyed by
relative path with a (mtime, size) signature and a content hash. Only new or modified files are
re-parsed; the graph itself is rebuilt from facts (cheap) and memoized per project in memory.
"""
import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from fastapi_architect.files import iter_python_files
from fastapi_architect.graph.builder import GraphBuilder
from fastapi_architect.graph.extract import (
    AliasFacts,
    ClassFacts,
    FunctionFacts,
    IncludeFacts,
    MiddlewareFacts,
    ModuleFacts,
    ParamFacts,
    RouteFacts,
    VariableFacts,
    extract_module,
)
from fastapi_architect.graph.model import KnowledgeGraph

CACHE_DIR = ".fastapi-architect"
CACHE_FILE = "graph.json"
# bump whenever extraction output changes, to invalidate existing caches
CACHE_VERSION = 3


@dataclass
class GraphResult:
    graph: KnowledgeGraph
    reparsed: list[str] = field(default_factory=list)
    reused: int = 0
    removed: list[str] = field(default_factory=list)
    from_memory: bool = False


@dataclass
class _Entry:
    signature: tuple[int, int]  # (mtime_ns, size)
    sha1: str
    facts: ModuleFacts


_memory: dict[Path, tuple[dict[str, _Entry], KnowledgeGraph]] = {}


def load_graph(project_root: str | Path, force: bool = False, persist: bool = True) -> GraphResult:
    """Return the project's knowledge graph, re-parsing only files changed since the last call."""
    root = Path(project_root).resolve()
    if force:
        entries: dict[str, _Entry] = {}
        previous_graph = None
    elif root in _memory:
        entries, previous_graph = dict(_memory[root][0]), _memory[root][1]
    else:
        entries, previous_graph = _read_cache(root), None

    result = GraphResult(graph=KnowledgeGraph())
    current: dict[str, _Entry] = {}
    dirty = force

    for path in iter_python_files(root):
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        entry = entries.get(rel)

        if entry and entry.signature == signature:
            current[rel] = entry
            result.reused += 1
            continue

        raw = path.read_bytes()
        sha1 = hashlib.sha1(raw).hexdigest()
        if entry and entry.sha1 == sha1:  # touched but unchanged
            current[rel] = _Entry(signature, sha1, entry.facts)
            result.reused += 1
        else:
            facts = extract_module(path, root, source=raw.decode("utf-8", errors="replace"))
            current[rel] = _Entry(signature, sha1, facts)
            result.reparsed.append(rel)
        dirty = True

    result.removed = sorted(set(entries) - set(current))
    dirty = dirty or bool(result.removed)

    if not result.reparsed and not result.removed and previous_graph is not None:
        result.graph, result.from_memory = previous_graph, True
    else:
        result.graph = GraphBuilder([current[rel].facts for rel in sorted(current)]).build()

    _memory[root] = (current, result.graph)
    if dirty and persist:
        _write_cache(root, current)
    return result


def clear_memory() -> None:
    _memory.clear()


# ─── persistence ──────────────────────────────────────────────────────────────

def _read_cache(root: Path) -> dict[str, _Entry]:
    try:
        data = json.loads((root / CACHE_DIR / CACHE_FILE).read_text())
        if data.get("version") != CACHE_VERSION:
            return {}
        return {
            rel: _Entry(tuple(e["signature"]), e["sha1"], _facts_from_dict(e["facts"]))
            for rel, e in data["files"].items()
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _write_cache(root: Path, entries: dict[str, _Entry]) -> None:
    cache_dir = root / CACHE_DIR
    data = {
        "version": CACHE_VERSION,
        "files": {
            rel: {"signature": list(e.signature), "sha1": e.sha1, "facts": dataclasses.asdict(e.facts)}
            for rel, e in sorted(entries.items())
        },
    }
    try:
        cache_dir.mkdir(exist_ok=True)
        (cache_dir / ".gitignore").write_text("*\n")
        tmp = cache_dir / f"{CACHE_FILE}.tmp"
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        os.replace(tmp, cache_dir / CACHE_FILE)
    except OSError:
        pass  # read-only project: the in-memory cache still applies


def _facts_from_dict(d: dict) -> ModuleFacts:
    def functions(items: list[dict]) -> list[FunctionFacts]:
        return [
            FunctionFacts(**{
                **f,
                "params": [ParamFacts(**p) for p in f["params"]],
                "routes": [RouteFacts(**r) for r in f["routes"]],
                "sql": [tuple(s) for s in f["sql"]],
            })
            for f in items
        ]

    return ModuleFacts(**{
        **d,
        "imports": {k: tuple(v) for k, v in d["imports"].items()},
        "functions": functions(d["functions"]),
        "classes": [ClassFacts(**c) for c in d["classes"]],
        "aliases": [AliasFacts(**a) for a in d["aliases"]],
        "variables": [VariableFacts(**v) for v in d["variables"]],
        "includes": [IncludeFacts(**i) for i in d["includes"]],
        "middlewares": [MiddlewareFacts(**m) for m in d["middlewares"]],
        "sql": [tuple(s) for s in d["sql"]],
    })
