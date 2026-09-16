"""Incremental graph cache tests."""
import os
import shutil
from pathlib import Path

import pytest

from fastapi_architect.graph import load_graph
from fastapi_architect.graph.cache import CACHE_DIR, CACHE_FILE, clear_memory

FIXTURE = Path(__file__).parent / "fixtures" / "sample_app"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    clear_memory()
    dest = tmp_path / "sample_app"
    shutil.copytree(FIXTURE, dest)
    yield dest
    clear_memory()


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_first_load_parses_everything_and_writes_cache(project: Path):
    result = load_graph(project)

    assert len(result.reparsed) == 9
    assert result.reused == 0
    assert (project / CACHE_DIR / CACHE_FILE).exists()
    assert (project / CACHE_DIR / ".gitignore").read_text() == "*\n"


def test_second_load_is_served_from_memory(project: Path):
    first = load_graph(project)

    second = load_graph(project)

    assert second.reparsed == []
    assert second.from_memory
    assert second.graph is first.graph


def test_disk_cache_rebuilds_identical_graph(project: Path):
    fresh = load_graph(project).graph
    clear_memory()

    cached = load_graph(project)

    assert cached.reparsed == []
    assert cached.reused == 9
    assert cached.graph.to_dict() == fresh.to_dict()


def test_modified_file_is_reparsed_alone(project: Path):
    load_graph(project)
    users = project / "app" / "routes" / "users.py"
    users.write_text(users.read_text() + '\n\n@router.get("/me")\ndef me():\n    return None\n')
    _bump_mtime(users)

    result = load_graph(project)

    assert result.reparsed == ["app/routes/users.py"]
    assert "route:GET:app.routes.users:me" in result.graph


def test_touched_but_unchanged_file_is_not_reparsed(project: Path):
    load_graph(project)
    _bump_mtime(project / "app" / "crud.py")

    assert load_graph(project).reparsed == []


def test_removed_file_disappears_from_graph(project: Path):
    load_graph(project)
    (project / "app" / "crud.py").unlink()

    result = load_graph(project)

    assert result.removed == ["app/crud.py"]
    assert "app.crud:get_users" not in result.graph


def test_force_reparses_everything(project: Path):
    load_graph(project)

    assert len(load_graph(project, force=True).reparsed) == 9


def test_corrupt_cache_is_ignored(project: Path):
    load_graph(project)
    clear_memory()
    (project / CACHE_DIR / CACHE_FILE).write_text("{not json")

    assert len(load_graph(project).reparsed) == 9
