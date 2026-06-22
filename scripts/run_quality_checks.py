#!/usr/bin/env python3
"""Quality check script: run source_scout's inspection against the ground-truth corpus
and generate a pass/fail report.

Usage:
    python scripts/run_quality_checks.py [--output reports/quality_<date>.json]

Requires:
    GITHUB_TOKEN env var set.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from source_scout import repo_inspector  # noqa: E402

REPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")
CORPUS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "tests", "corpus", "ground_truth.json"
)
REGRESSION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "tests", "corpus", "regression_bucket.json"
)


def _load_json(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return data["corpus"] if "corpus" in data else data


async def check_one(entry: dict) -> dict:
    owner = entry["owner"]
    repo = entry["repo"]
    exp = entry["expected"]
    result = await repo_inspector.inspect_repo(owner, repo)
    checks: dict[str, bool | str] = {}

    checks["stars_ok"] = result.stars >= exp["min_stars"]
    checks["stars_got"] = result.stars

    checks["license_ok"] = result.license_name == exp["license_spdx"]
    checks["license_got"] = result.license_name

    checks["archived_ok"] = result.archived == exp["archived"]
    checks["archived_got"] = result.archived

    checks["language_ok"] = (
        result.language is not None
        and result.language.lower() == exp["language"].lower()
    )
    checks["language_got"] = result.language

    checks["verdict_ok"] = result.verdict in exp["verdict_possible"]
    checks["verdict_got"] = result.verdict

    return {
        "repo": f"{owner}/{repo}",
        "passed": all(
            v for k, v in checks.items() if k.endswith("_ok")
        ),
        "checks": checks,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run quality checks against corpus")
    parser.add_argument(
        "--output",
        default=None,
        help="Output report path (default: reports/quality_<date>.json)",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)

    corpus = _load_json(CORPUS_PATH)
    results = []
    for entry in corpus:
        print(f"  Checking {entry['owner']}/{entry['repo']}...", end=" ")
        try:
            r = await check_one(entry)
            results.append(r)
            status = "PASS" if r["passed"] else "FAIL"
            print(status)
        except Exception as exc:
            results.append({
                "repo": f"{entry['owner']}/{entry['repo']}",
                "passed": False,
                "error": str(exc),
            })
            print(f"ERROR: {exc}")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)

    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "summary": f"{passed}/{total} checks passed",
        "results": results,
    }

    if args.output:
        report_path = args.output
    else:
        os.makedirs(REPORT_DIR, exist_ok=True)
        date_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(REPORT_DIR, f"quality_{date_str}.json")

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nReport: {report_path}")
    print(f"Summary: {report['summary']}")
    print(f"Detail: {report_path}")

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
