"""Install the local plugin, preserving config and retiring only two old skills.

Run from the Source Scout venv. Backups live outside active skill roots. Re-running
updates the plugin through Codex's supported personal-marketplace helpers.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_SKILLS = ("fastcontext-local", "source-scout-reuse-flow")
OLD_SERVERS = ("source_scout", "source_scout_references")


def without_old_servers(text: str) -> str:
    """Preserve every unrelated TOML section, comment and setting verbatim."""
    result = []
    skip = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            match = re.match(r"\s*\[mcp_servers\.([A-Za-z0-9_]+)(?:\.|\])", line)
            skip = bool(match and match[1] in OLD_SERVERS)
        if not skip:
            result.append(line)
    return "".join(result)


def install() -> None:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Install Codex with plugin support first.")
    user_dir = Path.home()
    config = user_dir / ".codex/config.toml"
    original = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    parsed = tomllib.loads(original)
    old_env = parsed.get("mcp_servers", {}).get("source_scout", {}).get("env", {})
    collection = old_env.get("SOURCE_SCOUT_HOME")
    marketplace = user_dir / ".agents/plugins/marketplace.json"
    # Codex 0.153 resolves personal local sources from the user directory.
    destination = user_dir / "plugins/source-scout"
    helpers = user_dir / ".codex/skills/.system/plugin-creator/scripts"
    required = (
        "create_basic_plugin.py",
        "validate_plugin.py",
        "read_marketplace_name.py",
        "update_plugin_cachebuster.py",
    )
    if any(not (helpers / name).is_file() for name in required):
        raise RuntimeError("Codex plugin-creator helpers are required for personal marketplace installation.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = user_dir / ".codex/backups" / ("source-scout-" + stamp)
    backup.mkdir(parents=True)
    if config.exists():
        shutil.copy2(config, backup / "config.toml")
    if marketplace.exists():
        shutil.copy2(marketplace, backup / "marketplace.json")
    if destination.exists():
        shutil.copytree(destination, backup / "plugin")
        if not collection:
            previous = json.loads((destination / ".mcp.json").read_text())
            collection = (
                previous.get("mcpServers", previous)
                .get("source_scout", {})
                .get("env", {})
                .get("SOURCE_SCOUT_HOME")
            )

    subprocess.run(
        [
            sys.executable,
            str(helpers / "create_basic_plugin.py"),
            "source-scout",
            "--path",
            str(destination.parent),
            "--with-skills",
            "--with-mcp",
            "--with-marketplace",
            "--marketplace-path",
            str(marketplace),
            "--force",
        ],
        check=True,
    )
    shutil.copytree(ROOT / "plugins/source-scout", destination, dirs_exist_ok=True)
    mcp_path = destination / ".mcp.json"
    mcp = json.loads(mcp_path.read_text())
    settings = mcp["mcpServers"]["source_scout"]
    settings.update(
        command=sys.executable,
        args=["-m", "source_scout", "serve-mcp"],
        env={"PYTHONPATH": str(ROOT / "src")},
    )
    if collection:
        settings["env"]["SOURCE_SCOUT_HOME"] = collection
    # No target root or remote-policy override belongs in a global plugin.
    mcp_path.write_text(json.dumps(mcp, indent=2), encoding="utf-8")
    subprocess.run(
        [sys.executable, str(helpers / "update_plugin_cachebuster.py"), str(destination)], check=True
    )
    subprocess.run([sys.executable, str(helpers / "validate_plugin.py"), str(destination)], check=True)
    name = subprocess.check_output(
        [sys.executable, str(helpers / "read_marketplace_name.py"), "--marketplace-path", str(marketplace)],
        text=True,
    ).strip()
    # Installation success precedes retirement of existing capabilities.
    subprocess.run([codex, "plugin", "add", "source-scout@" + name, "--json"], check=True)
    current = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    config.write_text(without_old_servers(current), encoding="utf-8")
    moved = []
    for root_name in (".codex", ".agents"):
        skill_root = (user_dir / root_name / "skills").resolve()
        for name in OLD_SKILLS:
            source = skill_root / name
            if source.exists():
                if source.is_symlink() or source.resolve().parent != skill_root:
                    raise RuntimeError("Refusing to move an unexpected skill path: " + str(source))
                target = backup / (root_name[1:] + "-" + name)
                source.rename(target)
                moved.append({"from": str(source), "to": str(target)})
    (backup / "migration.json").write_text(
        json.dumps(
            {
                "backup": str(backup),
                "moved_skills": moved,
                "collection_preserved": collection,
                "source_checkout": str(ROOT),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Backup and rollback inventory: " + str(backup))


if __name__ == "__main__":
    install()
