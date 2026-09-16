import os
from collections.abc import Iterator
from pathlib import Path

EXCLUDED_DIRS = {
    "venv", ".venv", "env", ".env", ".git", "node_modules", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", "site-packages",
}


def iter_python_files(root: str | Path) -> Iterator[Path]:
    """Yield project .py files, pruning virtualenvs, VCS and build directories during the walk."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name
