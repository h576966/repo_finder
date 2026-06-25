from typing import Any

from .fastcontext_constants import MAX_GREP_RESULTS, MAX_READ_LINES


def fastcontext_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read a UTF-8 text file under the workspace root by line range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the workspace root. Do not shorten it.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "1-based start line. Defaults to 1.",
                            "minimum": 1,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum lines to read.",
                            "minimum": 1,
                            "maximum": MAX_READ_LINES,
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Glob",
                "description": "List files under the workspace root using ripgrep-style glob patterns.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Directory relative to the workspace root. Defaults to '.'.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern such as '**/*.ts' or 'src/**/*.tsx'.",
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Grep",
                "description": "Search text under the workspace root with ripgrep-compatible options.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "Directory or file path relative to the workspace root.",
                        },
                        "glob": {"type": "string"},
                        "output_mode": {
                            "type": "string",
                            "enum": ["content", "files", "files_with_matches", "count"],
                        },
                        "-A": {"type": "integer", "minimum": 0, "maximum": 20},
                        "-B": {"type": "integer", "minimum": 0, "maximum": 20},
                        "-C": {"type": "integer", "minimum": 0, "maximum": 20},
                        "-n": {"type": "boolean"},
                        "-i": {"type": "boolean"},
                        "type": {"type": "string"},
                        "head_limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_GREP_RESULTS,
                        },
                        "multiline": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
    ]
