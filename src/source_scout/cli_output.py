from __future__ import annotations


def _format_local_explore_text(result: object) -> str:
    evidence_paths = getattr(result, "evidence_paths")
    notes = getattr(result, "notes")
    lines = [
        f"Task: {getattr(result, 'task')}",
        f"Project: {getattr(result, 'project_path')}",
        f"Status: {getattr(result, 'status')}",
        "",
        "Citations:",
    ]
    lines.extend(f"- {path}" for path in evidence_paths)
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)
