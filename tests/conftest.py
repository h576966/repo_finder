import hashlib
import shutil
from pathlib import Path

import pytest

from source_scout import catalog


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    test_id = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    test_home = Path.cwd() / ".source_scout" / "test-catalogs" / test_id
    shutil.rmtree(test_home, ignore_errors=True)
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(test_home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    catalog.reset_connection()
    yield
    catalog.reset_connection()
    shutil.rmtree(test_home, ignore_errors=True)
