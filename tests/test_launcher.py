import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_windows_launcher_works_outside_repository(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    launcher = project_root / "source-scout.cmd"

    result = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(launcher), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "explore-local" in result.stdout
