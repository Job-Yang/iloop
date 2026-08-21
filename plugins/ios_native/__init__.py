"""iOS 官方插件 —— 实现内核 Capability 契约（真实命令，非占位）。

两条独立链路，按 mode 分叉：
  simulator: xcrun simctl + xcodebuild        —— 更成熟
  real:      xcrun devicectl + WDA (Appium)   —— 真机自动化一等能力

真机 UI（截图/点击/滑动/输入/UI树）走 WDAClient（纯开源栈）。
签名走本机 Xcode，无私有服务。判成功看 success marker + 产物存在。

诚实缺口：
  - WDA 的 build/install/start 与 iproxy 生命周期仍需宿主准备
  - 真机 WDA 使用坐标动作，不冒充模拟器的语义 elementRef
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from kernel.capability import Capability, CapabilityResult, CapabilityStatus, unsupported
from kernel.runner import CommandOutput, CommandRunner
from kernel.evidence import EvidenceKind
from .environment import discover_developer_dir
from .evidence_writer import EvidenceWriter
from .wda_client import WDAClient
from .wda_manager import WDAManager

PLATFORM_ID = "ios_native"

# 真机核心链路依赖（全开源，无内网）
DEP_TOOLS = {
    "xcrun": "Xcode command line tools (devicectl/simctl/xcodebuild)",
    "xcodebuildmcp": "public Xcode build/run/simulator UI automation CLI",
}

class IOSNativePlugin:
    platform_id = PLATFORM_ID

    def __init__(self, mode: str = "simulator", *, data_dir: str = "/tmp/iloop-ios",
                 config: Optional[dict] = None, runner: Optional[CommandRunner] = None,
                 wda: Optional[WDAClient] = None) -> None:
        self.mode = mode  # "simulator" | "real"
        self.config = config or {}
        runner_environment = (
            getattr(runner, "environment_overrides", {})
            if runner is not None
            else {}
        )
        self.developer_dir = (
            getattr(runner, "developer_dir", None)
            or runner_environment.get("DEVELOPER_DIR")
            if runner is not None
            else discover_developer_dir()
        )
        self.runner = runner or CommandRunner(
            environment_overrides=(
                {"DEVELOPER_DIR": self.developer_dir}
                if self.developer_dir
                else {}
            )
        )
        self.writer = EvidenceWriter(data_dir)
        self.state_dir = Path(data_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        wda_port = int(self.config.get("wda_port", 8100))
        expected_wda_url = f"http://127.0.0.1:{wda_port}"
        if wda is not None and getattr(wda, "base", expected_wda_url) != expected_wda_url:
            raise ValueError(
                f"WDA client endpoint {wda.base} does not match managed endpoint {expected_wda_url}"
            )
        self.wda = wda or WDAClient(expected_wda_url)

    # ---- 契约声明 ----
    def capabilities(self) -> List[Capability]:
        caps = [
            Capability.DOCTOR, Capability.BUILD, Capability.RUN, Capability.INSTALL,
            Capability.LAUNCH, Capability.SCREENSHOT, Capability.VIEW_TREE,
            Capability.LOGS, Capability.PROBE, Capability.CRASH,
            Capability.COUNTER_PROBE,
            Capability.TAP, Capability.SWIPE, Capability.TYPE_TEXT,
            Capability.UI_PREPARE, Capability.UI_STATUS, Capability.UI_STOP,
        ]
        return caps

    def invoke(self, capability: Capability, **kwargs) -> CapabilityResult:
        if capability not in self.capabilities():
            return unsupported(self.platform_id, capability)
        handler = {
            Capability.DOCTOR: self._doctor,
            Capability.BUILD: self._build,
            Capability.RUN: self._run,
            Capability.INSTALL: self._install,
            Capability.LAUNCH: self._launch,
            Capability.SCREENSHOT: self._screenshot,
            Capability.VIEW_TREE: self._view_tree,
            Capability.LOGS: self._logs,
            Capability.PROBE: self._probe,
            Capability.COUNTER_PROBE: self._counter_probe,
            Capability.CRASH: self._crash,
            Capability.TAP: self._tap,
            Capability.SWIPE: self._swipe,
            Capability.TYPE_TEXT: self._type_text,
            Capability.UI_PREPARE: self._ui_prepare,
            Capability.UI_STATUS: self._ui_status,
            Capability.UI_STOP: self._ui_stop,
        }[capability]
        # 只透传 handler 真正接受的关键字；其余（如 sim_udid）已进 self.config
        import inspect
        accepted = set(inspect.signature(handler).parameters)
        passable = {k: v for k, v in kwargs.items() if k in accepted}
        try:
            return handler(**passable)
        except Exception as e:  # 任何异常都转成 error 结果，不让插件崩掉内核
            return self._err(capability, f"{type(e).__name__}: {e}")

    # ---- helpers ----
    def _ok(self, cap: Capability, summary: str, evidence_dir: str = "", artifacts=None) -> CapabilityResult:
        return CapabilityResult(self.platform_id, cap.value, CapabilityStatus.SUCCESS,
                                summary, evidence_dir, artifacts or [],
                                metadata={"device": "ios-device" if self.mode == "real"
                                          else "ios-simulator"})

    def _err(self, cap: Capability, summary: str, evidence_dir: str = "", artifacts=None) -> CapabilityResult:
        return CapabilityResult(self.platform_id, cap.value, CapabilityStatus.ERROR,
                                summary, evidence_dir, artifacts or [],
                                metadata={"device": "ios-device" if self.mode == "real"
                                          else "ios-simulator"})

    def _has_full_xcode(self) -> bool:
        return bool(
            self.developer_dir
            and not str(self.developer_dir).endswith("/CommandLineTools")
        )

    def _wda_manager(self) -> WDAManager:
        manager = WDAManager(
            self.state_dir / "wda",
            device_udid=self.config.get("device_udid", ""),
            team_id=self.config.get("team_id", ""),
            xcodebuildmcp=self._xb(),
            local_port=int(self.config.get("wda_port", 8100)),
        )
        manager.client = self.wda
        return manager

    def _xb(self) -> str:
        return self.config.get("xcodebuildmcp") or shutil.which("xcodebuildmcp") or "xcodebuildmcp"

    def _project_args(self) -> List[str]:
        if self.config.get("workspace"):
            return ["--workspace-path", self.config["workspace"]]
        if self.config.get("project"):
            return ["--project-path", self.config["project"]]
        return []

    def _target_args(self, *, require_simulator: bool = False) -> List[str]:
        if self.mode == "simulator":
            configured = str(self.config.get("sim_udid", ""))
            if not configured and not require_simulator:
                return []
            udid = self._simulator_id()
            if not udid:
                raise ValueError(
                    "未找到 booted simulator UUID；请启动模拟器或传 sim_udid"
                )
            return ["--simulator-id", udid]
        udid = self.config.get("device_udid", "")
        return ["--device-id", udid] if udid else []

    def _simulator_id(self, requested: str = "") -> str:
        configured = requested or str(self.config.get("sim_udid", ""))
        if configured and configured != "booted":
            return configured
        out = self.runner.run(
            ["xcrun", "simctl", "list", "devices", "booted", "--json"],
            timeout=30,
        )
        if not out.ok():
            return ""
        try:
            devices = json.loads(out.stdout or out.combined).get("devices", {})
        except (AttributeError, json.JSONDecodeError):
            return ""
        for rows in devices.values():
            for device in rows:
                if (
                    device.get("state") == "Booted"
                    and device.get("isAvailable", True)
                    and device.get("udid")
                ):
                    return str(device["udid"])
        return ""

    @staticmethod
    def _global_developer_dir() -> str:
        environment = dict(os.environ)
        environment.pop("DEVELOPER_DIR", None)
        try:
            completed = subprocess.run(
                ["/usr/bin/xcode-select", "-p"],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    @staticmethod
    def _artifact_path(text: str, suffix: str) -> Optional[Path]:
        try:
            root = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            root = None
        stack = [root]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str) and value.endswith(suffix):
                path = Path(value).expanduser()
                if path.exists():
                    return path
        line_match = re.search(
            rf"(?:runtime\s+logs?|log\s+path|path)\s*[:=]\s*"
            rf"([^\n]+{re.escape(suffix)})\s*$",
            text or "",
            re.IGNORECASE | re.MULTILINE,
        )
        if line_match:
            path = Path(
                line_match.group(1).strip().strip("'\"")
            ).expanduser()
            if path.exists():
                return path
        candidates = re.findall(
            r"(?:~|/)[^\s\"']*?" + re.escape(suffix),
            text or "",
        )
        return next(
            (
                Path(candidate).expanduser()
                for candidate in reversed(candidates)
                if Path(candidate).expanduser().exists()
            ),
            None,
        )

    @staticmethod
    def _structured_success(text: str) -> bool:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("didError") is False
            and payload.get("data", {}).get("summary", {}).get("status")
            == "SUCCEEDED"
        )

    @staticmethod
    def _snapshot_targets(text: str) -> dict[str, str]:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {}
        targets = payload.get("data", {}).get("capture", {}).get("targets", [])
        result = {}
        for target in targets:
            if not isinstance(target, str) or "|" not in target:
                continue
            reference, signature = target.split("|", 1)
            if reference and signature:
                result[reference] = signature
        return result

    def _snapshot_state_path(self, udid: str) -> Path:
        safe_udid = re.sub(r"[^A-Za-z0-9_.-]", "_", udid)
        return self.state_dir / f"snapshot-{safe_udid}.json"

    def _save_snapshot_targets(self, udid: str, text: str) -> None:
        targets = self._snapshot_targets(text)
        if targets:
            self._snapshot_state_path(udid).write_text(
                json.dumps({"udid": udid, "targets": targets}, indent=2),
                encoding="utf-8",
            )

    def _load_snapshot_targets(self, udid: str) -> dict[str, str]:
        path = self._snapshot_state_path(udid)
        if not path.is_file():
            return {}
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("udid") != udid:
            return {}
        return {
            str(reference): str(signature)
            for reference, signature in row.get("targets", {}).items()
        }

    # ---- DOCTOR ----
    def _doctor(self, **_) -> CapabilityResult:
        missing = [n for n in DEP_TOOLS if shutil.which(n) is None]
        notes = []
        if not self._has_full_xcode():
            notes.append("需完整 Xcode（当前是 CommandLineTools）：simctl/xcodebuild/devicectl 不可用")
        global_developer_dir = self._global_developer_dir()
        if (
            self.mode == "simulator"
            and global_developer_dir
            and global_developer_dir.endswith("/CommandLineTools")
        ):
            notes.append(
                "全局 xcode-select 指向 CommandLineTools，"
                "XcodeBuildMCP 语义 UI 不可用"
            )
        if self.mode == "simulator" and not self._simulator_id():
            notes.append("未发现已启动的 simulator UUID")
        if self.mode == "real" and shutil.which("iproxy") is None:
            notes.append("iproxy 缺失：仅 WDA 真机 UI 自动化不可用，编译/安装/拉起仍可用")
        if missing or notes:
            details = []
            if missing:
                details.append(", ".join(missing))
            details.extend(notes)
            return self._err(Capability.DOCTOR,
                             f"[{self.mode}] 未就绪: {'；'.join(details)}")
        return self._ok(Capability.DOCTOR,
                        f"[{self.mode}] 依赖齐备: {', '.join(DEP_TOOLS)} + 完整 Xcode")

    # ---- BUILD ----
    def _build(self, *, scheme: str = "", project: str = "", workspace: str = "",
               configuration: str = "Debug", derived_data: str = "") -> CapabilityResult:
        if project:
            self.config["project"] = project
        if workspace:
            self.config["workspace"] = workspace
        scheme = scheme or self.config.get("scheme", "")
        if not scheme:
            return self._err(Capability.BUILD, "缺少 scheme")
        argv = [self._xb(), self.mode if self.mode == "simulator" else "device", "build"]
        argv += self._project_args()
        argv += ["--scheme", scheme, "--configuration", configuration]
        if self.mode == "simulator":
            argv += self._target_args()
        if derived_data:
            argv += ["--derived-data-path", derived_data]
        argv += ["--output", "text"]
        out = self.runner.run(argv, timeout=1800)
        succeeded = out.returncode == 0 and bool(
            re.search(r"BUILD SUCCEEDED|Build succeeded|build succeeded", out.combined)
        )
        ev, edir = self.writer.from_command(
            capability="build", source=f"{self.platform_id}.xcodebuildmcp", out=out,
            summary=("BUILD SUCCEEDED" if succeeded else "构建失败"),
            kind=EvidenceKind.OBSERVED,
            outcome="success" if succeeded else "failure")
        if succeeded:
            return self._ok(Capability.BUILD, f"[{self.mode}] {scheme} 构建成功", edir, [ev.id])
        return self._err(Capability.BUILD, f"[{self.mode}] {scheme} 构建失败（见 cmd.log）", edir, [ev.id])

    def _run(self, *, scheme: str = "", configuration: str = "Debug",
             derived_data: str = "", task_id: str = "direct",
             run_id: str = "direct") -> CapabilityResult:
        scheme = scheme or self.config.get("scheme", "")
        if not scheme:
            return self._err(Capability.RUN, "缺少 scheme")
        workflow = "simulator" if self.mode == "simulator" else "device"
        argv = [self._xb(), workflow, "build-and-run"]
        argv += self._project_args()
        argv += ["--scheme", scheme, "--configuration", configuration]
        target_args = self._target_args(require_simulator=True)
        argv += target_args
        target_id = target_args[-1] if target_args else ""
        if derived_data:
            argv += ["--derived-data-path", derived_data]
        argv += ["--output", "text"]
        run_started_at = time.time()
        out = self.runner.run(argv, timeout=1800)
        runtime_log = self._artifact_path(out.stdout or out.combined, ".log")
        succeeded = out.returncode == 0 and bool(
            re.search(r"build succeeded|BUILD SUCCEEDED|launch(?:ed)?|running", out.combined, re.I)
        )
        if succeeded:
            state = {
                "captured_at": time.time(),
                "started_at": run_started_at,
                "status": "success",
                "task_id": task_id,
                "run_id": run_id,
                "mode": self.mode,
                "scheme": scheme,
                "project": self.config.get("project", ""),
                "workspace": self.config.get("workspace", ""),
                "runtime_log": str(runtime_log) if runtime_log else "",
                "bundle_id": self.config.get("bundle_id", ""),
                "device_id": target_id,
            }
            (self.state_dir / f"last-run-{task_id}.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            state_path = self.state_dir / f"last-run-{task_id}.json"
            if state_path.is_file():
                previous = json.loads(state_path.read_text(encoding="utf-8"))
                if previous.get("run_id") == run_id:
                    state_path.unlink()
        ev, edir = self.writer.from_command(
            capability="run", source=f"{self.platform_id}.xcodebuildmcp", out=out,
            summary=("构建并拉起成功" if succeeded else "构建或拉起失败"),
            outcome="success" if succeeded else "failure")
        if succeeded:
            return self._ok(Capability.RUN, f"[{self.mode}] 构建、安装、拉起完成；运行日志路径见证据",
                            edir, [ev.id])
        return self._err(Capability.RUN, f"[{self.mode}] build-and-run 失败", edir, [ev.id])

    # ---- INSTALL ----
    def _install(self, *, app_path: str = "", udid: str = "") -> CapabilityResult:
        app_path = app_path or self.config.get("app_path", "")
        if not app_path:
            return self._err(Capability.INSTALL, "缺少 app_path")
        if self.mode == "simulator":
            udid = self._simulator_id(udid)
            if not udid:
                return self._err(Capability.INSTALL, "未找到 booted simulator UUID")
            argv = [self._xb(), "simulator", "install",
                    "--simulator-id", udid, "--app-path", app_path]
        else:
            udid = udid or self.config.get("device_udid", "")
            if not udid:
                return self._err(Capability.INSTALL, "真机 install 需要 device udid")
            argv = [self._xb(), "device", "install",
                    "--device-id", udid, "--app-path", app_path]
        out = self.runner.run(argv, timeout=600)
        ev, edir = self.writer.from_command(capability="install",
                                            source=f"{self.platform_id}.{self.mode}", out=out,
                                            summary=("安装成功" if out.ok() else "安装失败"))
        if out.ok():
            return self._ok(Capability.INSTALL, f"[{self.mode}] 安装成功", edir, [ev.id])
        return self._err(Capability.INSTALL, f"[{self.mode}] 安装失败", edir, [ev.id])

    # ---- LAUNCH ----
    def _launch(self, *, bundle_id: str = "", udid: str = "", url: str = "") -> CapabilityResult:
        bundle_id = bundle_id or self.config.get("bundle_id", "")
        if self.mode == "simulator":
            udid = self._simulator_id(udid)
            if not udid:
                return self._err(Capability.LAUNCH, "未找到 booted simulator UUID")
            if url:
                argv = ["xcrun", "simctl", "openurl", udid, url]
            else:
                if not bundle_id:
                    return self._err(Capability.LAUNCH, "缺少 bundle_id")
                argv = [self._xb(), "simulator", "launch-app",
                        "--simulator-id", udid, "--bundle-id", bundle_id, "--output", "text"]
        else:
            udid = udid or self.config.get("device_udid", "")
            if not udid or not bundle_id:
                return self._err(Capability.LAUNCH, "真机 launch 需要 device udid + bundle_id")
            argv = [self._xb(), "device", "launch",
                    "--device-id", udid, "--bundle-id", bundle_id, "--output", "text"]
            if url:
                return self._err(Capability.LAUNCH,
                                 "XcodeBuildMCP device launch 不支持 payload URL；请先用普通 bundle launch 验证")
        out = self.runner.run(argv, timeout=120)
        ev, edir = self.writer.from_command(capability="launch",
                                            source=f"{self.platform_id}.{self.mode}", out=out,
                                            summary=("拉起成功" if out.ok() else "拉起失败"))
        if out.ok():
            return self._ok(Capability.LAUNCH, f"[{self.mode}] 拉起成功", edir, [ev.id])
        return self._err(Capability.LAUNCH, f"[{self.mode}] 拉起失败", edir, [ev.id])

    # ---- SCREENSHOT ----
    def _screenshot(self, *, udid: str = "", out_path: str = "") -> CapabilityResult:
        edir = self.writer._dir("screenshot")
        image_path = Path(out_path) if out_path else None
        if self.mode == "simulator":
            udid = self._simulator_id(udid)
            if not udid:
                return self._err(Capability.SCREENSHOT, "未找到 booted simulator UUID", str(edir))
            out = self.runner.run(
                [self._xb(), "ui-automation", "screenshot", "--simulator-id", udid,
                 "--return-format", "path", "--output", "json"],
                timeout=120,
            )
            output = out.stdout or out.combined
            source = next(
                (
                    found
                    for suffix in (".png", ".jpg", ".jpeg")
                    if (found := self._artifact_path(output, suffix))
                ),
                None,
            )
            if source:
                if image_path is None:
                    image_path = Path(edir) / f"shot{source.suffix.lower()}"
                elif image_path.suffix.lower() != source.suffix.lower():
                    image_path = image_path.with_suffix(source.suffix.lower())
                shutil.copy2(source, image_path)
            success = bool(
                source
                and image_path
                and image_path.exists()
                and (out.ok() or self._structured_success(output))
            )
            if not success:
                ev, _ = self.writer.from_command(
                    capability="screenshot",
                    source=f"{self.platform_id}.simulator",
                    out=out,
                    summary="截图失败",
                    directory=edir,
                )
                return self._err(
                    Capability.SCREENSHOT,
                    "[simulator] 截图失败",
                    str(edir),
                    [ev.id],
                )
        else:
            if image_path is None:
                image_path = Path(edir) / "shot.png"
            ready = self._wda_manager().status()
            if not ready["ready"]:
                return self._err(
                    Capability.SCREENSHOT,
                    "[real] managed WDA is not ready for the selected device",
                    str(edir),
                )
            # 真机走 WDA 截图（Appium 社区版）
            data = self.wda.screenshot_png()
            if not data:
                return self._err(Capability.SCREENSHOT, "[real] WDA 截图为空（WDA 是否在线？）", str(edir))
            image_path.write_bytes(data)
        ev = self.writer.register_file(capability="screenshot",
                                       source=f"{self.platform_id}.{self.mode}",
                                       file_path=str(image_path), summary="截图产物")
        return self._ok(
            Capability.SCREENSHOT,
            f"[{self.mode}] 截图成功 -> {image_path}",
            str(edir),
            [ev.id],
        )

    # ---- VIEW_TREE ----
    def _view_tree(self, *, udid: str = "") -> CapabilityResult:
        edir = self.writer._dir("view_tree")
        tree_file = Path(edir) / "tree.json"
        if self.mode == "simulator":
            udid = self._simulator_id(udid)
            if not udid:
                return self._err(Capability.VIEW_TREE, "未找到 booted simulator UUID", str(edir))
            out = self.runner.run(
                [self._xb(), "simulator", "snapshot-ui", "--simulator-id", udid,
                 "--output", "json"],
                timeout=60,
            )
            tree_file.write_text(out.stdout or out.combined, encoding="utf-8")
            if out.ok():
                self._save_snapshot_targets(udid, out.stdout or out.combined)
            ev = self.writer.register_file(
                capability="view_tree",
                source=f"{self.platform_id}.xcodebuildmcp",
                file_path=str(tree_file),
                summary="模拟器语义 UI 层级树（含 elementRef）",
            )
            if out.ok() and tree_file.stat().st_size:
                return self._ok(Capability.VIEW_TREE,
                                "[simulator] XcodeBuildMCP 语义 UI 树已抓取",
                                str(edir), [ev.id])
            return self._err(Capability.VIEW_TREE,
                             "[simulator] XcodeBuildMCP UI 树抓取失败",
                             str(edir), [ev.id])
        else:
            ready = self._wda_manager().status()
            if not ready["ready"]:
                return self._err(
                    Capability.VIEW_TREE,
                    "[real] managed WDA is not ready for the selected device",
                    str(edir),
                )
            import json as _json
            tree = self.wda.source()
            tree_file.write_text(_json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
            ev = self.writer.register_file(capability="view_tree",
                                           source=f"{self.platform_id}.wda",
                                           file_path=str(tree_file), summary="真机 UI 层级树(WDA)")
            return self._ok(Capability.VIEW_TREE, "[real] WDA UI 树已抓取", str(edir), [ev.id])

    # ---- LOGS ----
    def _logs(self, *, udid: str = "", predicate: str = "", limit: int = 200,
              task_id: str = "direct", run_id: str = "direct") -> CapabilityResult:
        log_path = self.config.get("log_path", "")
        candidates = []
        if log_path and task_id == "direct":
            candidates = [Path(log_path)]
        else:
            state_path = self.state_dir / f"last-run-{task_id}.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if (
                    state.get("status") == "success"
                    and state.get("task_id") == task_id
                    and state.get("run_id") == run_id
                    and state.get("mode") == self.mode
                    and state.get("runtime_log")
                ):
                    candidates = [Path(state["runtime_log"])]
        source = next((p for p in candidates if p.exists() and p.is_file()), None)
        if source is None:
            return self._err(
                Capability.LOGS,
                f"[{self.mode}] 本次 run 未绑定可用动态日志；先执行 run，或显式传 log_path",
            )
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        if predicate:
            lines = [line for line in lines if predicate.lower() in line.lower()]
        selected = lines[-max(1, int(limit)):]
        edir = self.writer._dir("logs")
        dest = Path(edir) / "runtime.log"
        dest.write_text("\n".join(selected) + ("\n" if selected else ""), encoding="utf-8")
        ev = self.writer.register_file(
            capability="logs",
            source=f"{self.platform_id}.xcodebuildmcp",
            file_path=str(dest),
            summary=f"动态日志 {len(selected)} 行，源={source}",
        )
        if predicate and not selected:
            return self._err(
                Capability.LOGS,
                f"[{self.mode}] 动态日志中未命中 predicate={predicate!r}",
                str(edir),
                [ev.id],
            )
        return self._ok(Capability.LOGS,
                        f"[{self.mode}] 动态日志已抓取 {len(selected)} 行 -> {dest}",
                        str(edir), [ev.id])

    def _tap(self, *, udid: str = "", element_ref: str = "",
             x: float = -1, y: float = -1) -> CapabilityResult:
        if self.mode == "real":
            if not self._wda_manager().status()["ready"]:
                return self._err(Capability.TAP, "[real] managed WDA is not ready")
            x, y = float(x), float(y)
            if x < 0 or y < 0:
                return self._err(Capability.TAP, "真机 WDA tap 需要 x + y")
            result = self.wda.tap(float(x), float(y))
            return self._real_ui_evidence(Capability.TAP, "tap", result)
        return self._sim_ui_action(Capability.TAP, "tap", udid, [
            "--element-ref", element_ref,
        ] if element_ref else [], required="element_ref")

    def _swipe(self, *, udid: str = "", within_element_ref: str = "",
               direction: str = "up", distance: float = 0.7,
               x1: float = -1, y1: float = -1,
               x2: float = -1, y2: float = -1) -> CapabilityResult:
        if self.mode == "real":
            if not self._wda_manager().status()["ready"]:
                return self._err(Capability.SWIPE, "[real] managed WDA is not ready")
            x1, y1, x2, y2 = map(float, (x1, y1, x2, y2))
            if min(x1, y1, x2, y2) < 0:
                return self._err(Capability.SWIPE, "真机 WDA swipe 需要 x1/y1/x2/y2")
            result = self.wda.swipe(float(x1), float(y1), float(x2), float(y2))
            return self._real_ui_evidence(Capability.SWIPE, "swipe", result)
        args = ["--within-element-ref", within_element_ref, "--direction", direction,
                "--distance", str(distance)] if within_element_ref else []
        return self._sim_ui_action(Capability.SWIPE, "swipe", udid, args,
                                   required="within_element_ref")

    def _type_text(self, *, udid: str = "", element_ref: str = "",
                   text: str = "") -> CapabilityResult:
        if self.mode == "real":
            if not self._wda_manager().status()["ready"]:
                return self._err(Capability.TYPE_TEXT, "[real] managed WDA is not ready")
            if not text:
                return self._err(Capability.TYPE_TEXT, "真机 type_text 需要 text")
            result = self.wda.type_text(text)
            return self._real_ui_evidence(
                Capability.TYPE_TEXT, "type_text", result
            )
        args = ["--element-ref", element_ref, "--text", text] if element_ref and text else []
        return self._sim_ui_action(Capability.TYPE_TEXT, "type-text", udid, args,
                                   required="element_ref + text")

    def _ui_prepare(self, **_) -> CapabilityResult:
        if self.mode != "real":
            return self._ok(Capability.UI_PREPARE,
                            "[simulator] XcodeBuildMCP UI automation requires no WDA preparation")
        try:
            status = self._wda_manager().prepare(
                timeout=float(self.config.get("wda_timeout", 120))
            )
        except Exception as error:
            return self._err(Capability.UI_PREPARE, f"[real] WDA prepare failed: {error}")
        return self._ok(Capability.UI_PREPARE, f"[real] WDA ready: {status.get('ready')}")

    def _ui_status(self, **_) -> CapabilityResult:
        if self.mode != "real":
            return self._ok(Capability.UI_STATUS, "[simulator] XcodeBuildMCP UI automation")
        status = self._wda_manager().status()
        if status["ready"]:
            return self._ok(Capability.UI_STATUS, "[real] WDA ready")
        return self._err(Capability.UI_STATUS, "[real] WDA is not ready")

    def _ui_stop(self, **_) -> CapabilityResult:
        if self.mode != "real":
            return self._ok(Capability.UI_STOP, "[simulator] no managed WDA runtime")
        stopped = self._wda_manager().stop()
        if stopped.get("unresolved"):
            return self._err(
                Capability.UI_STOP,
                f"[real] WDA ownership could not be verified: {stopped['unresolved']}",
            )
        return self._ok(Capability.UI_STOP, f"[real] WDA runtime stopped: {stopped['stopped']}")

    def _sim_ui_action(self, capability: Capability, command: str, udid: str,
                       args: List[str], *, required: str) -> CapabilityResult:
        if not args:
            return self._err(capability, f"{command} 需要 {required}")
        udid = self._simulator_id(udid)
        if not udid:
            return self._err(capability, "未找到 booted simulator UUID")
        argv = [
            self._xb(), "ui-automation", command, "--simulator-id", udid,
            *args, "--output", "text",
        ]
        out = self.runner.run(argv, timeout=60)
        if not out.ok() and any(
            code in out.combined
            for code in ("SNAPSHOT_EXPIRED", "SNAPSHOT_MISSING")
        ):
            refresh = self.runner.run(
                [
                    self._xb(), "simulator", "snapshot-ui",
                    "--simulator-id", udid, "--output", "json",
                ],
                timeout=60,
            )
            if refresh.ok():
                reference_flags = {
                    "--element-ref",
                    "--within-element-ref",
                }
                old_reference = next(
                    (
                        args[index + 1]
                        for index, item in enumerate(args[:-1])
                        if item in reference_flags
                    ),
                    "",
                )
                old_signature = self._load_snapshot_targets(udid).get(
                    old_reference, ""
                )
                refreshed_targets = self._snapshot_targets(refresh.combined)
                rebound_references = [
                    reference
                    for reference, signature in refreshed_targets.items()
                    if old_signature and signature == old_signature
                ]
                if len(rebound_references) == 1:
                    rebound_reference = rebound_references[0]
                    retry_argv = list(argv)
                    for index, item in enumerate(retry_argv[:-1]):
                        if (
                            item in reference_flags
                            and retry_argv[index + 1] == old_reference
                        ):
                            retry_argv[index + 1] = rebound_reference
                            break
                    retry = self.runner.run(retry_argv, timeout=60)
                    out = CommandOutput(
                        argv=retry.argv,
                        returncode=retry.returncode,
                        stdout=(
                            "--- snapshot refresh ---\n"
                            f"{refresh.combined}\n"
                            "--- action retry ---\n"
                            f"{retry.stdout}"
                        ),
                        stderr=retry.stderr,
                        duration=(
                            out.duration + refresh.duration + retry.duration
                        ),
                    )
                elif len(rebound_references) > 1:
                    out = CommandOutput(
                        argv=argv,
                        returncode=1,
                        stdout=refresh.combined,
                        stderr=(
                            "snapshot rebind is ambiguous: "
                            f"{len(rebound_references)} targets match "
                            f"{old_signature!r}; fetch a new view_tree"
                        ),
                        duration=out.duration + refresh.duration,
                    )
        if out.ok():
            self._snapshot_state_path(udid).unlink(missing_ok=True)
        ev, edir = self.writer.from_command(
            capability=capability.value,
            source=f"{self.platform_id}.xcodebuildmcp",
            out=out,
            summary=(f"{command} 成功" if out.ok() else f"{command} 失败"),
        )
        if out.ok():
            return self._ok(capability, f"[simulator] {command} 成功", edir, [ev.id])
        return self._err(capability, f"[simulator] {command} 失败", edir, [ev.id])

    def _real_ui_evidence(
        self,
        capability: Capability,
        action: str,
        result: dict,
    ) -> CapabilityResult:
        edir = self.writer._dir(capability.value)
        artifact = Path(edir) / "wda-response.json"
        artifact.write_text(
            json.dumps(
                {"action": action, "response": result, "captured_at": time.time()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        evidence = self.writer.register_file(
            capability=capability.value,
            source=f"{self.platform_id}.wda",
            file_path=str(artifact),
            summary=f"真机 WDA {action} 响应",
        )
        return self._ok(
            capability,
            f"[real] WDA {action} 完成",
            str(edir),
            [evidence.id],
        )

    # ---- PROBE ----
    def _probe(self, *, udid: str = "") -> CapabilityResult:
        """探测：设备/模拟器枚举 + 环境快照。"""
        if self.mode == "simulator":
            argv = [self._xb(), "simulator", "list", "--output", "text"]
        else:
            argv = [self._xb(), "device", "list", "--output", "text"]
        out = self.runner.run(
            argv,
            timeout=float(self.config.get("probe_timeout", 120)),
        )
        ev, edir = self.writer.from_command(capability="probe",
                                            source=f"{self.platform_id}.xcodebuildmcp", out=out,
                                            summary=("探测完成" if out.ok() else "探测失败"))
        if out.ok():
            return self._ok(Capability.PROBE, f"[{self.mode}] 探测完成", edir, [ev.id])
        return self._err(Capability.PROBE, f"[{self.mode}] 探测失败", edir, [ev.id])

    def _counter_probe(
        self,
        *,
        counter_capability: str = "",
        counter_condition: str = "",
        counter_expect: str = "",
        scheme: str = "",
        bundle_id: str = "",
        predicate: str = "",
        udid: str = "",
    ) -> CapabilityResult:
        if not counter_condition.strip():
            return self._err(
                Capability.COUNTER_PROBE,
                "counter_probe requires counter_condition",
            )
        if not (
            counter_expect.startswith("summary_contains:")
            or counter_expect.startswith("artifact_contains:")
        ):
            return self._err(
                Capability.COUNTER_PROBE,
                "counter_probe requires counter_expect="
                "summary_contains:<text> or artifact_contains:<text>",
            )
        try:
            capability = Capability(counter_capability)
        except ValueError:
            return self._err(
                Capability.COUNTER_PROBE,
                f"invalid counter_capability: {counter_capability}",
            )
        if capability == Capability.COUNTER_PROBE:
            return self._err(
                Capability.COUNTER_PROBE,
                "counter_probe cannot invoke itself",
            )
        result = self.invoke(
            capability,
            scheme=scheme,
            bundle_id=bundle_id,
            predicate=predicate,
            udid=udid,
        )
        if not result.ok():
            return self._err(
                Capability.COUNTER_PROBE,
                f"counter condition failed: {result.summary}",
                result.evidence_dir,
                result.artifacts,
            )
        expectation_kind, expected_text = counter_expect.split(":", 1)
        matched = False
        if expected_text:
            if expectation_kind == "summary_contains":
                matched = expected_text in result.summary
            else:
                evidence_root = Path(result.evidence_dir)
                if evidence_root.is_dir():
                    for item in evidence_root.rglob("*"):
                        if not item.is_file() or item.stat().st_size > 2_000_000:
                            continue
                        try:
                            text = item.read_text(
                                encoding="utf-8", errors="replace"
                            )
                        except OSError:
                            continue
                        if expected_text in text:
                            matched = True
                            break
        if not matched:
            return self._err(
                Capability.COUNTER_PROBE,
                f"counter expectation did not match: {counter_expect}",
                result.evidence_dir,
                result.artifacts,
            )
        edir = self.writer._dir("counter_probe")
        artifact = Path(edir) / "counter-result.json"
        artifact.write_text(
            json.dumps({
                "condition": counter_condition,
                "capability": capability.value,
                "expectation": counter_expect,
                "matched": True,
                "result": result.to_dict(),
                "captured_at": time.time(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        evidence = self.writer.register_file(
            capability="counter_probe",
            source=f"{self.platform_id}.{self.mode}",
            file_path=str(artifact),
            summary=f"反证条件已执行: {counter_condition}",
        )
        return self._ok(
            Capability.COUNTER_PROBE,
            f"[{self.mode}] 反证条件已执行",
            str(edir),
            [evidence.id],
        )

    # ---- CRASH ----
    def _crash(self, *, udid: str = "", bundle_id: str = "",
               task_id: str = "direct", run_id: str = "direct",
               since_seconds: int = 600) -> CapabilityResult:
        """采集本地 crash report（.ips/.crash）。

        模拟器：扫 ~/Library/Logs/DiagnosticReports（模拟器 crash 也落这里）。
        真机：xcrun devicectl device copy from 拉设备 crash 目录（需 device udid）。
        """
        edir = self.writer._dir("crash")
        bundle = bundle_id or self.config.get("bundle_id", "")
        if not bundle:
            return self._err(
                Capability.CRASH,
                f"[{self.mode}] crash 采集需要 bundle_id",
                str(edir),
            )
        since = time.time() - max(1, int(since_seconds))
        selected_device_id = (
            udid or self.config.get("device_udid", "")
            if self.mode == "real"
            else self._simulator_id(udid)
        )
        if task_id != "direct":
            state_path = self.state_dir / f"last-run-{task_id}.json"
            if not state_path.is_file():
                return self._err(
                    Capability.CRASH,
                    f"[{self.mode}] 本次 crash 未绑定前序成功 run",
                    str(edir),
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                state.get("status") != "success"
                or state.get("task_id") != task_id
                or state.get("run_id") != run_id
                or state.get("mode") != self.mode
                or state.get("bundle_id") != bundle
                or state.get("device_id") != selected_device_id
            ):
                return self._err(
                    Capability.CRASH,
                    f"[{self.mode}] crash run binding mismatch",
                    str(edir),
                )
            since = float(state.get("started_at", since))
        if self.mode == "simulator":
            reports_dir = Path.home() / "Library" / "Logs" / "DiagnosticReports"
            if not reports_dir.exists():
                return self._err(Capability.CRASH, "[simulator] 无 DiagnosticReports 目录", str(edir))
            crashes = sorted(reports_dir.glob("*.ips"), key=lambda p: p.stat().st_mtime, reverse=True)
            crashes = [item for item in crashes if item.stat().st_mtime >= since]
            if bundle:
                short = bundle.split(".")[-1]
                crashes = [
                    crash for crash in crashes
                    if crash.name.lower().startswith(short.lower() + "-")
                    or bundle.lower() in crash.read_text(
                        encoding="utf-8", errors="replace"
                    ).lower()
                ]
            if not crashes:
                manifest = Path(edir) / "absence.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "bundle_id": bundle,
                            "since": since,
                            "run_id": run_id,
                            "matches": 0,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                ev = self.writer.register_file(
                    capability="crash",
                    source=f"{self.platform_id}.simulator",
                    file_path=str(manifest),
                    summary=f"{bundle} 在绑定时间窗内无 crash report",
                )
                return self._ok(
                    Capability.CRASH,
                    "[simulator] 绑定时间窗内无目标 App crash report",
                    str(edir),
                    [ev.id],
                )
            latest = crashes[0]
            dest = Path(edir) / latest.name
            dest.write_text(latest.read_text(errors="replace"), encoding="utf-8")
            ev = self.writer.register_file(capability="crash", source=f"{self.platform_id}.simulator",
                                           file_path=str(dest), summary=f"最近 crash report: {latest.name}")
            return self._ok(Capability.CRASH, f"[simulator] 采集到 {len(crashes)} 份 crash，最新 {latest.name}",
                            str(edir), [ev.id])
        else:
            udid = udid or self.config.get("device_udid", "")
            if not udid:
                return self._err(Capability.CRASH, "真机 crash 采集需要 device udid")
            raw_dir = Path(edir) / "raw"
            out = self.runner.run(
                ["xcrun", "devicectl", "device", "copy", "from", "--device", udid,
                 "--domain-type", "systemCrashLogs",
                 "--source", ".",
                 "--destination", str(raw_dir)],
                timeout=300)
            candidates = [
                item for item in raw_dir.rglob("*")
                if item.is_file() and item.suffix.lower() in {".ips", ".crash"}
            ]
            short = bundle.split(".")[-1].lower()
            reports = [
                item for item in candidates
                if item.stat().st_mtime >= since
                and (
                    item.name.lower().startswith(short + "-")
                    or bundle.lower() in item.read_text(
                        encoding="utf-8", errors="replace"
                    ).lower()
                )
            ]
            selected = []
            for report in reports:
                destination = Path(edir) / report.name
                shutil.copy2(report, destination)
                selected.append(destination)
            shutil.rmtree(raw_dir, ignore_errors=True)
            ev, _ = self.writer.from_command(
                capability="crash",
                source=f"{self.platform_id}.real",
                out=out,
                summary=("真机 crash 查询完成" if out.ok()
                         else "真机 crash 拉取失败"),
                directory=edir,
                outcome="success" if out.ok() else "failure",
            )
            if out.ok() and selected:
                return self._ok(Capability.CRASH, "[real] 真机 crash report 已拉取", str(edir), [ev.id])
            if out.ok():
                absence = Path(edir) / "absence.json"
                absence.write_text(
                    json.dumps({
                        "bundle_id": bundle,
                        "device_id": udid,
                        "task_id": task_id,
                        "run_id": run_id,
                        "since": since,
                        "matches": 0,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                absence_evidence = self.writer.register_file(
                    capability="crash",
                    source=f"{self.platform_id}.real",
                    file_path=str(absence),
                    summary=f"{bundle} 在绑定真机 run 中无 crash report",
                )
                return self._ok(
                    Capability.CRASH,
                    "[real] 绑定 run 内无目标 App crash report",
                    str(edir),
                    [ev.id, absence_evidence.id],
                )
            return self._err(Capability.CRASH, "[real] 真机 crash 拉取失败（设备权限/路径？）", str(edir), [ev.id])
