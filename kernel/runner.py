"""命令执行框架 —— 真跑 subprocess，产出可复核证据。

插件用它执行真实命令，判成功看 success marker + 产物存在，不只看 exit code
（VDD：编译工具 exit 0 不等于成功）。runner 可注入，便于测试真跑等价命令。
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence


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

    平台插件可通过 environment_overrides 注入宿主环境；内核不发现具体 SDK。
    """

    def __init__(
        self,
        auto_developer_dir: Optional[bool] = None,
        *,
        environment_overrides: Optional[Mapping[str, str]] = None,
        enforce_redline: bool = True,
    ) -> None:
        self.environment_overrides = dict(environment_overrides or {})
        self.developer_dir = self.environment_overrides.get(
            "DEVELOPER_DIR", ""
        )
        if auto_developer_dir is not None:
            warnings.warn(
                "auto_developer_dir is deprecated; inject DEVELOPER_DIR via "
                "environment_overrides from the platform adapter",
                DeprecationWarning,
                stacklevel=2,
            )
            if auto_developer_dir and not self.developer_dir:
                from plugins.ios_native.environment import (
                    discover_developer_dir,
                )
                self.developer_dir = discover_developer_dir() or ""
                if self.developer_dir:
                    self.environment_overrides["DEVELOPER_DIR"] = (
                        self.developer_dir
                    )
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
        if self.environment_overrides:
            env = dict(os.environ)
            env.update(self.environment_overrides)
        start = time.time()
        proc = None
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process_options = (
                    {
                        "creationflags": getattr(
                            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                        ),
                    }
                    if os.name == "nt"
                    else {"start_new_session": True}
                )
                proc = subprocess.Popen(
                    argv,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=str(cwd) if cwd else None,
                    env=env,
                    **process_options,
                )
                proc.wait(timeout=timeout)
                stdout = self._read_capture(stdout_file)
                stderr = self._read_capture(stderr_file)
                return CommandOutput(
                    argv, proc.returncode, stdout, stderr, time.time() - start
                )
            except FileNotFoundError:
                return CommandOutput(
                    argv, 127, "", f"command not found: {argv[0]}",
                    time.time() - start,
                )
            except subprocess.TimeoutExpired:
                if proc is not None:
                    self._stop_process_group(proc)
                stdout = self._read_capture(stdout_file)
                stderr = self._read_capture(stderr_file)
                message = f"timeout after {timeout}s"
                stderr = f"{stderr}\n{message}".strip()
                return CommandOutput(
                    argv, 124, stdout, stderr, time.time() - start
                )
            except BaseException:
                if proc is not None:
                    self._stop_process_group(proc)
                raise

    @staticmethod
    def _read_capture(file) -> str:
        file.flush()
        file.seek(0)
        return file.read().decode("utf-8", errors="replace")

    @staticmethod
    def _stop_process_group(proc: subprocess.Popen) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    [
                        "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            CommandRunner._wait_ignoring_interrupt(proc, 5)
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        CommandRunner._wait_ignoring_interrupt(proc, 5)
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        if proc.poll() is None:
            CommandRunner._wait_ignoring_interrupt(proc, 5)

    @staticmethod
    def _wait_ignoring_interrupt(
        proc: subprocess.Popen,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                proc.wait(timeout=remaining)
            except KeyboardInterrupt:
                continue
            except subprocess.TimeoutExpired:
                return False
        return True
