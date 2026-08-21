"""iOS 插件真命令行为测试 —— 用可注入 runner 验证命令拼装 + 成功判定。

本机没有完整 Xcode 也能验证：命令拼对了、成功判定对了、证据落盘了。
真机/真模拟器上换成默认 CommandRunner 即真跑（同一套代码路径）。
"""

from __future__ import annotations

import json
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kernel.capability import Capability, CapabilityStatus  # noqa: E402
from kernel.runner import CommandOutput  # noqa: E402
from plugins.ios_native import IOSNativePlugin  # noqa: E402
from plugins.ios_native.wda_manager import (  # noqa: E402
    WDAManager,
    WDA_COMMIT,
    WDA_REPOSITORY,
    WDA_VERSION,
)
from plugins.ios_native.evidence_writer import EvidenceWriter  # noqa: E402


class FakeRunner:
    """记录 argv、返回预设输出。不真跑，但走的是插件的真实命令拼装路径。"""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.stdout = stdout
        self.returncode = returncode

    def run(self, argv, *, timeout=600.0, cwd=None) -> CommandOutput:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.timeouts.append(timeout)
        return CommandOutput(argv, self.returncode, self.stdout, "", 0.01)


_checks: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    _checks.append((name, bool(cond)))


def test_sim_build_command_and_success_marker() -> None:
    with tempfile.TemporaryDirectory() as d:
        fake = FakeRunner(stdout="... \n** BUILD SUCCEEDED **\n", returncode=0)
        p = IOSNativePlugin("simulator", data_dir=d, runner=fake,
                            config={"workspace": "App.xcworkspace", "scheme": "App"})
        res = p.invoke(Capability.BUILD)
        argv = " ".join(fake.calls[0])
        check("build: 用了 XcodeBuildMCP", "xcodebuildmcp simulator build" in argv)
        check("build: 模拟器目标正确", "--simulator-id booted" in argv)
        check("build: 传了 scheme", "--scheme App" in argv)
        check("build: BUILD SUCCEEDED 判成功", res.status == CapabilityStatus.SUCCESS)
        check("build: 产出了证据", len(res.artifacts) == 1 and res.evidence_dir)


def test_build_exit0_without_marker_is_failure() -> None:
    # VDD：exit 0 不等于成功，没有 BUILD SUCCEEDED marker 判失败
    with tempfile.TemporaryDirectory() as d:
        fake = FakeRunner(stdout="warning: something\n", returncode=0)
        p = IOSNativePlugin("simulator", data_dir=d, runner=fake, config={"scheme": "App"})
        res = p.invoke(Capability.BUILD)
        check("build: exit0 但无 marker 判失败（不只看 exit code）",
              res.status == CapabilityStatus.ERROR)


def test_real_build_uses_xcodebuildmcp_device_workflow() -> None:
    with tempfile.TemporaryDirectory() as d:
        fake = FakeRunner(stdout="** BUILD SUCCEEDED **", returncode=0)
        p = IOSNativePlugin("real", data_dir=d, runner=fake,
                            config={"scheme": "App", "team_id": "ABCDE12345"})
        p.invoke(Capability.BUILD)
        argv = " ".join(fake.calls[0])
        check("real build: 走 XcodeBuildMCP device workflow",
              "xcodebuildmcp device build" in argv)
        check("real build: 传 scheme", "--scheme App" in argv)
        check("real build: 输出采用公开 CLI", "--output text" in argv)
        fake_with_device = FakeRunner(
            stdout="** BUILD SUCCEEDED **",
            returncode=0,
        )
        plugin_with_device = IOSNativePlugin(
            "real",
            data_dir=d,
            runner=fake_with_device,
            config={
                "scheme": "App",
                "project": "App.xcodeproj",
                "device_udid": "DEVICE-1",
            },
        )
        plugin_with_device.invoke(Capability.BUILD)
        check(
            "real build: device workflow 不传不支持的 --device-id",
            "--device-id" not in fake_with_device.calls[0],
        )


def test_sim_install_launch_screenshot_commands() -> None:
    with tempfile.TemporaryDirectory() as d:
        fake = FakeRunner(stdout="ok", returncode=0)
        p = IOSNativePlugin("simulator", data_dir=d, runner=fake,
                            config={"app_path": "/x/App.app", "bundle_id": "com.x.app", "sim_udid": "booted"})
        p.invoke(Capability.INSTALL)
        p.invoke(Capability.LAUNCH)
        install_argv = " ".join(fake.calls[0])
        launch_argv = " ".join(fake.calls[1])
        check("install: XcodeBuildMCP simulator install 命令正确",
              "xcodebuildmcp simulator install --simulator-id booted --app-path /x/App.app" in install_argv)
        check("launch: XcodeBuildMCP launch-app 命令正确",
              "xcodebuildmcp simulator launch-app --simulator-id booted --bundle-id com.x.app" in launch_argv)


def test_probe_allows_slow_device_discovery() -> None:
    with tempfile.TemporaryDirectory() as d:
        fake = FakeRunner(stdout="device list", returncode=0)
        plugin = IOSNativePlugin("real", data_dir=d, runner=fake)
        result = plugin.invoke(Capability.PROBE)
        check(
            "probe: 设备发现默认允许 120 秒",
            result.status == CapabilityStatus.SUCCESS
            and fake.timeouts == [120],
        )
        counter = plugin.invoke(
            Capability.COUNTER_PROBE,
            counter_capability="probe",
            counter_condition="use a different device condition",
            counter_expect="summary_contains:探测完成",
        )
        check(
            "counter_probe: 明确执行反证条件并落独立产物",
            counter.status == CapabilityStatus.SUCCESS
            and (Path(counter.evidence_dir) / "counter-result.json").is_file(),
        )
        mismatch = plugin.invoke(
            Capability.COUNTER_PROBE,
            counter_capability="probe",
            counter_condition="use another condition",
            counter_expect="summary_contains:never-matches",
        )
        check("counter_probe: 反证断言不匹配时不能过关",
              mismatch.status == CapabilityStatus.ERROR)


def test_sim_view_tree_and_ui_actions_use_xcodebuildmcp() -> None:
    with tempfile.TemporaryDirectory() as d:
        fake = FakeRunner(stdout='{"screen":{"hash":"x"},"targets":[]}', returncode=0)
        p = IOSNativePlugin("simulator", data_dir=d, runner=fake,
                            config={"sim_udid": "SIM-1"})
        tree = p.invoke(Capability.VIEW_TREE)
        tap = p.invoke(Capability.TAP, element_ref="e1")
        swipe = p.invoke(Capability.SWIPE, within_element_ref="e7",
                         direction="up", distance=0.7)
        typed = p.invoke(Capability.TYPE_TEXT, element_ref="e8", text="hello")
        commands = [" ".join(call) for call in fake.calls]
        check("UI: view_tree 是真实 snapshot-ui",
              "xcodebuildmcp simulator snapshot-ui --simulator-id SIM-1" in commands[0]
              and tree.status == CapabilityStatus.SUCCESS)
        check("UI: tap 使用 elementRef", "--element-ref e1" in commands[1]
              and tap.status == CapabilityStatus.SUCCESS)
        check("UI: swipe 使用 withinElementRef", "--within-element-ref e7" in commands[2]
              and swipe.status == CapabilityStatus.SUCCESS)
        check("UI: type_text 使用 elementRef", "--element-ref e8 --text hello" in commands[3]
              and typed.status == CapabilityStatus.SUCCESS)


def test_sim_ui_action_refreshes_expired_snapshot_once() -> None:
    class ExpiredRunner:
        developer_dir = "/Applications/Xcode.app/Contents/Developer"

        def __init__(self):
            self.calls = []

        def run(self, argv, *, timeout=600.0, cwd=None):
            argv = [str(item) for item in argv]
            self.calls.append(argv)
            if len(self.calls) == 1:
                return CommandOutput(
                    argv, 1, "Code: SNAPSHOT_EXPIRED", "", 0.01
                )
            if len(self.calls) == 2:
                return CommandOutput(
                    argv,
                    0,
                    json.dumps({
                        "didError": False,
                        "data": {
                            "capture": {
                                "targets": [
                                    "e9|tap|button|Count: 0||iloop-counter"
                                ],
                            },
                        },
                    }),
                    "",
                    0.01,
                )
            return CommandOutput(argv, 0, '{"didError":false}', "", 0.01)

    with tempfile.TemporaryDirectory() as d:
        runner = ExpiredRunner()
        plugin = IOSNativePlugin(
            "simulator",
            data_dir=d,
            runner=runner,
            config={"sim_udid": "SIM-1"},
        )
        plugin._snapshot_state_path("SIM-1").write_text(
            json.dumps({
                "udid": "SIM-1",
                "targets": {
                    "e8": "tap|button|Count: 0||iloop-counter",
                },
            }),
            encoding="utf-8",
        )
        result = plugin.invoke(Capability.TAP, element_ref="e8")
        commands = [" ".join(call) for call in runner.calls]
        check(
            "UI: snapshot 缺失或过期时自动刷新并重试一次",
            result.status == CapabilityStatus.SUCCESS
            and len(commands) == 3
            and "simulator snapshot-ui" in commands[1]
            and "ui-automation tap" in commands[2]
            and "--element-ref e9" in commands[2],
        )


def test_sim_ui_action_rejects_ambiguous_snapshot_rebind() -> None:
    class AmbiguousRunner:
        developer_dir = "/Applications/Xcode.app/Contents/Developer"

        def __init__(self):
            self.calls = []

        def run(self, argv, *, timeout=600.0, cwd=None):
            argv = [str(item) for item in argv]
            self.calls.append(argv)
            if len(self.calls) == 1:
                return CommandOutput(
                    argv, 1, "Code: SNAPSHOT_EXPIRED", "", 0.01
                )
            return CommandOutput(
                argv,
                0,
                json.dumps({
                    "didError": False,
                    "data": {
                        "capture": {
                            "targets": [
                                "e9|tap|button|Delete||delete-button",
                                "e10|tap|button|Delete||delete-button",
                            ],
                        },
                    },
                }),
                "",
                0.01,
            )

    with tempfile.TemporaryDirectory() as d:
        runner = AmbiguousRunner()
        plugin = IOSNativePlugin(
            "simulator",
            data_dir=d,
            runner=runner,
            config={"sim_udid": "SIM-1"},
        )
        plugin._snapshot_state_path("SIM-1").write_text(
            json.dumps({
                "udid": "SIM-1",
                "targets": {
                    "e8": "tap|button|Delete||delete-button",
                },
            }),
            encoding="utf-8",
        )
        result = plugin.invoke(Capability.TAP, element_ref="e8")
        check(
            "UI: 重复语义签名拒绝静默重绑",
            result.status == CapabilityStatus.ERROR
            and len(runner.calls) == 2,
        )


def test_snapshot_rebind_does_not_replace_business_text() -> None:
    class TextRunner:
        developer_dir = "/Applications/Xcode.app/Contents/Developer"

        def __init__(self):
            self.calls = []

        def run(self, argv, *, timeout=600.0, cwd=None):
            argv = [str(item) for item in argv]
            self.calls.append(argv)
            if len(self.calls) == 1:
                return CommandOutput(
                    argv, 1, "Code: SNAPSHOT_EXPIRED", "", 0.01
                )
            if len(self.calls) == 2:
                return CommandOutput(
                    argv,
                    0,
                    json.dumps({
                        "didError": False,
                        "data": {
                            "capture": {
                                "targets": [
                                    "e9|type|text-field|Name||name-field"
                                ],
                            },
                        },
                    }),
                    "",
                    0.01,
                )
            return CommandOutput(argv, 0, "typed", "", 0.01)

    with tempfile.TemporaryDirectory() as d:
        runner = TextRunner()
        plugin = IOSNativePlugin(
            "simulator",
            data_dir=d,
            runner=runner,
            config={"sim_udid": "SIM-1"},
        )
        plugin._snapshot_state_path("SIM-1").write_text(
            json.dumps({
                "udid": "SIM-1",
                "targets": {
                    "e8": "type|text-field|Name||name-field",
                },
            }),
            encoding="utf-8",
        )
        result = plugin.invoke(
            Capability.TYPE_TEXT,
            element_ref="e8",
            text="e8",
        )
        retry = runner.calls[-1]
        check(
            "UI: snapshot 重绑只替换 ref 参数不改业务文本",
            result.status == CapabilityStatus.SUCCESS
            and retry[retry.index("--element-ref") + 1] == "e9"
            and retry[retry.index("--text") + 1] == "e8",
        )


def test_injected_runner_developer_dir_matches_doctor() -> None:
    with tempfile.TemporaryDirectory() as d:
        runner = FakeRunner()
        runner.environment_overrides = {
            "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer",
        }
        plugin = IOSNativePlugin(
            "simulator",
            data_dir=d,
            runner=runner,
        )
        check(
            "doctor: 注入 runner 的 DEVELOPER_DIR 参与环境判定",
            plugin.developer_dir
            == "/Applications/Xcode.app/Contents/Developer",
        )


def test_real_device_needs_udid() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = IOSNativePlugin("real", data_dir=d, runner=FakeRunner(),
                            config={"app_path": "/x/App.app"})
        res = p.invoke(Capability.INSTALL)  # 没给 device udid
        check("real install: 缺 udid 时报错而非乱跑", res.status == CapabilityStatus.ERROR and "udid" in res.summary)


def test_crash_now_implemented() -> None:
    # crash 已实现本地采集：应在声明能力里，不再是 unsupported
    p = IOSNativePlugin("simulator", data_dir="/tmp/iloop-ios-test", runner=FakeRunner())
    check("crash: 已纳入声明能力集", Capability.CRASH in p.capabilities())
    res = p.invoke(Capability.CRASH)
    # 本机可能无 crash report，但不应是 unsupported（要么 success 要么 error，都算已实现）
    check("crash: 不再返回 unsupported", res.status != CapabilityStatus.UNSUPPORTED)


def test_real_crash_needs_udid() -> None:
    p = IOSNativePlugin(
        "real",
        data_dir="/tmp/iloop-ios-test",
        runner=FakeRunner(),
        config={"bundle_id": "dev.iloop.test"},
    )
    res = p.invoke(Capability.CRASH)
    check("crash: 真机采集缺 udid 时报错而非乱跑",
          res.status == CapabilityStatus.ERROR and "udid" in res.summary)


def test_plugin_never_crashes_kernel() -> None:
    # runner 抛异常也不能让插件崩，必须转成 error 结果
    class BoomRunner:
        def run(self, *a, **k):
            raise RuntimeError("boom")
    p = IOSNativePlugin("simulator", data_dir="/tmp/x", runner=BoomRunner(), config={"scheme": "App"})
    res = p.invoke(Capability.BUILD)
    check("鲁棒: 底层异常转成 error 而非崩溃", res.status == CapabilityStatus.ERROR and "boom" in res.summary)


def test_xcodebuildmcp_json_artifact_path() -> None:
    with tempfile.TemporaryDirectory() as d:
        png = Path(d) / "path with spaces.png"
        png.write_bytes(b"png")
        payload = '{"artifacts":[{"path":' + repr(str(png)).replace("'", '"') + '}]}'
        found = IOSNativePlugin._artifact_path(payload, ".png")
        check("截图: 从 XcodeBuildMCP JSON 解析含空格产物路径", found == png)
    home_path = "~/.iloop/runtime.log"
    with mock.patch.object(Path, "exists", return_value=True):
        found = IOSNativePlugin._artifact_path(
            f"Runtime Logs: {home_path}",
            ".log",
        )
    check(
        "运行日志: 展开 XcodeBuildMCP 返回的 home 相对路径",
        found == Path.home() / ".iloop/runtime.log",
    )
    large_output = ("compile output /tmp/object.o\n" * 10000) + (
        "Runtime Logs: ~/.iloop/runtime.log\n"
    )
    with mock.patch.object(Path, "exists", return_value=True):
        found = IOSNativePlugin._artifact_path(large_output, ".log")
    check(
        "运行日志: 大体积构建输出路径解析保持线性",
        found == Path.home() / ".iloop/runtime.log",
    )


def test_screenshot_accepts_structured_success_and_keeps_evidence_dir() -> None:
    with tempfile.TemporaryDirectory() as d:
        jpg = Path(d) / "shot result.jpg"
        jpg.write_bytes(b"jpeg")
        payload = json.dumps({
            "didError": False,
            "data": {
                "summary": {"status": "SUCCEEDED"},
                "artifacts": {"screenshotPath": str(jpg)},
            },
        })
        fake = FakeRunner(stdout=payload, returncode=1)
        plugin = IOSNativePlugin(
            "simulator",
            data_dir=d,
            runner=fake,
            config={"sim_udid": "SIM-1"},
        )
        result = plugin.invoke(Capability.SCREENSHOT)
        copied = list(Path(result.evidence_dir).glob("shot.jpg"))
        check(
            "截图: 结构化成功产物不被宿主尾部非零覆盖",
            result.status == CapabilityStatus.SUCCESS
            and len(copied) == 1
            and fake.timeouts == [120],
        )


def test_evidence_directories_do_not_collide() -> None:
    with tempfile.TemporaryDirectory() as d:
        writer = EvidenceWriter(d)
        first = writer._dir("tap")
        second = writer._dir("tap")
        check(
            "证据: 同秒重复能力调用使用不同目录",
            first != second and first.is_dir() and second.is_dir(),
        )
        output = CommandOutput(["build"], 0, "warning only", "", 0.01)
        evidence, _ = writer.from_command(
            capability="build",
            source="test",
            out=output,
            summary="marker missing",
            outcome="failure",
        )
        check(
            "证据: 语义失败不会被 exit 0 写成 success",
            evidence.outcome == "failure",
        )


def test_runtime_logs_are_bound_to_latest_run() -> None:
    with tempfile.TemporaryDirectory() as d:
        runtime_log = Path(d) / "App runtime.log"
        runtime_log.write_text("line1\nline2\n", encoding="utf-8")
        fake = FakeRunner(
            stdout=f"Build succeeded. App launched. Runtime log: {runtime_log}\n",
            returncode=0,
        )
        config = {"scheme": "App", "sim_udid": "SIM-1"}
        plugin = IOSNativePlugin("simulator", data_dir=d, runner=fake, config=config)
        run = plugin.invoke(Capability.RUN, task_id="task-a", run_id="run-a")
        run_state = json.loads(
            (plugin.state_dir / "last-run-task-a.json").read_text(
                encoding="utf-8"
            )
        )
        logs = plugin.invoke(Capability.LOGS, task_id="task-a", run_id="run-a")
        stale_run_logs = plugin.invoke(
            Capability.LOGS, task_id="task-a", run_id="run-b"
        )
        other_logs = plugin.invoke(
            Capability.LOGS, task_id="task-b", run_id="run-a"
        )
        fake.stdout = "build failed"
        fake.returncode = 1
        failed_run = plugin.invoke(
            Capability.RUN, task_id="task-a", run_id="run-b"
        )
        failed_run_logs = plugin.invoke(
            Capability.LOGS, task_id="task-a", run_id="run-b"
        )
        same_id_failed_run = plugin.invoke(
            Capability.RUN, task_id="task-a", run_id="run-a"
        )
        same_id_logs = plugin.invoke(
            Capability.LOGS, task_id="task-a", run_id="run-a"
        )
        check("动态日志: run 成功后记录本次日志路径", run.status == CapabilityStatus.SUCCESS)
        check("动态日志: run 窗口从 build-and-run 调用前开始",
              run_state["started_at"] <= run_state["captured_at"])
        check("动态日志: logs 只读取本次 run 绑定文件",
              logs.status == CapabilityStatus.SUCCESS and "2 行" in logs.summary)
        check("动态日志: 其他 Task 不能复用上一任务日志",
              other_logs.status == CapabilityStatus.ERROR)
        check("动态日志: 同一 Task 的其他 run 不能复用成功日志",
              stale_run_logs.status == CapabilityStatus.ERROR)
        check("动态日志: 失败 run 不会覆盖或产出可用日志绑定",
              failed_run.status == CapabilityStatus.ERROR
              and failed_run_logs.status == CapabilityStatus.ERROR)
        check("动态日志: 同 run_id 后续失败会清理旧成功绑定",
              same_id_failed_run.status == CapabilityStatus.ERROR
              and same_id_logs.status == CapabilityStatus.ERROR)
        fake.stdout = f"Build succeeded. App launched. Runtime log: {runtime_log}\n"
        fake.returncode = 0
        plugin.invoke(Capability.RUN, task_id="task-c", run_id="run-c")
        empty = plugin.invoke(
            Capability.LOGS,
            task_id="task-c",
            run_id="run-c",
            predicate="NEVER_PRESENT",
        )
        check("动态日志: predicate 零命中不算成功",
              empty.status == CapabilityStatus.ERROR)


def test_real_ui_actions_write_durable_evidence() -> None:
    class FakeWDA:
        base = "http://127.0.0.1:8100"

        def tap(self, x, y):
            return {"value": None}

    with tempfile.TemporaryDirectory() as d:
        plugin = IOSNativePlugin(
            "real",
            data_dir=d,
            runner=FakeRunner(),
            config={"device_udid": "DEVICE-1"},
            wda=FakeWDA(),
        )
        manager = mock.Mock()
        manager.status.return_value = {"ready": True}
        with mock.patch.object(plugin, "_wda_manager", return_value=manager):
            result = plugin.invoke(Capability.TAP, x=10, y=20)
        check(
            "真机 UI: 成功动作写入可哈希响应证据",
            result.status == CapabilityStatus.SUCCESS
            and bool(result.evidence_dir)
            and (Path(result.evidence_dir) / "wda-response.json").is_file(),
        )


def test_sim_crash_never_falls_back_to_other_app() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        reports = home / "Library" / "Logs" / "DiagnosticReports"
        reports.mkdir(parents=True)
        other = reports / "OtherApp.ips"
        other.write_text("bundle_id: com.other.app", encoding="utf-8")
        plugin = IOSNativePlugin(
            "simulator",
            data_dir=Path(d) / "data",
            runner=FakeRunner(),
            config={"bundle_id": "com.target.app"},
        )
        with mock.patch.object(Path, "home", return_value=home):
            result = plugin.invoke(
                Capability.CRASH,
                bundle_id="com.target.app",
                since_seconds=3600,
            )
            unbound = plugin.invoke(
                Capability.CRASH,
                bundle_id="com.target.app",
                task_id="task-without-run",
                run_id="missing-run",
            )
            state_path = plugin.state_dir / "last-run-task-bound.json"
            state_path.write_text(json.dumps({
                "status": "success",
                "task_id": "task-bound",
                "run_id": "run-bound",
                "mode": "simulator",
                "bundle_id": "com.other.app",
                "device_id": "booted",
                "captured_at": __import__("time").time(),
            }), encoding="utf-8")
            wrong_subject = plugin.invoke(
                Capability.CRASH,
                bundle_id="com.target.app",
                task_id="task-bound",
                run_id="run-bound",
            )
        copied = list(Path(result.evidence_dir).glob("OtherApp.ips"))
        check(
            "crash: 目标 App 无报告时不回退其他 App",
            result.status == CapabilityStatus.SUCCESS
            and not copied
            and (Path(result.evidence_dir) / "absence.json").is_file(),
        )
        check("crash: Task 采集必须绑定前序成功 run",
              unbound.status == CapabilityStatus.ERROR)
        check("crash: run 绑定同时校验 task/bundle/device",
              wrong_subject.status == CapabilityStatus.ERROR)
        real_plugin = IOSNativePlugin(
            "real",
            data_dir=Path(d) / "real-data",
            runner=FakeRunner(stdout="copy complete", returncode=0),
            config={
                "bundle_id": "com.target.app",
                "device_udid": "DEVICE-1",
            },
        )
        no_real_crash = real_plugin.invoke(
            Capability.CRASH,
            bundle_id="com.target.app",
            udid="DEVICE-1",
            since_seconds=3600,
        )
        check(
            "crash: 真机查询成功且无目标报告时产出 absence 证据",
            no_real_crash.status == CapabilityStatus.SUCCESS
            and (Path(no_real_crash.evidence_dir) / "absence.json").is_file(),
        )


def test_wda_actions_share_managed_endpoint() -> None:
    with tempfile.TemporaryDirectory() as d:
        plugin = IOSNativePlugin(
            "real",
            data_dir=d,
            runner=FakeRunner(),
            config={"device_udid": "DEVICE-1", "wda_port": 8200},
        )
        manager = plugin._wda_manager()
        check("WDA: 健康检查和实际动作共享同一自定义端点",
              manager.client is plugin.wda
              and plugin.wda.base == "http://127.0.0.1:8200")


def test_wda_manager_uses_pinned_source_and_managed_command() -> None:
    with tempfile.TemporaryDirectory() as d:
        manager = WDAManager(
            d, device_udid="DEVICE-1", team_id="TEAM123",
            xcodebuildmcp="/usr/local/bin/xcodebuildmcp",
        )
        manager._process_identity = lambda pid: {
            "sha256": f"process-{pid}",
            "command": f"fake process {pid}",
        }
        manager._source_identity = lambda: {
            "tag": WDA_VERSION,
            "commit": WDA_COMMIT,
            "origin": WDA_REPOSITORY,
            "worktree_valid": True,
        }
        project = Path(d) / WDA_VERSION / "WebDriverAgent.xcodeproj"
        project.mkdir(parents=True)
        block = (
            "\t\tABCDEF1234567890ABCDEF12 /* {name} */ = {{\n"
            "\t\t\tbaseConfigurationReference = IOSSettings.xcconfig;\n"
            "\t\t\tbuildSettings = {{\n"
            "\t\t\t\tINFOPLIST_FILE = WebDriverAgentRunner/Info.plist;\n"
            "\t\t\t\tUSES_XCTRUNNER = YES;\n"
            "\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = WebDriverAgentRunner;\n"
            "\t\t\t}};\n"
            "\t\t}};\n"
        )
        (project / "project.pbxproj").write_text(
            block.format(name="Debug") + block.replace(
                "ABCDEF1234567890ABCDEF12", "ABCDEF1234567890ABCDEF13"
            ).format(name="Release"),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=manager.source, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=manager.source, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=manager.source, check=True)
        subprocess.run(["git", "add", "."], cwd=manager.source, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=manager.source, check=True)
        command = manager.command()
        payload = " ".join(command)
        check("WDA: 使用固定公开版本源码目录", WDA_VERSION in payload)
        check("WDA: 通过 XcodeBuildMCP device test 启动",
              command[:3] == ["/usr/local/bin/xcodebuildmcp", "device", "test"])
        check("WDA: 命令包含目标设备", "DEVICE-1" in payload)
        project_text = (project / "project.pbxproj").read_text(encoding="utf-8")
        check("WDA: 签名只补到 runner 配置而非全局 extraArgs",
              project_text.count("ILOOP_WDA_SIGNING_BEGIN") == 2
              and "DEVELOPMENT_TEAM=TEAM123" not in payload)
        fake_manager = WDAManager(Path(d) / "fake")
        fake_project = (
            Path(d) / "fake" / WDA_VERSION / "WebDriverAgent.xcodeproj"
        )
        fake_project.mkdir(parents=True)
        fake_source_rejected = False
        try:
            fake_manager.install_source()
        except RuntimeError:
            fake_source_rejected = True
        check("WDA: 仅伪造 xcodeproj 目录不能冒充固定官方源码",
              fake_source_rejected)
        dirty_root = Path(d) / "dirty"
        dirty_manager = WDAManager(dirty_root, team_id="TEAM123")
        dirty_source = dirty_root / WDA_VERSION
        (dirty_source / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        (dirty_source / "WebDriverAgent.xcodeproj" / "project.pbxproj").write_text(
            "base\n", encoding="utf-8"
        )
        (dirty_source / "source.m").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=dirty_source, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=dirty_source, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=dirty_source, check=True)
        subprocess.run(["git", "remote", "add", "origin", WDA_REPOSITORY],
                       cwd=dirty_source, check=True)
        subprocess.run(["git", "add", "."], cwd=dirty_source, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=dirty_source, check=True)
        subprocess.run(["git", "tag", WDA_VERSION], cwd=dirty_source, check=True)
        (dirty_source / "source.m").write_text("malicious\n", encoding="utf-8")
        check("WDA: 固定 tag/commit 元数据不能掩盖 dirty 源文件",
              dirty_manager._source_identity()["worktree_valid"] is False)


def test_wda_prepare_failure_cleans_processes() -> None:
    class FakeProcess:
        _next_pid = 100
        def __init__(self):
            self.pid = FakeProcess._next_pid
            FakeProcess._next_pid += 1
            self.terminated = False
        def poll(self):
            return 0 if self.terminated else None
        def terminate(self):
            self.terminated = True
        def wait(self, timeout=None):
            return 0
        def kill(self):
            self.terminated = True

    with tempfile.TemporaryDirectory() as d:
        manager = WDAManager(
            d, device_udid="DEVICE-1", team_id="TEAM123",
            xcodebuildmcp="/usr/local/bin/xcodebuildmcp",
        )
        manager._process_identity = lambda pid: {
            "sha256": f"process-{pid}",
            "command": f"fake process {pid}",
        }
        manager._source_identity = lambda: {
            "tag": WDA_VERSION,
            "commit": WDA_COMMIT,
            "origin": WDA_REPOSITORY,
            "worktree_valid": True,
        }
        manager._configure_runner_signing = lambda: None
        project = Path(d) / WDA_VERSION / "WebDriverAgent.xcodeproj"
        project.mkdir(parents=True)
        block = (
            "\t\tABCDEF1234567890ABCDEF12 /* {name} */ = {{\n"
            "\t\t\tbaseConfigurationReference = IOSSettings.xcconfig;\n"
            "\t\t\tbuildSettings = {{\n"
            "\t\t\t\tINFOPLIST_FILE = WebDriverAgentRunner/Info.plist;\n"
            "\t\t\t\tUSES_XCTRUNNER = YES;\n"
            "\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = WebDriverAgentRunner;\n"
            "\t\t\t}};\n\t\t}};\n"
        )
        (project / "project.pbxproj").write_text(
            block.format(name="Debug") + block.replace(
                "ABCDEF1234567890ABCDEF12", "ABCDEF1234567890ABCDEF13"
            ).format(name="Release"),
            encoding="utf-8",
        )
        manager.install_source = lambda: project
        processes = [FakeProcess(), FakeProcess()]
        with mock.patch.object(manager, "status", return_value={"ready": False}), \
             mock.patch("plugins.ios_native.wda_manager.shutil.which", return_value="/usr/bin/iproxy"), \
             mock.patch("plugins.ios_native.wda_manager.subprocess.Popen", side_effect=processes):
            failed = False
            try:
                manager.prepare(timeout=0)
            except RuntimeError:
                failed = True
        check("WDA: prepare 超时明确失败", failed)
        check("WDA: prepare 失败清理 runner 与 iproxy",
              all(process.terminated for process in processes))
        state_processes = [FakeProcess(), FakeProcess()]
        with mock.patch.object(manager, "status", return_value={"ready": False}), \
             mock.patch.object(manager, "_write_state", side_effect=OSError("disk full")), \
             mock.patch("plugins.ios_native.wda_manager.shutil.which", return_value="/usr/bin/iproxy"), \
             mock.patch("plugins.ios_native.wda_manager.subprocess.Popen", side_effect=state_processes):
            state_failed = False
            try:
                manager.prepare(timeout=10)
            except OSError:
                state_failed = True
        check("WDA: state 落盘失败向上返回", state_failed)
        check("WDA: state 落盘失败同样清理两个进程",
              all(process.terminated for process in state_processes))
        interrupted_processes = [FakeProcess(), FakeProcess()]
        with mock.patch.object(
            manager, "status",
            side_effect=[{"ready": False}, KeyboardInterrupt()],
        ), mock.patch(
            "plugins.ios_native.wda_manager.shutil.which",
            return_value="/usr/bin/iproxy",
        ), mock.patch(
            "plugins.ios_native.wda_manager.subprocess.Popen",
            side_effect=interrupted_processes,
        ):
            interrupted = False
            try:
                manager.prepare(timeout=10)
            except KeyboardInterrupt:
                interrupted = True
        check("WDA: 等待阶段中断向上返回", interrupted)
        check("WDA: KeyboardInterrupt 也清理两个进程",
              all(process.terminated for process in interrupted_processes))
        old_device_processes = [FakeProcess(), FakeProcess()]
        with mock.patch.object(
            manager, "status",
            return_value={
                "ready": True,
                "runtime": {"device_udid": "OLD-DEVICE", "version": WDA_VERSION},
            },
        ), mock.patch.object(
            manager,
            "_stop_locked",
            return_value={"stopped": [1, 2], "unresolved": []},
        ) as stop_mock, \
             mock.patch("plugins.ios_native.wda_manager.shutil.which", return_value="/usr/bin/iproxy"), \
             mock.patch("plugins.ios_native.wda_manager.subprocess.Popen", side_effect=old_device_processes):
            try:
                manager.prepare(timeout=0)
            except RuntimeError:
                pass
        check("WDA: 切换设备时不会复用旧设备 ready 状态", stop_mock.called)
        with mock.patch.object(
            manager,
            "status",
            return_value={
                "ready": False,
                "runtime": {"device_udid": "OLD-DEVICE", "version": WDA_VERSION},
            },
        ), mock.patch.object(
            manager,
            "_stop_locked",
            return_value={"stopped": [], "unresolved": ["wda_pid"]},
        ):
            ownership_blocked = False
            try:
                manager.prepare(timeout=0)
            except RuntimeError as error:
                ownership_blocked = "ownership is unresolved" in str(error)
        check("WDA: 旧进程所有权未确认时拒绝启动替代实例",
              ownership_blocked)
        startup_process = FakeProcess()
        with mock.patch.object(manager, "status", return_value={"ready": False}), \
             mock.patch("plugins.ios_native.wda_manager.shutil.which", return_value="/usr/bin/iproxy"), \
             mock.patch(
                 "plugins.ios_native.wda_manager.subprocess.Popen",
                 side_effect=[startup_process, KeyboardInterrupt()],
             ):
            startup_interrupted = False
            try:
                manager.prepare(timeout=10)
            except KeyboardInterrupt:
                startup_interrupted = True
        check("WDA: 第二进程启动中断向上返回", startup_interrupted)
        check("WDA: 第二进程启动中断清理已启动 runner", startup_process.terminated)


def test_wda_cleanup_targets_process_group() -> None:
    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("process-group cleanup should be preferred")

        def kill(self):
            raise AssertionError("process-group cleanup should be preferred")

    signals = []
    with mock.patch(
        "plugins.ios_native.wda_manager.os.killpg",
        side_effect=lambda pid, signal_value: signals.append((pid, signal_value)),
    ):
        WDAManager._terminate_process(FakeProcess())
    check(
        "WDA: cleanup terminates the dedicated process group",
        signals == [(4321, __import__("signal").SIGTERM), (4321, __import__("signal").SIGKILL)],
    )
    with tempfile.TemporaryDirectory() as d:
        manager = WDAManager(d)
        state = {
            "wda_pid": 5001,
            "proxy_pid": 5002,
            "wda_identity_sha256": "old",
            "proxy_identity_sha256": "old",
        }
        with mock.patch.object(manager, "_read_state", return_value=state), \
             mock.patch.object(manager, "_pid_matches", return_value=False), \
             mock.patch.object(manager, "_pid_exists", return_value=False):
            stopped = manager.stop()
        check(
            "WDA: 已死亡 PID 不会被误判为所有权冲突",
            stopped["unresolved"] == [],
        )


def test_wda_status_binds_endpoint_to_managed_processes() -> None:
    with tempfile.TemporaryDirectory() as d:
        manager = WDAManager(d, device_udid="DEVICE-1", team_id="TEAM123")
        state = {
            "wda_pid": 101,
            "proxy_pid": 102,
            "wda_identity_sha256": "wda-sha",
            "proxy_identity_sha256": "proxy-sha",
            "device_udid": "DEVICE-1",
            "version": WDA_VERSION,
            "local_port": 8100,
            "source_commit": WDA_COMMIT,
        }
        with mock.patch.object(manager, "_read_state", return_value=state), \
             mock.patch.object(manager.client, "status", return_value={"value": {"ready": True}}), \
             mock.patch.object(manager, "_pid_matches", side_effect=[True, True]), \
             mock.patch.object(manager, "_source_identity",
                               return_value={"tag": WDA_VERSION, "commit": WDA_COMMIT,
                                             "origin": WDA_REPOSITORY,
                                             "worktree_valid": True}), \
             mock.patch.object(manager, "_port_owner_pid", return_value=102):
            ready = manager.status()
        with mock.patch.object(manager, "_read_state", return_value=state), \
             mock.patch.object(manager.client, "status", return_value={"value": {"ready": True}}), \
             mock.patch.object(manager, "_pid_matches", side_effect=[False, True]), \
             mock.patch.object(manager, "_source_identity",
                               return_value={"tag": WDA_VERSION, "commit": WDA_COMMIT,
                                             "origin": WDA_REPOSITORY,
                                             "worktree_valid": True}), \
             mock.patch.object(manager, "_port_owner_pid", return_value=102):
            stale = manager.status()
        with mock.patch.object(manager, "_read_state", return_value=state), \
             mock.patch.object(manager.client, "status", return_value={"value": {"ready": True}}), \
             mock.patch.object(manager, "_pid_matches", side_effect=[True, True]), \
             mock.patch.object(manager, "_source_identity",
                               return_value={"tag": WDA_VERSION, "commit": WDA_COMMIT,
                                             "origin": WDA_REPOSITORY,
                                             "worktree_valid": True}), \
             mock.patch.object(manager, "_port_owner_pid", return_value=999):
            wrong_listener = manager.status()
        with mock.patch.object(manager, "_read_state", return_value=state), \
             mock.patch.object(manager.client, "status", return_value={"value": {"ready": True}}), \
             mock.patch.object(manager, "_pid_matches", side_effect=[True, True]), \
             mock.patch.object(manager, "_source_identity",
                               return_value={"tag": WDA_VERSION, "commit": "evil-commit",
                                             "origin": WDA_REPOSITORY,
                                             "worktree_valid": True}), \
             mock.patch.object(manager, "_port_owner_pid", return_value=102):
            wrong_commit = manager.status()
        with mock.patch.object(manager, "_read_state", return_value=state), \
             mock.patch.object(manager.client, "status", return_value={"value": {"ready": True}}), \
             mock.patch.object(manager, "_pid_matches", side_effect=[True, True]), \
             mock.patch.object(manager, "_source_identity",
                               return_value={"tag": WDA_VERSION, "commit": WDA_COMMIT,
                                             "origin": WDA_REPOSITORY,
                                             "worktree_valid": False}), \
             mock.patch.object(manager, "_port_owner_pid", return_value=102):
            dirty_source = manager.status()
        check("WDA: endpoint 与托管 runner/proxy 同时存活才 ready", ready["ready"])
        check("WDA: endpoint 存活但托管进程不匹配时拒绝 ready", not stale["ready"])
        check("WDA: 监听端口不属于托管 iproxy 时拒绝 ready",
              not wrong_listener["ready"])
        check("WDA: 同名 tag 但非固定 upstream commit 时拒绝 ready",
              not wrong_commit["ready"])
        check("WDA: 固定 commit 但 worktree 被额外修改时拒绝 ready",
              not dirty_source["ready"])


def run() -> int:
    for fn in [
        test_sim_build_command_and_success_marker,
        test_build_exit0_without_marker_is_failure,
        test_real_build_uses_xcodebuildmcp_device_workflow,
        test_sim_install_launch_screenshot_commands,
        test_probe_allows_slow_device_discovery,
        test_sim_view_tree_and_ui_actions_use_xcodebuildmcp,
        test_sim_ui_action_refreshes_expired_snapshot_once,
        test_sim_ui_action_rejects_ambiguous_snapshot_rebind,
        test_snapshot_rebind_does_not_replace_business_text,
        test_injected_runner_developer_dir_matches_doctor,
        test_real_device_needs_udid,
        test_crash_now_implemented,
        test_real_crash_needs_udid,
        test_plugin_never_crashes_kernel,
        test_xcodebuildmcp_json_artifact_path,
        test_screenshot_accepts_structured_success_and_keeps_evidence_dir,
        test_evidence_directories_do_not_collide,
        test_runtime_logs_are_bound_to_latest_run,
        test_real_ui_actions_write_durable_evidence,
        test_sim_crash_never_falls_back_to_other_app,
        test_wda_actions_share_managed_endpoint,
        test_wda_manager_uses_pinned_source_and_managed_command,
        test_wda_prepare_failure_cleans_processes,
        test_wda_cleanup_targets_process_group,
        test_wda_status_binds_endpoint_to_managed_processes,
    ]:
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    for name, ok in _checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n【iLoop】✅ ios plugin selftest: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(run())
