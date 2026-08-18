"""iOS 插件真命令行为测试 —— 用可注入 runner 验证命令拼装 + 成功判定。

本机没有完整 Xcode 也能验证：命令拼对了、成功判定对了、证据落盘了。
真机/真模拟器上换成默认 CommandRunner 即真跑（同一套代码路径）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kernel.capability import Capability, CapabilityStatus  # noqa: E402
from kernel.runner import CommandOutput  # noqa: E402
from plugins.ios_native import IOSNativePlugin  # noqa: E402


class FakeRunner:
    """记录 argv、返回预设输出。不真跑，但走的是插件的真实命令拼装路径。"""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def run(self, argv, *, timeout=600.0, cwd=None) -> CommandOutput:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
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
    p = IOSNativePlugin("real", data_dir="/tmp/iloop-ios-test", runner=FakeRunner())
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


def run() -> int:
    for fn in [
        test_sim_build_command_and_success_marker,
        test_build_exit0_without_marker_is_failure,
        test_real_build_uses_xcodebuildmcp_device_workflow,
        test_sim_install_launch_screenshot_commands,
        test_sim_view_tree_and_ui_actions_use_xcodebuildmcp,
        test_real_device_needs_udid,
        test_crash_now_implemented,
        test_real_crash_needs_udid,
        test_plugin_never_crashes_kernel,
        test_xcodebuildmcp_json_artifact_path,
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
