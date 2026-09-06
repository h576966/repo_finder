import json

import httpx
import pytest

from source_scout import fastcontext, fastcontext_routing
from source_scout.investigation_anchors import InvestigationAnchor, anchor_seed, parse_cli_anchor
from tests.fastcontext_helpers import _response_message_json


@pytest.mark.parametrize(
    "anchor",
    [
        InvestigationAnchor("../other.py"),
        InvestigationAnchor(".env"),
        InvestigationAnchor("source.py", 0),
        InvestigationAnchor("source.py", 1, 999),
        InvestigationAnchor("source.py", symbol="nonexistent"),
    ],
)
def test_invalid_anchors_cannot_expand_source_scope(tmp_path, anchor):
    (tmp_path / "source.py").write_text("def handler(): return 1\n")
    with pytest.raises(fastcontext.FastContextError):
        anchor_seed(tmp_path, [anchor])


@pytest.mark.asyncio
async def test_anchors_skip_broad_seed_but_do_not_count_as_observed_source(monkeypatch, tmp_path):
    (tmp_path / "source.py").write_text("def handler(): return 1\n")
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "on")

    def forbidden(*args):
        pytest.fail("Anchored investigation must not rebuild the broad seed")

    monkeypatch.setattr(fastcontext_routing, "_local_seed_context", forbidden)

    def respond(request):
        payload = request.content.decode()
        assert "start_anchors" in payload and "handler" in payload
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps({"evidence_paths": ["source.py:1-1"], "notes": ["unread anchor"]})
            ),
        )

    result = await fastcontext.explore_local_project(
        "trace runtime registration",
        tmp_path,
        max_turns=1,
        reason="Local reads could not establish the runtime registration mapping",
        anchors=[InvestigationAnchor("source.py", 1, 1, "handler")],
        transport=httpx.MockTransport(respond),
    )
    assert result.status == "incomplete" and not result.evidence_paths
    assert result.missing_context
    assert parse_cli_anchor("source.py:1-1") == InvestigationAnchor("source.py", 1, 1)
