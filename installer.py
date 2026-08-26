#!/usr/bin/env python3
"""Atomic public installer and host registration for iLoop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional


CommandRunner = Callable[
    [list[str], Optional[Path], float],
    subprocess.CompletedProcess,
]


def _run(
    argv: list[str],
    cwd: Optional[Path] = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Installer:
    """Install a clean public revision, then atomically register host entries."""

    def __init__(
        self,
        *,
        home: str | Path,
        runner: CommandRunner = _run,
        smoke_runner: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / ".iloop"
        self.install_root = self.root / "iloop"
        self.runner = runner
        self.smoke_runner = smoke_runner or self._smoke

    def install(
        self,
        source: str | Path,
        *,
        branch: str = "main",
    ) -> dict:
        origin = self._origin(source)
        self.root.mkdir(parents=True, exist_ok=True)
        candidate = (
            self.root / "tmp"
            / f"install-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        result = self.runner(
            [
                "git", "clone", "--branch", branch, "--single-branch",
                origin, str(candidate),
            ],
            None,
            300,
        )
        if result.returncode:
            raise RuntimeError(
                (result.stderr or result.stdout or "git clone failed").strip()
            )
        self.smoke_runner(candidate)
        commit = self._command(
            ["git", "-C", str(candidate), "rev-parse", "HEAD"]
        ).stdout.strip()
        backup = None
        if self.install_root.exists():
            backup = (
                self.root / "backups"
                / f"iloop-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.install_root, backup)
        try:
            os.replace(candidate, self.install_root)
            hosts = self.register_hosts()
            state = {
                "status": "ready",
                "install_root": str(self.install_root),
                "origin": origin,
                "branch": branch,
                "commit": commit,
                "hosts": hosts,
                "installed_at": time.time(),
                "backup": str(backup) if backup else "",
            }
            self._write_state(state)
            return state
        except BaseException:
            if self.install_root.exists():
                failed = self.root / "tmp" / (
                    f"failed-{int(time.time())}-{uuid.uuid4().hex[:8]}"
                )
                os.replace(self.install_root, failed)
            if backup is not None and backup.exists():
                os.replace(backup, self.install_root)
                self.register_hosts()
            raise

    def update(self) -> dict:
        if not self.install_root.is_dir():
            raise RuntimeError("iLoop is not installed")
        origin = self._command([
            "git", "-C", str(self.install_root),
            "remote", "get-url", "origin",
        ]).stdout.strip()
        branch = self._command([
            "git", "-C", str(self.install_root),
            "branch", "--show-current",
        ]).stdout.strip() or "main"
        return self.install(origin, branch=branch)

    def register_hosts(self) -> dict:
        prompt = self.install_root / "AGENT_PROMPT.md"
        if not prompt.is_file():
            raise RuntimeError("installed AGENT_PROMPT.md is missing")
        body = prompt.read_text(encoding="utf-8")
        payloads = self._host_payloads(body)
        for path, content in payloads.values():
            _atomic_text(path, content)
        return {
            name: str(path)
            for name, (path, _) in payloads.items()
        }

    def status(self) -> dict:
        state_path = self.root / "install-state.json"
        if not state_path.is_file() or not self.install_root.is_dir():
            return {"status": "missing", "ready": False}
        state = json.loads(state_path.read_text(encoding="utf-8"))
        prompt = self.install_root / "AGENT_PROMPT.md"
        expected = _sha256(prompt) if prompt.is_file() else ""
        payloads = self._host_payloads(
            prompt.read_text(encoding="utf-8")
            if prompt.is_file() else ""
        )
        host_state = {}
        for name, path_value in dict(state.get("hosts", {})).items():
            path = Path(path_value)
            expected_payload = payloads.get(name)
            host_state[name] = bool(
                expected_payload
                and path == expected_payload[0]
                and path.is_file()
                and path.read_text(encoding="utf-8")
                == expected_payload[1]
            )
        try:
            actual_commit = self._command([
                "git", "-C", str(self.install_root),
                "rev-parse", "HEAD",
            ]).stdout.strip()
            actual_origin = self._command([
                "git", "-C", str(self.install_root),
                "remote", "get-url", "origin",
            ]).stdout.strip()
            actual_branch = self._command([
                "git", "-C", str(self.install_root),
                "branch", "--show-current",
            ]).stdout.strip()
            repository_ready = (
                actual_commit == state.get("commit")
                and actual_origin == state.get("origin")
                and actual_branch == state.get("branch")
            )
        except RuntimeError:
            actual_commit = actual_origin = actual_branch = ""
            repository_ready = False
        ready = bool(
            expected
            and repository_ready
            and host_state
            and all(host_state.values())
        )
        return {
            **state,
            "status": "ready" if ready else "blocked",
            "ready": ready,
            "host_state": host_state,
            "repository_state": {
                "ready": repository_ready,
                "commit": actual_commit,
                "origin": actual_origin,
                "branch": actual_branch,
            },
        }

    def _host_payloads(
        self, body: str
    ) -> dict[str, tuple[Path, str]]:
        return {
            "generic": (
                self.home / ".agents" / "iloop" / "AGENT_PROMPT.md",
                body,
            ),
            "claude": (
                self.home / ".claude" / "agents" / "iloop.md",
                "---\n"
                "name: iloop\n"
                "description: Evidence-driven engineering loop\n"
                "---\n\n"
                + body,
            ),
            "codex": (
                self.home / ".codex" / "agents" / "iloop.toml",
                'name = "iloop"\n'
                'description = "Evidence-driven engineering loop"\n'
                'sandbox_mode = "workspace-write"\n'
                f"developer_instructions = "
                f"{json.dumps(body, ensure_ascii=False)}\n",
            ),
        }

    def _origin(self, source: str | Path) -> str:
        source_path = Path(str(source)).expanduser()
        if source_path.is_dir() and (source_path / ".git").exists():
            result = self.runner(
                [
                    "git", "-C", str(source_path.resolve()),
                    "remote", "get-url", "origin",
                ],
                None,
                30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return str(source_path.resolve())
        return str(source)

    def _command(
        self,
        argv: list[str],
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess:
        result = self.runner(argv, None, timeout)
        if result.returncode:
            raise RuntimeError(
                (result.stderr or result.stdout or "command failed").strip()
            )
        return result

    @staticmethod
    def _smoke(root: Path) -> None:
        for argv in (
            [sys.executable, "-m", "host_cli", "selftest"],
            [sys.executable, "scripts/fresh_clone_smoke.py"],
        ):
            result = subprocess.run(
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if result.returncode:
                raise RuntimeError(
                    f"candidate smoke failed: {' '.join(argv)}\n"
                    f"{result.stdout}\n{result.stderr}"
                )

    def _write_state(self, state: dict) -> None:
        path = self.root / "install-state.json"
        _atomic_text(
            path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )
        os.chmod(path, 0o600)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("install", "update", "status")
    )
    parser.add_argument("--source", default=str(Path(__file__).parent))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args(argv)
    installer = Installer(home=args.home)
    if args.command == "install":
        value = installer.install(args.source, branch=args.branch)
    elif args.command == "update":
        value = installer.update()
    else:
        value = installer.status()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("status") != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
