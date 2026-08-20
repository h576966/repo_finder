import pytest

from source_scout import catalog


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    catalog.reset_connection()
    yield
    catalog.reset_connection()
