import json
import shutil
from pathlib import Path

import git
import pytest

from source_scout import implementation_references

pytestmark = pytest.mark.usefixtures("isolated_catalog")


@pytest.mark.asyncio
async def test_reference_quality_golden_suite(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "reference_quality_repo"
    source_root = tmp_path / "reference-quality"
    shutil.copytree(fixture_root, source_root)
    repo = git.Repo.init(source_root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Source Scout Eval")
        writer.set_value("user", "email", "source-scout@example.invalid")
    repo.git.add(".")
    repo.index.commit("fixed reference quality corpus")
    repo.close()

    added = await implementation_references.add_reference_source(source_root, selection_kind="curated")
    assert added["reference_count"] == 7

    suite_path = Path(__file__).parents[1] / "evals" / "golden" / "references_v1.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    assert suite["suite_id"] == "implementation-references-v1"

    for case in suite["tasks"]:
        result = implementation_references.find_implementation_references(case["task"])
        expected_path = case.get("expected_path")
        if expected_path:
            assert result.status == "matches", case["id"]
            assert result.results[0].path == expected_path, case["id"]
            context = implementation_references.get_implementation_reference(
                result.results[0].reference_id,
                task=case["task"],
            )
            assert case["expected_text"] in "\n".join(
                snippet.content for snippet in context.snippets
            ), case["id"]
            assert context.missing_evidence == [], case["id"]
        else:
            assert result.status == case["expected_status"], case["id"]
            assert result.results == [], case["id"]
