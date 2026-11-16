from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_HOME = REPO / "examples" / "home"


@pytest.fixture
def example_home() -> Path:
    return EXAMPLE_HOME


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A writable copy of the example home folder."""
    target = tmp_path / "home"
    shutil.copytree(EXAMPLE_HOME, target)
    return target
