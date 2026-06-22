import pytest

from source_scout.cache import cache_delete, cache_get, cache_set, get_ttl


@pytest.fixture(autouse=True)
def clear_cache():
    for key in ["test:key1", "test:key2", "test:expired"]:
        cache_delete(key)
    yield


def test_cache_set_and_get():
    cache_set("test:key1", {"data": "hello", "num": 42}, ttl_seconds=3600)
    result = cache_get("test:key1")
    assert result is not None
    assert result["data"] == "hello"
    assert result["num"] == 42


def test_cache_miss():
    result = cache_get("test:nonexistent")
    assert result is None


def test_cache_expiry():
    cache_set("test:expired", {"data": "stale"}, ttl_seconds=0)
    result = cache_get("test:expired")
    assert result is None


def test_cache_delete():
    cache_set("test:key2", {"data": "temp"}, ttl_seconds=3600)
    assert cache_get("test:key2") is not None
    cache_delete("test:key2")
    assert cache_get("test:key2") is None


def test_cache_overwrite():
    cache_set("test:key1", {"data": "first"}, ttl_seconds=3600)
    cache_set("test:key1", {"data": "second"}, ttl_seconds=3600)
    result = cache_get("test:key1")
    assert result["data"] == "second"


def test_get_ttl_defaults():
    assert get_ttl("search") == 1800
    assert get_ttl("metadata") == 3600
    assert get_ttl("readme") == 7200
    assert get_ttl("unknown") == 3600
