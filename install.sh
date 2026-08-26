#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
command -v git >/dev/null 2>&1 || {
  printf '%s\n' "iLoop requires git." >&2
  exit 2
}
command -v python3 >/dev/null 2>&1 || {
  printf '%s\n' "iLoop requires Python 3.9 or newer." >&2
  exit 2
}
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 2)' || {
  printf '%s\n' "iLoop requires Python 3.9 or newer." >&2
  exit 2
}

exec python3 "$ROOT/installer.py" install --source "$ROOT"
