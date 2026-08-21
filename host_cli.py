#!/usr/bin/env python3
"""Managed iLoop host entrypoint with a durable out-of-task trust ledger."""

from __future__ import annotations

import os
import sys

import cli


def main(argv: list[str]) -> int:
    os.environ["ILOOP_MANAGED_HOST"] = "1"
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print((cli.__doc__ or "").replace("python3 -m cli", "python3 -m host_cli"))
        return 0
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
