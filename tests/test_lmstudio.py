import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from repo_finder import catalog, lmstudio, pipeline, profiler


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_FINDER_HOME", str(tmp_path / ".repo_finder"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def test_lmstudio_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("LM_STUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("REPO_FINDER_GEMMA_MODEL", raising=False)
    monkeypatch.delenv("REPO_FINDER_FASTCONTEXT_MODEL", raising=False)
    monkeypatch.delenv("REPO_FINDER_LMSTUDIO_TIMEOUT", raising=False)

    config = lmstudio.get_config()
    assert config.base_url == "http://127.0.0.1:1234/v1"
    assert config.gemma_model == "google/gemma-4-12b-qat"
    assert config.fastcontext_model == "fastcontext-1.0-4b-rl"
    assert config.timeout_seconds == 30.0


def test_lmstudio_config_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://localhost:9999/v1/")
    monkeypatch.setenv("REPO_FINDER_GEMMA_MODEL", "gemma-local")
    monkeypatch.setenv("REPO_FINDER_FASTCONTEXT_MODEL", "fastcontext-local")
    monkeypatch.setenv("REPO_FINDER_LMSTUDIO_TIMEOUT", "7")

    config = lmstudio.get_config()
    assert config.base_url == "http://localhost:9999/v1"
    assert config.gemma_model == "gemma-local"
    assert config.fastcontext_model == "fastcontext-local"
    assert config.timeout_seconds == 7.0


@pytest.mark.asyncio
async def test_list_models_parses_openai_compatible_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})

    transport = httpx.MockTransport(handler)
    models = await lmstudio.list_models(transport=transport)
    assert models == ["model-a", "model-b"]


@pytest.mark.asyncio
async def test_chat_json_posts_chat_completion_and_parses_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "gemma"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '```json\n{"ok": true}\n```'}},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    result = await lmstudio.chat_json(
        "gemma",
        [{"role": "user", "content": "return json"}],
        transport=transport,
    )
    assert result == {"ok": True}


def test_parse_json_content_handles_embedded_json() -> None:
    assert lmstudio.parse_json_content('Here is JSON: {"ok": true}') == {"ok": True}


def _write_card_fixture(root: Path) -> None:
    (root / "app").mkdir()
    (root / "app" / "page.tsx").write_text("export default function Page() { return <main /> }")
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15", "react": "19"}}),
        encoding="utf-8",
    )


def _create_repository_card(tmp_path: Path) -> str:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    _write_card_fixture(snapshot_root)
    repo_id = catalog.upsert_repository(
        {
            "owner": {"login": "owner"},
            "name": "repo",
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "private": False,
            "archived": False,
            "language": "TypeScript",
            "topics": ["nextjs"],
        },
        "test",
    )
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    return catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(snapshot_root))


@pytest.mark.asyncio
async def test_profile_repository_cards_stores_gemma_profile(tmp_path, monkeypatch) -> None:
    card_id = _create_repository_card(tmp_path)

    async def fake_validate_models(config: lmstudio.LMStudioConfig) -> dict[str, Any]:
        return {
            "models": [config.gemma_model],
            "gemma_available": True,
            "fastcontext_available": False,
        }

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "repository_type": "reference_application",
            "capabilities": [{"name": "dashboard", "confidence": 0.8, "evidence": ["app/page.tsx"]}],
            "likely_usefulness": 0.7,
            "extractability": 0.6,
            "maintenance_quality": 0.5,
            "needs_fastcontext": True,
            "concerns": [],
        }

    monkeypatch.setattr(profiler.lmstudio, "validate_models", fake_validate_models)
    monkeypatch.setattr(profiler.lmstudio, "chat_json", fake_chat_json)

    result = await profiler.profile_repository_cards(limit=5)
    assert result == {"profiled_cards": 1, "failed_cards": 0, "available_cards": 1}

    row = catalog.get_connection().execute(
        "SELECT gemma_profile FROM repository_cards WHERE card_id = ?",
        [card_id],
    ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert stored["schema_version"] == profiler.PROFILE_SCHEMA_VERSION
    assert stored["repository_type"] == "reference_application"

    runs = catalog.get_connection().execute(
        "SELECT stage_name, status, model_id FROM analysis_runs WHERE stage_name = 'profile'"
    ).fetchall()
    assert runs == [("profile", "completed", lmstudio.DEFAULT_GEMMA_MODEL)]


def test_lmstudio_status_cli_prints_json(monkeypatch, capsys) -> None:
    import repo_finder.__main__ as main_module

    async def fake_status(start_server: bool, smoke_test: bool) -> dict[str, object]:
        assert start_server is True
        assert smoke_test is True
        return {"reachable": True}

    monkeypatch.setattr(main_module, "_lmstudio_status", fake_status)
    monkeypatch.setattr(sys, "argv", ["repo-finder", "lmstudio-status", "--start-server", "--smoke-test"])
    main_module.main()
    captured = capsys.readouterr()
    assert '"reachable": true' in captured.out


def test_profile_cli_invokes_profiler(monkeypatch, capsys) -> None:
    import repo_finder.__main__ as main_module

    async def fake_profile(limit: int, force: bool = False) -> dict[str, int]:
        assert limit == 2
        assert force is True
        return {"profiled_cards": 2}

    monkeypatch.setattr(profiler, "profile_repository_cards", fake_profile)
    monkeypatch.setattr(sys, "argv", ["repo-finder", "profile", "--limit", "2", "--force"])
    main_module.main()
    captured = capsys.readouterr()
    assert "{'profiled_cards': 2}" in captured.out
