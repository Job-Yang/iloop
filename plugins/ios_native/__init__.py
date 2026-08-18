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
import re
import shutil
from pathlib import Path
from typing import List, Optional

from kernel.capability import Capability, CapabilityResult, CapabilityStatus, unsupported
from kernel.runner import CommandRunner
from kernel.evidence import EvidenceKind
from .evidence_writer import EvidenceWriter
from .wda_client import WDAClient

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
        self.runner = runner or CommandRunner()
        self.writer = EvidenceWriter(data_dir)
        self.wda = wda or WDAClient()

    # ---- 契约声明 ----
    def capabilities(self) -> List[Capability]:
        caps = [
            Capability.DOCTOR, Capability.BUILD, Capability.RUN, Capability.INSTALL,
            Capability.LAUNCH, Capability.SCREENSHOT, Capability.VIEW_TREE,
            Capability.LOGS, Capability.PROBE, Capability.CRASH,
            Capability.TAP, Capability.SWIPE, Capability.TYPE_TEXT,
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
            Capability.CRASH: self._crash,
            Capability.TAP: self._tap,
            Capability.SWIPE: self._swipe,
            Capability.TYPE_TEXT: self._type_text,
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
                                summary, evidence_dir, artifacts or [])

    def _err(self, cap: Capability, summary: str, evidence_dir: str = "", artifacts=None) -> CapabilityResult:
        return CapabilityResult(self.platform_id, cap.value, CapabilityStatus.ERROR,
                                summary, evidence_dir, artifacts or [])

    def _has_full_xcode(self) -> bool:
        # 不只信全局 xcode-select：runner 已通过 discover_developer_dir 自愈到已装 Xcode
        return getattr(self.runner, "developer_dir", None) is not None

    def _xb(self) -> str:
        return self.config.get("xcodebuildmcp") or shutil.which("xcodebuildmcp") or "xcodebuildmcp"

    def _project_args(self) -> List[str]:
        if self.config.get("workspace"):
            return ["--workspace-path", self.config["workspace"]]
        if self.config.get("project"):
            return ["--project-path", self.config["project"]]
        return []

    def _target_args(self) -> List[str]:
        if self.mode == "simulator":
            udid = self.config.get("sim_udid", "booted")
            return ["--simulator-id", udid]
        udid = self.config.get("device_udid", "")
        return ["--device-id", udid] if udid else []

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
                path = Path(value)
                if path.exists():
                    return path
        candidates = re.findall(r"(?:/[^ \n]+)+" + re.escape(suffix), text or "")
        return next((Path(candidate) for candidate in reversed(candidates)
                     if Path(candidate).exists()), None)

    # ---- DOCTOR ----
    def _doctor(self, **_) -> CapabilityResult:
        missing = [n for n in DEP_TOOLS if shutil.which(n) is None]
        notes = []
        if not self._has_full_xcode():
            notes.append("需完整 Xcode（当前是 CommandLineTools）：simctl/xcodebuild/devicectl 不可用")
        if self.mode == "real" and shutil.which("iproxy") is None:
            notes.append("iproxy 缺失：仅 WDA 真机 UI 自动化不可用，编译/安装/拉起仍可用")
        if missing or notes:
            return self._err(Capability.DOCTOR,
                             f"[{self.mode}] 未就绪: {', '.join(missing)}" +
                             (("；" + "；".join(notes)) if notes else ""))
        suffix = ("；" + "；".join(notes)) if notes else ""
        return self._ok(Capability.DOCTOR,
                        f"[{self.mode}] 依赖齐备: {', '.join(DEP_TOOLS)} + 完整 Xcode{suffix}")

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
            kind=EvidenceKind.OBSERVED)
        if succeeded:
            return self._ok(Capability.BUILD, f"[{self.mode}] {scheme} 构建成功", edir, [ev.id])
        return self._err(Capability.BUILD, f"[{self.mode}] {scheme} 构建失败（见 cmd.log）", edir, [ev.id])

    def _run(self, *, scheme: str = "", configuration: str = "Debug",
             derived_data: str = "") -> CapabilityResult:
        scheme = scheme or self.config.get("scheme", "")
        if not scheme:
            return self._err(Capability.RUN, "缺少 scheme")
        workflow = "simulator" if self.mode == "simulator" else "device"
        argv = [self._xb(), workflow, "build-and-run"]
        argv += self._project_args()
        argv += ["--scheme", scheme, "--configuration", configuration]
        argv += self._target_args()
        if derived_data:
            argv += ["--derived-data-path", derived_data]
        argv += ["--output", "text"]
        out = self.runner.run(argv, timeout=1800)
        ev, edir = self.writer.from_command(
            capability="run", source=f"{self.platform_id}.xcodebuildmcp", out=out,
            summary=("构建并拉起成功" if out.ok() else "构建或拉起失败"))
        if out.ok():
            return self._ok(Capability.RUN, f"[{self.mode}] 构建、安装、拉起完成；运行日志路径见证据",
                            edir, [ev.id])
        return self._err(Capability.RUN, f"[{self.mode}] build-and-run 失败", edir, [ev.id])

    # ---- INSTALL ----
    def _install(self, *, app_path: str = "", udid: str = "") -> CapabilityResult:
        app_path = app_path or self.config.get("app_path", "")
        if not app_path:
            return self._err(Capability.INSTALL, "缺少 app_path")
        if self.mode == "simulator":
            udid = udid or self.config.get("sim_udid", "booted")
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
            udid = udid or self.config.get("sim_udid", "booted")
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
        png = out_path or str(Path(edir) / "shot.png")
        if self.mode == "simulator":
            udid = udid or self.config.get("sim_udid", "booted")
            out = self.runner.run(
                [self._xb(), "ui-automation", "screenshot", "--simulator-id", udid,
                 "--return-format", "path", "--output", "json"],
                timeout=60,
            )
            source = self._artifact_path(out.stdout or out.combined, ".png")
            if source:
                shutil.copy2(source, png)
            success = out.ok() and Path(png).exists()
            if not success:
                self.writer.from_command(capability="screenshot", source=f"{self.platform_id}.simulator",
                                         out=out, summary="截图失败")
                return self._err(Capability.SCREENSHOT, "[simulator] 截图失败", str(edir))
        else:
            # 真机走 WDA 截图（Appium 社区版）
            data = self.wda.screenshot_png()
            if not data:
                return self._err(Capability.SCREENSHOT, "[real] WDA 截图为空（WDA 是否在线？）", str(edir))
            Path(png).write_bytes(data)
        ev = self.writer.register_file(capability="screenshot",
                                       source=f"{self.platform_id}.{self.mode}",
                                       file_path=png, summary="截图产物")
        return self._ok(Capability.SCREENSHOT, f"[{self.mode}] 截图成功 -> {png}", str(edir), [ev.id])

    # ---- VIEW_TREE ----
    def _view_tree(self, *, udid: str = "") -> CapabilityResult:
        edir = self.writer._dir("view_tree")
        tree_file = Path(edir) / "tree.json"
        if self.mode == "simulator":
            udid = udid or self.config.get("sim_udid", "booted")
            out = self.runner.run(
                [self._xb(), "simulator", "snapshot-ui", "--simulator-id", udid,
                 "--output", "json"],
                timeout=60,
            )
            tree_file.write_text(out.stdout or out.combined, encoding="utf-8")
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
            import json as _json
            tree = self.wda.source()
            tree_file.write_text(_json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
            ev = self.writer.register_file(capability="view_tree",
                                           source=f"{self.platform_id}.wda",
                                           file_path=str(tree_file), summary="真机 UI 层级树(WDA)")
            return self._ok(Capability.VIEW_TREE, "[real] WDA UI 树已抓取", str(edir), [ev.id])

    # ---- LOGS ----
    def _logs(self, *, udid: str = "", predicate: str = "", limit: int = 200) -> CapabilityResult:
        log_path = self.config.get("log_path", "")
        candidates = []
        if log_path:
            candidates = [Path(log_path)]
        else:
            root = Path.home() / "Library" / "Developer" / "XcodeBuildMCP"
            candidates = sorted(root.glob("workspaces/*/logs/*.log"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
        source = next((p for p in candidates if p.exists() and p.is_file()), None)
        if source is None:
            return self._err(
                Capability.LOGS,
                f"[{self.mode}] 未找到 XcodeBuildMCP 动态日志；先执行 run/launch，或传 log_path",
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
        return self._ok(Capability.LOGS,
                        f"[{self.mode}] 动态日志已抓取 {len(selected)} 行 -> {dest}",
                        str(edir), [ev.id])

    def _tap(self, *, udid: str = "", element_ref: str = "",
             x: float = -1, y: float = -1) -> CapabilityResult:
        if self.mode == "real":
            x, y = float(x), float(y)
            if x < 0 or y < 0:
                return self._err(Capability.TAP, "真机 WDA tap 需要 x + y")
            result = self.wda.tap(float(x), float(y))
            return self._ok(Capability.TAP, f"[real] WDA tap 完成: {result}")
        return self._sim_ui_action(Capability.TAP, "tap", udid, [
            "--element-ref", element_ref,
        ] if element_ref else [], required="element_ref")

    def _swipe(self, *, udid: str = "", within_element_ref: str = "",
               direction: str = "up", distance: float = 0.7,
               x1: float = -1, y1: float = -1,
               x2: float = -1, y2: float = -1) -> CapabilityResult:
        if self.mode == "real":
            x1, y1, x2, y2 = map(float, (x1, y1, x2, y2))
            if min(x1, y1, x2, y2) < 0:
                return self._err(Capability.SWIPE, "真机 WDA swipe 需要 x1/y1/x2/y2")
            result = self.wda.swipe(float(x1), float(y1), float(x2), float(y2))
            return self._ok(Capability.SWIPE, f"[real] WDA swipe 完成: {result}")
        args = ["--within-element-ref", within_element_ref, "--direction", direction,
                "--distance", str(distance)] if within_element_ref else []
        return self._sim_ui_action(Capability.SWIPE, "swipe", udid, args,
                                   required="within_element_ref")

    def _type_text(self, *, udid: str = "", element_ref: str = "",
                   text: str = "") -> CapabilityResult:
        if self.mode == "real":
            if not text:
                return self._err(Capability.TYPE_TEXT, "真机 type_text 需要 text")
            result = self.wda.type_text(text)
            return self._ok(Capability.TYPE_TEXT, f"[real] WDA 输入完成: {result}")
        args = ["--element-ref", element_ref, "--text", text] if element_ref and text else []
        return self._sim_ui_action(Capability.TYPE_TEXT, "type-text", udid, args,
                                   required="element_ref + text")

    def _sim_ui_action(self, capability: Capability, command: str, udid: str,
                       args: List[str], *, required: str) -> CapabilityResult:
        if not args:
            return self._err(capability, f"{command} 需要 {required}")
        udid = udid or self.config.get("sim_udid", "booted")
        out = self.runner.run(
            [self._xb(), "ui-automation", command, "--simulator-id", udid, *args,
             "--output", "text"],
            timeout=60,
        )
        ev, edir = self.writer.from_command(
            capability=capability.value,
            source=f"{self.platform_id}.xcodebuildmcp",
            out=out,
            summary=(f"{command} 成功" if out.ok() else f"{command} 失败"),
        )
        if out.ok():
            return self._ok(capability, f"[simulator] {command} 成功", edir, [ev.id])
        return self._err(capability, f"[simulator] {command} 失败", edir, [ev.id])

    # ---- PROBE ----
    def _probe(self, *, udid: str = "") -> CapabilityResult:
        """探测：设备/模拟器枚举 + 环境快照。"""
        if self.mode == "simulator":
            argv = [self._xb(), "simulator", "list", "--output", "text"]
        else:
            argv = [self._xb(), "device", "list", "--output", "text"]
        out = self.runner.run(argv, timeout=60)
        ev, edir = self.writer.from_command(capability="probe",
                                            source=f"{self.platform_id}.xcodebuildmcp", out=out,
                                            summary=("探测完成" if out.ok() else "探测失败"))
        if out.ok():
            return self._ok(Capability.PROBE, f"[{self.mode}] 探测完成", edir, [ev.id])
        return self._err(Capability.PROBE, f"[{self.mode}] 探测失败", edir, [ev.id])

    # ---- CRASH ----
    def _crash(self, *, udid: str = "", bundle_id: str = "") -> CapabilityResult:
        """采集本地 crash report（.ips/.crash）。

        模拟器：扫 ~/Library/Logs/DiagnosticReports（模拟器 crash 也落这里）。
        真机：xcrun devicectl device copy from 拉设备 crash 目录（需 device udid）。
        """
        edir = self.writer._dir("crash")
        if self.mode == "simulator":
            reports_dir = Path.home() / "Library" / "Logs" / "DiagnosticReports"
            if not reports_dir.exists():
                return self._err(Capability.CRASH, "[simulator] 无 DiagnosticReports 目录", str(edir))
            bundle = bundle_id or self.config.get("bundle_id", "")
            crashes = sorted(reports_dir.glob("*.ips"), key=lambda p: p.stat().st_mtime, reverse=True)
            if bundle:
                # 粗筛：文件名或内容含 bundle 短名
                short = bundle.split(".")[-1]
                crashes = [c for c in crashes if short.lower() in c.name.lower()] or crashes[:5]
            else:
                crashes = crashes[:5]
            if not crashes:
                return self._ok(Capability.CRASH, "[simulator] 近期无 crash report", str(edir))
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
            out = self.runner.run(
                ["xcrun", "devicectl", "device", "copy", "from", "--device", udid,
                 "--source", "/var/mobile/Library/Logs/CrashReporter", "--destination", str(edir)],
                timeout=300)
            ev, _ = self.writer.from_command(capability="crash", source=f"{self.platform_id}.real",
                                             out=out, summary=("真机 crash 已拉取" if out.ok() else "真机 crash 拉取失败"))
            if out.ok():
                return self._ok(Capability.CRASH, "[real] 真机 crash report 已拉取", str(edir), [ev.id])
            return self._err(Capability.CRASH, "[real] 真机 crash 拉取失败（设备权限/路径？）", str(edir), [ev.id])
