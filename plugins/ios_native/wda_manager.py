"""Managed, pinned WebDriverAgent lifecycle for public real-device automation."""

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path

from .wda_client import WDAClient

WDA_REPOSITORY = "https://github.com/appium/WebDriverAgent.git"
WDA_VERSION = "v16.1.1"
WDA_COMMIT = "1b1bcef84dda62a9638e84ece31277bc21ee9fba"


class WDAManager:
    def __init__(self, root: str | Path, *, device_udid: str = "", team_id: str = "",
                 xcodebuildmcp: str = "", local_port: int = 8100) -> None:
        self.root = Path(root)
        self.source = self.root / WDA_VERSION
        self.state_path = self.root / "runtime.json"
        self.device_udid = device_udid
        self.team_id = team_id
        self.xcodebuildmcp = xcodebuildmcp or shutil.which("xcodebuildmcp") or ""
        self.local_port = local_port
        self.client = WDAClient(f"http://127.0.0.1:{local_port}", timeout=1.0)

    def status(self) -> dict:
        state = self._read_state()
        try:
            value = self.client.status().get("value", {})
            endpoint_ready = bool(value.get("ready"))
        except Exception:
            endpoint_ready = False
            value = {}
        process_ready = (
            self._pid_matches(
                int(state.get("wda_pid") or 0),
                ("xcodebuildmcp", str(self.source / "WebDriverAgent.xcodeproj"),
                 self.device_udid),
                str(state.get("wda_identity_sha256", "")),
            )
            and self._pid_matches(
                int(state.get("proxy_pid") or 0),
                ("iproxy", str(self.local_port), "8100", "-u", self.device_udid),
                str(state.get("proxy_identity_sha256", "")),
            )
            and self._port_owner_pid(self.local_port) == int(state.get("proxy_pid") or 0)
        )
        source_identity = self._source_identity()
        subject_ready = (
            state.get("device_udid") == self.device_udid
            and state.get("version") == WDA_VERSION
            and state.get("local_port") == self.local_port
            and source_identity.get("tag") == WDA_VERSION
            and source_identity.get("commit") == WDA_COMMIT
            and source_identity.get("origin") == WDA_REPOSITORY
            and source_identity.get("worktree_valid") is True
            and state.get("source_commit") == WDA_COMMIT
        )
        ready = endpoint_ready and process_ready and subject_ready
        return {"ready": ready, "wda": value, "runtime": state}

    def install_source(self) -> Path:
        project = self.source / "WebDriverAgent.xcodeproj"
        if project.exists():
            identity = self._source_identity()
            if (
                identity.get("tag") != WDA_VERSION
                or identity.get("commit") != WDA_COMMIT
                or identity.get("origin") != WDA_REPOSITORY
                or identity.get("worktree_valid") is not True
            ):
                raise RuntimeError("existing WDA source does not match pinned upstream commit")
            return project
        self.root.mkdir(parents=True, exist_ok=True)
        if self.source.exists() and any(self.source.iterdir()):
            raise RuntimeError(f"WDA source directory is not empty: {self.source}")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", WDA_VERSION,
             WDA_REPOSITORY, str(self.source)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"cannot clone WDA {WDA_VERSION}: {result.stderr.strip()}")
        identity = self._source_identity()
        if (
            identity.get("tag") != WDA_VERSION
            or identity.get("commit") != WDA_COMMIT
            or identity.get("origin") != WDA_REPOSITORY
            or identity.get("worktree_valid") is not True
        ):
            raise RuntimeError("cloned WDA source does not match pinned upstream commit")
        return project

    def command(self) -> list[str]:
        if not self.xcodebuildmcp:
            raise RuntimeError("xcodebuildmcp is required")
        if not self.device_udid:
            raise RuntimeError("device_udid is required")
        if not self.team_id:
            raise RuntimeError("team_id is required for WDA signing")
        project = self.source / "WebDriverAgent.xcodeproj"
        if not project.exists():
            raise RuntimeError("WDA source is not installed")
        self._configure_runner_signing()
        payload = {
            "projectPath": str(project),
            "scheme": "WebDriverAgentRunner",
            "deviceId": self.device_udid,
            "derivedDataPath": str(self.source / "DerivedData"),
            "preferXcodebuild": False,
            "extraArgs": ["-allowProvisioningUpdates"],
        }
        return [
            self.xcodebuildmcp, "device", "test",
            "--json", json.dumps(payload, ensure_ascii=False),
            "--output", "text",
        ]

    def _configure_runner_signing(self) -> None:
        """Patch only WebDriverAgentRunner build settings.

        Passing signing settings on the command line also applies them to
        WebDriverAgentLib and breaks framework signing.
        """
        pbxproj = self.source / "WebDriverAgent.xcodeproj" / "project.pbxproj"
        if not pbxproj.is_file():
            raise RuntimeError(f"WDA project file missing: {pbxproj}")
        base = subprocess.run(
            ["git", "-C", str(self.source), "show",
             "HEAD:WebDriverAgent.xcodeproj/project.pbxproj"],
            capture_output=True, text=True,
        )
        if base.returncode:
            raise RuntimeError("cannot read pinned WDA project from git")
        pbxproj.write_text(
            self._patched_runner_project(base.stdout),
            encoding="utf-8",
        )

    def _patched_runner_project(self, text: str) -> str:
        lines = text.splitlines(keepends=True)
        output = []
        block = []
        in_configuration = False
        patched = 0
        runner_bundle = f"com.iloop.WebDriverAgentRunner.{self.team_id.lower()}"

        def patch(rows: list[str]) -> list[str]:
            nonlocal patched
            text = "".join(rows)
            if not (
                "IOSSettings.xcconfig" in text
                and "INFOPLIST_FILE = WebDriverAgentRunner/Info.plist;" in text
                and "USES_XCTRUNNER = YES;" in text
            ):
                return rows
            cleaned = []
            inside = False
            for row in rows:
                if "ILOOP_WDA_SIGNING_BEGIN" in row:
                    inside = True
                    continue
                if "ILOOP_WDA_SIGNING_END" in row:
                    inside = False
                    continue
                if not inside:
                    cleaned.append(row)
            signing = [
                "\t\t\t\t/* ILOOP_WDA_SIGNING_BEGIN */\n",
                "\t\t\t\tCODE_SIGN_STYLE = Automatic;\n",
                "\t\t\t\tCODE_SIGNING_ALLOWED = YES;\n",
                f"\t\t\t\tDEVELOPMENT_TEAM = {self.team_id};\n",
                "\t\t\t\t/* ILOOP_WDA_SIGNING_END */\n",
                f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {runner_bundle};\n",
            ]
            result = []
            replaced = False
            for row in cleaned:
                if "PRODUCT_BUNDLE_IDENTIFIER =" in row and "WebDriverAgentRunner" in row:
                    result.extend(signing)
                    replaced = True
                else:
                    result.append(row)
            if not replaced:
                raise RuntimeError("cannot locate WDA runner bundle setting")
            patched += 1
            return result

        for line in lines:
            if not in_configuration and re.match(
                r"^\t\t[A-F0-9]+ /\* (Debug|Release) \*/ = \{$", line.rstrip()
            ):
                in_configuration = True
                block = [line]
                continue
            if in_configuration:
                block.append(line)
                if line.rstrip("\n") == "\t\t};":
                    output.extend(patch(block))
                    block = []
                    in_configuration = False
                continue
            output.append(line)
        if block:
            output.extend(block)
        if patched != 2:
            raise RuntimeError(f"expected 2 WDA runner configurations, patched {patched}")
        return "".join(output)

    def prepare(self, timeout: float = 120.0) -> dict:
        current = self.status()
        current_runtime = current.get("runtime") or {}
        if (
            current["ready"]
            and current_runtime.get("device_udid") == self.device_udid
            and current_runtime.get("version") == WDA_VERSION
        ):
            return current
        if current["ready"] or current_runtime:
            self.stop()
        self.install_source()
        iproxy = shutil.which("iproxy")
        if not iproxy:
            raise RuntimeError("iproxy is required")
        self.root.mkdir(parents=True, exist_ok=True)
        wda_log = (self.root / "wda.log").open("ab")
        proxy_log = (self.root / "iproxy.log").open("ab")
        wda = None
        proxy = None
        try:
            wda = subprocess.Popen(
                self.command(), stdout=wda_log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            proxy = subprocess.Popen(
                [iproxy, str(self.local_port), "8100", "-u", self.device_udid],
                stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except BaseException:
            self._terminate_process(wda)
            self._terminate_process(proxy)
            raise
        finally:
            wda_log.close()
            proxy_log.close()
        try:
            self._write_state({
                "wda_pid": wda.pid,
                "proxy_pid": proxy.pid,
                "wda_identity_sha256": self._process_identity(wda.pid).get("sha256", ""),
                "proxy_identity_sha256": self._process_identity(proxy.pid).get("sha256", ""),
                "device_udid": self.device_udid,
                "version": WDA_VERSION,
                "local_port": self.local_port,
                "source_commit": self._source_identity().get("commit", ""),
                "started_at": time.time(),
            })
        except BaseException:
            self._terminate_process(proxy)
            self._terminate_process(wda)
            raise
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                status = self.status()
                if status["ready"]:
                    return status
                if wda.poll() is not None:
                    break
                time.sleep(1)
            raise RuntimeError(f"WDA did not become ready; log={self.root / 'wda.log'}")
        except BaseException:
            self._terminate_process(proxy)
            self._terminate_process(wda)
            self._write_state({})
            raise

    def stop(self) -> dict:
        state = self._read_state()
        stopped = []
        for key, marker, identity_key in (
            ("wda_pid", "xcodebuildmcp", "wda_identity_sha256"),
            ("proxy_pid", "iproxy", "proxy_identity_sha256"),
        ):
            pid = int(state.get(key) or 0)
            if pid > 0 and self._pid_matches(
                pid, marker, str(state.get(identity_key, ""))
            ):
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
        self._write_state({})
        return {"stopped": stopped}

    def _read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _process_identity(pid: int) -> dict:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True,
        )
        text = result.stdout.strip()
        if result.returncode != 0 or not text:
            return {}
        return {
            "command": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    @classmethod
    def _pid_matches(
        cls,
        pid: int,
        markers: str | tuple[str, ...],
        expected_sha256: str = "",
    ) -> bool:
        identity = cls._process_identity(pid)
        expected = (markers,) if isinstance(markers, str) else markers
        return (
            bool(expected_sha256)
            and identity.get("sha256") == expected_sha256
            and all(marker in identity.get("command", "") for marker in expected)
        )

    def _source_identity(self) -> dict:
        if not (self.source / ".git").exists():
            return {}
        commit = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        tag = subprocess.run(
            ["git", "-C", str(self.source), "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True,
        )
        origin = subprocess.run(
            ["git", "-C", str(self.source), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        status = subprocess.run(
            ["git", "-C", str(self.source), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True,
        )
        if commit.returncode or tag.returncode or origin.returncode or status.returncode:
            return {}
        origin_url = origin.stdout.strip()
        if origin_url == "git@github.com:appium/WebDriverAgent.git":
            origin_url = WDA_REPOSITORY
        changed = [
            line[3:]
            for line in status.stdout.splitlines()
            if len(line) > 3
        ]
        allowed = "WebDriverAgent.xcodeproj/project.pbxproj"
        worktree_valid = not changed
        if changed == [allowed] and self.team_id:
            base = subprocess.run(
                ["git", "-C", str(self.source), "show", f"HEAD:{allowed}"],
                capture_output=True, text=True,
            )
            current = self.source / allowed
            worktree_valid = (
                base.returncode == 0
                and current.is_file()
                and current.read_text(encoding="utf-8")
                == self._patched_runner_project(base.stdout)
            )
        return {
            "commit": commit.stdout.strip(),
            "tag": tag.stdout.strip(),
            "origin": origin_url,
            "worktree_valid": worktree_valid,
        }

    @staticmethod
    def _port_owner_pid(port: int) -> int:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True,
        )
        if result.returncode:
            return 0
        pids = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if len(pids) != 1:
            return 0
        try:
            return int(next(iter(pids)))
        except ValueError:
            return 0

    @staticmethod
    def _terminate_process(process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
