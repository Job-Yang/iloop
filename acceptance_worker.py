#!/usr/bin/env python3
"""Read-only subprocess used by the managed host for local acceptance."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main(package_path: str, result_path: str) -> int:
    package = json.loads(Path(package_path).read_text(encoding="utf-8"))["package"]
    observed = [
        item for item in package.get("evidence", [])
        if item.get("kind") == "observed"
        and item.get("outcome") == "success"
        and item.get("path")
        and Path(item["path"]).exists()
    ]
    haystack = " ".join(str(item.get("summary", "")) for item in observed)
    uncovered = [
        criterion for criterion in package.get("criteria", [])
        if criterion and criterion.split()[0] not in haystack
    ]
    verdict = "needs_more_context"
    if not observed:
        reasons = ["no successful observed evidence"]
    elif uncovered:
        reasons = [f"criterion lacks observed evidence: {item}" for item in uncovered]
    else:
        reasons = [
            "local preflight found evidence coverage; an external independent "
            "reviewer must issue the final verdict"
        ]
    result = {
        "package_id": package["package_id"],
        "case_id": package["case_id"],
        "review_token": package["review_token"],
        "subject_fingerprint": package.get("subject_fingerprint", ""),
        "reviewer": "iloop-acceptance-worker",
        "verdict": verdict,
        "reasons": reasons,
        "reviewed_at": time.time(),
        "expires_at": min(
            float(package["expires_at"]),
            time.time() + 1800,
        ),
    }
    Path(result_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 -m acceptance_worker <package.json> <result.json>")
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
