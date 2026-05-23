"""
Shared fixtures for integration tests against a real Antigravity CLI installation.
"""
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session", autouse=True)
def require_agy():
    """Skip the entire test session if agy is not installed."""
    if not shutil.which("agy"):
        pytest.skip("Antigravity CLI (agy) not found in PATH")


@pytest.fixture
def sample_file(tmp_path):
    """Create a temporary Python file for @filename tests."""
    f = tmp_path / "sample.py"
    f.write_text("def hello():\n    return 'world'\n")
    return f


@pytest.fixture
def sample_dir(tmp_path):
    """Create a temporary directory with multiple files for --add-dir tests."""
    d = tmp_path / "project"
    d.mkdir()
    (d / "main.py").write_text("print('main')\n")
    (d / "utils.py").write_text("def add(a, b): return a + b\n")
    return d
