import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_app"


@pytest.fixture
def sample_app(tmp_path: Path) -> Path:
    """A fresh copy of the sample FastAPI project (tools like rename_symbol write to disk)."""
    dest = tmp_path / "sample_app"
    shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture
def app_dir(sample_app: Path) -> Path:
    return sample_app / "app"
