"""
Shared fixtures for the test suite.

Only the tests that shell out to a real Antigravity CLI need `agy` on PATH.
The pure unit tests (security, conversation metadata, parsers, validation)
must stay runnable without it — an earlier session-wide skip here meant that
when agy was absent *nothing* ran, which let upstream drift go uncaught.
Modules that need the real CLI opt in with the `requires_agy` fixture.
"""
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

AGY_AVAILABLE = shutil.which("agy") is not None


@pytest.fixture(scope="session")
def requires_agy():
    """Skip a test (or module, via an autouse alias) when agy is not installed."""
    if not AGY_AVAILABLE:
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
