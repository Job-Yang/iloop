"""命令执行框架 —— 真跑 subprocess，产出可复核证据。

插件用它执行真实命令，判成功看 success marker + 产物存在，不只看 exit code
（VDD：编译工具 exit 0 不等于成功）。runner 可注入，便于测试真跑等价命令。
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence


def discover_developer_dir() -> Optional[str]:
    """发现可用的 Xcode developer dir（自愈 xcode-select 指向 CommandLineTools 的情况）。

    顺序：DEVELOPER_DIR 环境变量 → xcode-select -p（若非 CLT）→ 扫 /Applications/Xcode*.app。
    内部版靠这套自愈跑起来；开源版不能只认全局 xcode-select，否则装了 Xcode 也会误报缺。
    """
    env = os.environ.get("DEVELOPER_DIR")
    if env and Path(env).exists():
        return env
    try:
        out = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=10)
        p = out.stdout.strip()
        if p and "CommandLineTools" not in p and Path(p).exists():
            return p
    except Exception:
        pass
    for app in sorted(glob.glob("/Applications/Xcode*.app")):
        dev = Path(app) / "Contents" / "Developer"
        if (dev / "usr" / "bin" / "xcodebuild").exists():
            return str(dev)
    return None


@dataclass
class CommandOutput:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def combined(self) -> str:
        return (self.stdout or "") + (self.stderr or "")

    def ok(self, marker: Optional[str] = None) -> bool:
        """成功判定：returncode==0，且（若给了 marker）marker 出现在输出里。"""
        if self.returncode != 0:
            return False
        if marker:
            return marker in self.combined
        return True


class CommandRunner:
    """真实执行器。子类/注入可替换（测试里跑等价的平台无关命令）。

    auto_developer_dir=True 时自动把发现的 Xcode 注入子进程 DEVELOPER_DIR，
    让 xcrun/xcodebuild/simctl 用上已装的 Xcode，而不依赖全局 xcode-select。
    """

    def __init__(self, auto_developer_dir: bool = True, *, enforce_redline: bool = True) -> None:
        self.developer_dir = discover_developer_dir() if auto_developer_dir else None
        self.enforce_redline = enforce_redline

    def run(self, argv: Sequence[str], *, timeout: float = 600.0,
            cwd: Optional[str | Path] = None, allow_dangerous: bool = False) -> CommandOutput:
        argv = [str(a) for a in argv]
        if self.enforce_redline:
            from .redline import check_command
            safe, why = check_command(argv)
            if not safe and not allow_dangerous:
                return CommandOutput(argv, 126, "", f"[redline] {why}", 0.0)
        env = None
        if self.developer_dir:
            env = dict(os.environ)
            env["DEVELOPER_DIR"] = self.developer_dir
        start = time.time()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout, cwd=str(cwd) if cwd else None, env=env,
            )
            return CommandOutput(argv, proc.returncode, proc.stdout, proc.stderr, time.time() - start)
        except FileNotFoundError:
            return CommandOutput(argv, 127, "", f"command not found: {argv[0]}", time.time() - start)
        except subprocess.TimeoutExpired:
            return CommandOutput(argv, 124, "", f"timeout after {timeout}s", time.time() - start)
