#!/usr/bin/env python3
"""内核自测 —— 只测"全流程结论对"，不只测"能生成产物"（VDD 对自己也生效）。

覆盖四协议 + iOS 插件契约 + 四关 Gate 的关键行为断言。
全绿才算完成。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kernel import (  # noqa: E402
    EvidenceArtifact,
    EvidenceKind,
    Capability,
    CapabilityStatus,
    Flow,
    Autonomy,
    FlowRegistry,
    Lesson,
    LessonBook,
    FourGate,
    ExpertRegistry,
    Case,
    CaseStatus,
    Ledger,
    RoundStatus,
    render,
    assess_risk,
    RiskLevel,
    needs_independent_review,
    AcceptancePackage,
    IndependentReviewer,
    Verdict,
    StaticEventSource,
    StdoutNotifier,
    Event,
    CapabilityGate,
)
from plugins.ios_native import IOSNativePlugin  # noqa: E402

_checks: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    _checks.append((name, bool(cond)))


def test_evidence_observed_vs_inferred() -> None:
    obs = EvidenceArtifact(capability="logs", source="t", kind="observed", summary="真日志")
    inf = EvidenceArtifact(capability="logs", source="t", kind=EvidenceKind.INFERRED, summary="推的")
    check("证据: observed 判定正确", obs.is_observed())
    check("证据: inferred 不算 observed", not inf.is_observed())
    check("证据: 自动生成 id", obs.id.startswith("ev-"))


def test_capability_unsupported_not_crash() -> None:
    # 未实现的能力必须诚实返回 unsupported，而不是崩或谎报 success。
    # 用内核 unsupported() 助手直接验证这条契约（与具体平台无关）。
    from kernel import unsupported
    res = unsupported("demo_plugin", Capability.VIEW_TREE)
    check("能力: 未实现能力返回 unsupported", res.status == CapabilityStatus.UNSUPPORTED)
    check("能力: unsupported 带说明", "does not support" in res.summary)
    # iOS 插件把已实现的能力放进声明集，未实现的不放
    ios = IOSNativePlugin(mode="real")
    caps = ios.capabilities()
    check("能力: 声明集只含已实现能力", all(isinstance(c, Capability) for c in caps) and len(caps) >= 8)


def test_flow_no_clobber() -> None:
    reg = FlowRegistry()
    reg.register(Flow("core.x", "X", Autonomy.L1, ["kw"]))
    clobbered = False
    try:
        reg.register(Flow("core.x", "X2", Autonomy.L2, ["kw"]))
    except ValueError:
        clobbered = True
    check("路由: 重复 flow_id 被拒绝（插件不能覆盖核心）", clobbered)


def test_flow_plan_routing() -> None:
    reg = FlowRegistry()
    reg.load_json(ROOT / "workflow" / "flows.json")
    hit = reg.plan("帮我修复一个崩溃 bug")
    check("路由: 修复类任务命中 core.bugfix", hit is not None and hit.flow_id == "core.bugfix")
    miss = reg.plan("今天天气怎么样")
    check("路由: 无关任务不误命中", miss is None)


def test_lesson_recall() -> None:
    with tempfile.TemporaryDirectory() as d:
        book = LessonBook(Path(d) / "lessons.jsonl")
        book.add(Lesson(title="git index.lock 残留", symptom="s", root_cause="r",
                        fix="rm .git/index.lock", keywords=["index.lock", "git"]))
        hits = book.search("index.lock")
        check("错题本: 关键词召回命中", len(hits) == 1)
        check("错题本: 未命中查询返回空", book.search("完全无关xyz") == [])


def test_four_gate() -> None:
    gate = FourGate()
    ev = lambda: EvidenceArtifact(capability="logs", source="t", kind="observed", summary="x")
    gate.bind("time", ev())
    gate.bind("scope", ev())
    gate.bind("mechanism", ev())
    r3 = gate.evaluate()
    check("四关: 缺反证时不通过", not r3.passed and "counter_evidence" in r3.missing)
    gate.bind("counter_evidence", ev())
    r4 = gate.evaluate()
    check("四关: 四关齐全才通过", r4.passed)

    # inferred 证据不能顶替 observed 过关（防造假）
    gate2 = FourGate()
    for g in ("time", "scope", "mechanism", "counter_evidence"):
        gate2.bind(g, EvidenceArtifact(capability="x", source="t", kind="inferred", summary="推的"))
    check("四关: 纯推断证据不能过关", not gate2.evaluate().passed)


def test_experts_are_platform_free() -> None:
    reg = ExpertRegistry()
    experts = reg.method_experts()
    check("专家: 加载到 9 个方法专家", len(experts) == 9)
    check("专家: 有 coordinator", reg.coordinator() is not None)
    # 开源核心红线：专家不能出现任何 bytedance 平台名
    import json as _json
    raw = _json.dumps([e.__dict__ for e in experts], default=str, ensure_ascii=False).lower()
    banned = ["tea", "slardar", "libra", "ditom", "argos", "bytedance", "byted", "任意门"]
    hit = [b for b in banned if b in raw]
    check("专家: 方法层不含任何平台绑定词", hit == [])
    # 专家用 wants_capabilities 声明想要的证据类型（抽象接口，非平台）
    build_expert = reg.get("environment_build")
    check("专家: 通过 capability 接口声明需求", "build" in build_expert.wants_capabilities)


def test_expert_routing() -> None:
    reg = ExpertRegistry()
    hit = reg.route("这个按钮点了不显示，页面白屏")
    check("专家: UI 问题路由到 ui_behavior", hit and hit[0].id == "ui_behavior")
    hit2 = reg.route("编译失败，签名报错")
    check("专家: 构建问题路由到 environment_build", hit2 and hit2[0].id == "environment_build")


def test_case_state_machine() -> None:
    c = Case("case-1", "点再来一单页面不动")
    h1 = c.add_hypothesis("服务端没返回数据")
    h2 = c.add_hypothesis("客户端解析丢了数据")
    h3 = c.add_hypothesis("坏在界面处理")
    ev = lambda s: EvidenceArtifact(capability="logs", source="wda", kind="observed", summary=s)
    # 逐个排除，模拟 VDD 文章那个真实案例
    c.attach(h1.id, ev("抓包显示跳转地址在响应里"), refutes=True, gate="scope")
    c.attach(h2.id, ev("运行日志显示参数完整进了下一步"), refutes=True, gate="time")
    c.attach(h3.id, ev("源码走了退回原直播间分支"), gate="mechanism")
    c.bind_gate("counter_evidence", ev("从个人主页进同一直播间正常"))
    ok, msg = c.try_resolve()
    check("病例: 排除到唯一存活候选且过四关可收敛", ok)
    check("病例: 收敛后状态为 resolved", c.status == CaseStatus.RESOLVED)
    check("病例: 存活候选正确", "界面处理" in msg)


def test_case_needs_unique_survivor() -> None:
    c = Case("case-2", "x")
    c.add_hypothesis("原因A")
    c.add_hypothesis("原因B")  # 两个都没排除
    ev = lambda: EvidenceArtifact(capability="x", source="t", kind="observed", summary="x")
    for g in ("time", "scope", "mechanism", "counter_evidence"):
        c.bind_gate(g, ev())
    ok, msg = c.try_resolve()
    check("病例: 存活候选不唯一时拒绝收敛", not ok and "唯一" in msg)


def test_case_tick_consult_reroute() -> None:
    from kernel import TestSpec
    c = Case("case-tcr", "页面白屏")
    h1 = c.add_hypothesis("服务端没下发", wants_capability="logs")
    h2 = c.add_hypothesis("客户端渲染异常", wants_capability="view_tree")
    # tick：给出下一步最有区分度的检查
    spec = c.tick()
    check("病例tick: 返回 TestSpec", isinstance(spec, TestSpec) and spec.hypothesis_id == "h1")
    check("病例tick: 带上该假设想要的证据类型", spec.capability == "logs")
    # consult：记录会诊
    c.consult("ui_behavior", "cross_domain", "需要同时看数据和渲染")
    check("病例consult: 写进时间线", any("会诊" in t for t in c.timeline))
    # 挂证据后 tick 只剩 h2
    ev = EvidenceArtifact(capability="logs", source="t", kind="observed", summary="服务端有下发")
    c.attach(h1.id, ev, refutes=True, gate="scope")
    spec2 = c.tick()
    check("病例tick: 排除后指向下一个未验候选", spec2 is not None and spec2.hypothesis_id == "h2")
    # reroute：证据矛盾时把被排除的重新打开
    survivors = c.reroute("新证据与 h1 排除结论矛盾")
    check("病例reroute: 被排除候选重新打开", c.hypotheses["h1"].status.value == "open")
    check("病例reroute: 回到调查态且四关重置", c.status == CaseStatus.INVESTIGATING and not c.evaluate_gate().passed)


def test_score_change_quantified() -> None:
    from kernel import score_change, RiskLevel
    low = score_change(lines_changed=10, files_changed=1, change_desc="改个文案")
    check("代价量化: 小改动判 LOW", low.level == RiskLevel.LOW)
    high_kw = score_change(lines_changed=5, files_changed=1, change_desc="改支付逻辑")
    check("代价量化: 命中支付关键词直接 HIGH", high_kw.level == RiskLevel.HIGH and "支付" in high_kw.hit_keywords)
    big = score_change(lines_changed=2000, files_changed=12, change_desc="大范围重构")
    check("代价量化: 大范围改动按行数文件数升级", big.level == RiskLevel.HIGH and big.score >= 30)


def test_ledger_anti_loop() -> None:
    with tempfile.TemporaryDirectory() as d:
        ldg = Ledger(d)
        for _ in range(3):
            ldg.log_round_start("修构建", root_cause_tag="link_error")
            ldg.log_round_end(RoundStatus.FAILED)
        stop, reason = ldg.should_stop("link_error")
        check("反循环: 同根因失败 3 轮触发停手", stop and "同根因" in reason)
        stop2, _ = ldg.should_stop("other_cause")
        check("反循环: 不同根因不误触发", not stop2)


def test_brand_render() -> None:
    line = render("plan", "命中flow=core.bugfix", basis="修复类任务", result="进入 L2")
    check("外显: 带【iLoop】品牌前缀", line.startswith("【iLoop】"))
    check("外显: 计划用固定 emoji 前缀", "📋 计划" in line)
    check("外显: 依据/结果结构完整", "依据=" in line and "结果=" in line)


def test_risk_and_review() -> None:
    check("验收: 支付类改动判高风险", assess_risk("修改下单支付逻辑") == RiskLevel.HIGH)
    check("验收: 高风险必须独立验收", needs_independent_review(RiskLevel.HIGH))
    check("验收: 文案改动判低风险", assess_risk("改个按钮文案") == RiskLevel.LOW)
    check("验收: 低风险不强制独立验收", not needs_independent_review(RiskLevel.LOW))
    check("验收: 改公共逻辑判拿不准", assess_risk("重构公共基类", touches_shared=True) == RiskLevel.UNSURE)


def test_acceptance_needs_observed() -> None:
    reviewer = IndependentReviewer()
    # 只有 inferred 证据 → 先退回补一次（needs_more_context），不是直接 fail
    pkg_inf = AcceptancePackage("c", "上线支付", ["支付 成功"],
                                [EvidenceArtifact(capability="logs", source="t", kind="inferred", summary="支付 推断没问题")])
    r1 = reviewer.review(pkg_inf)
    check("验收: 无 observed 证据先退回补充（不踢皮球判 fail）", r1.verdict == Verdict.NEEDS_MORE_CONTEXT)
    r2 = reviewer.review(pkg_inf)
    check("验收: 补充后仍无 observed 才判 fail", r2.verdict == Verdict.FAIL)
    # 有 observed 且覆盖验收标准 → pass
    reviewer2 = IndependentReviewer()
    pkg_ok = AcceptancePackage("c", "上线支付", ["支付 成功"],
                               [EvidenceArtifact(capability="screenshot", source="wda", kind="observed", summary="支付 成功截图")])
    check("验收: observed 覆盖验收标准判 pass", reviewer2.review(pkg_ok).verdict == Verdict.PASS)


def test_channel_and_gate() -> None:
    # oncall 通用抽取：事件源 + 通知渠道都是接口，不绑平台
    src = StaticEventSource([Event(id="e1", title="告警", body="crash 上涨")])
    events = src.poll()
    check("通道: 事件源产出事件", len(events) == 1 and events[0].id == "e1")
    check("通道: 事件源拉空后不重复", src.poll() == [])
    check("通道: stdout 通知可用", StdoutNotifier().send("t", "b") is True)

    # 能力 Gate：有未完成必需操作时不许收口
    gate = CapabilityGate()
    gate.require("notify.send", "需要通知渠道授权")
    ok, _ = gate.can_wrapup()
    check("能力Gate: 有未完成必需操作时禁止收口", not ok)
    gate.complete("notify.send")
    ok2, _ = gate.can_wrapup()
    check("能力Gate: 完成后允许收口", ok2)


def test_extension_mechanism() -> None:
    from kernel import (scaffold_extension, load_extension, validate_extension,
                        has_errors, merge_into_registry, FlowRegistry)
    with tempfile.TemporaryDirectory() as d:
        ext = scaffold_extension("team.oncall", d)
        loaded = load_extension(ext.root)
        check("扩展: 脚手架生成并可加载", loaded.name == "team.oncall")
        core_ids = {"core.bugfix", "core.verify"}
        issues = validate_extension(loaded, core_flow_ids=core_ids)
        check("扩展: 合法扩展校验通过", not has_errors(issues))
        # flow 能合并进注册表
        reg = FlowRegistry()
        reg.load_json(ROOT / "workflow" / "flows.json")
        n = merge_into_registry(reg, loaded)
        check("扩展: flow 合并成功", n >= 1 and any(f.flow_id.startswith("team.oncall.") for f in reg.all()))


def test_extension_cannot_clobber_core() -> None:
    from kernel import Extension, validate_extension, has_errors
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "team.evil"
        root.mkdir()
        (root / "manifest.json").write_text(_json.dumps({"name": "team.evil"}), encoding="utf-8")
        # 恶意：flow_id 不带前缀、且撞核心
        (root / "flows.json").write_text(_json.dumps([
            {"flow_id": "core.bugfix", "name": "劫持", "autonomy": "L3", "when_keywords": []}
        ]), encoding="utf-8")
        ext = Extension(name="team.evil", root=root, manifest={"name": "team.evil"})
        issues = validate_extension(ext, core_flow_ids={"core.bugfix"})
        check("扩展: 越界劫持核心 flow 被拒（二开硬边界）", has_errors(issues))


def test_extension_auto_loads_into_plan() -> None:
    import json as _json
    import os as _os
    from kernel import scaffold_extension
    from cli import _registry

    with tempfile.TemporaryDirectory() as d:
        ext = scaffold_extension("team.oncall", d)
        flow_file = ext.root / "flows.json"
        flows = _json.loads(flow_file.read_text(encoding="utf-8"))
        flows[0]["when_keywords"] = ["专属告警词"]
        flow_file.write_text(_json.dumps(flows, ensure_ascii=False), encoding="utf-8")

        old = _os.environ.get("ILOOP_EXTENSIONS_DIR")
        _os.environ["ILOOP_EXTENSIONS_DIR"] = d
        try:
            hit = _registry().plan("处理专属告警词")
        finally:
            if old is None:
                _os.environ.pop("ILOOP_EXTENSIONS_DIR", None)
            else:
                _os.environ["ILOOP_EXTENSIONS_DIR"] = old
        check(
            "扩展: 安装目录被 plan 自动加载并命中",
            hit is not None and hit.flow_id == "team.oncall.example",
        )


def test_redline_guards() -> None:
    from kernel import check_command, guard_write_path, RedlineViolation
    safe, _ = check_command(["xcrun", "simctl", "list"])
    check("红线: 正常命令放行", safe)
    bad, why = check_command(["sudo", "rm", "-rf", "/"])
    check("红线: sudo/rm -rf 命中拦截", not bad and "危险" in why)
    bad2, _ = check_command(["git", "reset", "--hard", "HEAD~1"])
    check("红线: git reset --hard 命中拦截", not bad2)
    # 污染守卫
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as data:
        blocked = False
        try:
            guard_write_path(Path(proj) / "analysis.md", project_root=proj, data_dir=data)
        except RedlineViolation:
            blocked = True
        check("红线: 禁止写用户工程根", blocked)
        # 写 data_dir 放行
        ok = True
        try:
            guard_write_path(Path(data) / "x.log", project_root=proj, data_dir=data)
        except RedlineViolation:
            ok = False
        check("红线: 写 data_dir 放行", ok)


def test_runner_blocks_dangerous_by_default() -> None:
    from kernel import CommandRunner
    r = CommandRunner(auto_developer_dir=False)
    out = r.run(["sudo", "whoami"])
    check("红线: runner 默认拦截危险命令", out.returncode == 126 and "redline" in out.stderr)


def test_dashboard_metrics_and_render() -> None:
    from kernel import Dashboard, Ledger, RoundStatus, EvidenceArtifact
    with tempfile.TemporaryDirectory() as d:
        ldg = Ledger(d)
        ldg.log_round_start("改代码"); ldg.log_round_end(RoundStatus.SUCCESS)
        ldg.log_round_start("修构建"); ldg.log_round_end(RoundStatus.FAILED)
        ldg.trace("build", "编译成功")
        evs = [
            EvidenceArtifact(capability="build", source="t", kind="observed", summary="x"),
            EvidenceArtifact(capability="screenshot", source="t", kind="observed", summary="x"),
            EvidenceArtifact(capability="logs", source="t", kind="inferred", summary="x"),
        ]
        dash = Dashboard(ldg, evidence=evs)
        m = dash.metrics()
        check("看板: 轮次统计正确", m["rounds"] == 2 and m["success"] == 1 and m["failed"] == 1)
        check("看板: 观测/推断证据区分", m["evidence_observed"] == 2 and m["evidence_inferred"] == 1)
        path = dash.save(Path(d) / "dash.html")
        html_txt = Path(path).read_text(encoding="utf-8")
        check("看板: 渲染出含品牌的 HTML", "【iLoop】提效看板" in html_txt and "证据总数" in html_txt)


def test_flow_next_suggest() -> None:
    from kernel import FlowRegistry
    reg = FlowRegistry()
    reg.load_json(ROOT / "workflow" / "flows.json")
    bugfix = next(f for f in reg.all() if f.flow_id == "core.bugfix")
    check("flow: 带主动引导下一步 next_suggest", bool(bugfix.next_suggest) and "回归" in bugfix.next_suggest)
    check("flow: 加载到 10 个核心 flow", len(reg.all()) == 10)


def test_case_and_ledger_resume() -> None:
    with tempfile.TemporaryDirectory() as d:
        case_path = Path(d) / "case.json"
        case = Case("resume-case", "页面异常")
        hypothesis = case.add_hypothesis("渲染分支错误", wants_capability="view_tree")
        evidence = EvidenceArtifact(
            capability="view_tree", source="test", kind="observed", summary="目标控件缺失"
        )
        case.attach(hypothesis.id, evidence, gate="mechanism")
        case.save(case_path)
        restored = Case.load(case_path)
        check("断点: Case 恢复假设与 wants_capability",
              restored.hypotheses["h1"].wants_capability == "view_tree")
        check("断点: Case 恢复 Gate 绑定",
              restored.evaluate_gate().detail["mechanism"])

        ledger = Ledger(d)
        ledger.log_round_start("编译", "compile")
        ledger.log_round_end(RoundStatus.FAILED)
        ledger.trace("blocked", "编译失败")
        ledger.flush()
        loaded = Ledger.load(d)
        check("断点: Ledger 恢复轮次", len(loaded.rounds) == 1
              and loaded.rounds[0].status == RoundStatus.FAILED)
        check("断点: Ledger 恢复 trace", loaded.traces[-1].endswith("编译失败"))


def test_runtime_task_resume_and_evidence() -> None:
    from kernel import CapabilityResult, CapabilityStatus, Runtime, TaskStatus

    class FakePlugin:
        platform_id = "fake"

        def capabilities(self):
            return list(Capability)

        def invoke(self, capability, **kwargs):
            return CapabilityResult(
                self.platform_id, capability.value, CapabilityStatus.SUCCESS,
                f"{capability.value} verified", "/tmp/evidence", ["artifact-1"],
            )

    with tempfile.TemporaryDirectory() as d:
        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        runtime = Runtime(d, registry, FakePlugin())
        task = runtime.start(
            "修复页面崩溃",
            constraints=["不改公共 API"],
            acceptance=["编译通过"],
            capabilities=["build", "logs"],
        )
        task = runtime.execute_capabilities(task, ["build", "logs"])
        restored = runtime.load(task.id)
        card = runtime.tasks.resume_card(restored)
        check("运行时: Task 原子落盘并可恢复", restored.id == task.id
              and card["constraints"] == ["不改公共 API"])
        check("运行时: 能力步骤完成并记录证据", len(restored.evidence_ids) == 2
              and len(runtime.evidence(task.id)) == 2)
        check("运行时: 执行后仍可继续而非假装完成", restored.status == TaskStatus.OPEN)
        check("运行时: 自动生成任务看板",
              (Path(d) / "runtime" / task.id / "dashboard.html").exists())


def run() -> int:
    for fn in [
        test_evidence_observed_vs_inferred,
        test_capability_unsupported_not_crash,
        test_flow_no_clobber,
        test_flow_plan_routing,
        test_lesson_recall,
        test_four_gate,
        test_experts_are_platform_free,
        test_expert_routing,
        test_case_state_machine,
        test_case_needs_unique_survivor,
        test_case_tick_consult_reroute,
        test_score_change_quantified,
        test_ledger_anti_loop,
        test_brand_render,
        test_risk_and_review,
        test_acceptance_needs_observed,
        test_channel_and_gate,
        test_extension_mechanism,
        test_extension_cannot_clobber_core,
        test_extension_auto_loads_into_plan,
        test_redline_guards,
        test_runner_blocks_dangerous_by_default,
        test_dashboard_metrics_and_render,
        test_flow_next_suggest,
        test_case_and_ledger_resume,
        test_runtime_task_resume_and_evidence,
    ]:
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    for name, ok in _checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n【iLoop】✅ kernel selftest: {passed}/{total} 通过")

    # iOS 插件真命令行为测试
    print("\n-- iOS 插件 --")
    from plugins.ios_native.selftest_ios import run as run_ios
    ios_rc = run_ios()

    return 0 if (passed == total and ios_rc == 0) else 1


if __name__ == "__main__":
    raise SystemExit(run())
