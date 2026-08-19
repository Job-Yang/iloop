"""iOS host environment discovery kept outside the platform-neutral kernel."""

from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path
from typing import Optional


def discover_developer_dir() -> Optional[str]:
    env = os.environ.get("DEVELOPER_DIR")
    if env and Path(env).exists():
        return env
    try:
        out = subprocess.run(
            ["xcode-select", "-p"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        path = out.stdout.strip()
        if (
            path
            and "CommandLineTools" not in path
            and Path(path).exists()
        ):
            return path
    except Exception:
        pass
    for app in sorted(glob.glob("/Applications/Xcode*.app")):
        developer_dir = Path(app) / "Contents" / "Developer"
        if (developer_dir / "usr" / "bin" / "xcodebuild").exists():
            return str(developer_dir)
    return None
