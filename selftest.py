#!/usr/bin/env python3
"""内核自测 —— 只测"全流程结论对"，不只测"能生成产物"（VDD 对自己也生效）。

覆盖四协议 + iOS 插件契约 + 四关 Gate 的关键行为断言。
全绿才算完成。
"""

from __future__ import annotations

import sys
import tempfile
import time
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
    TaskStore,
)
from plugins.ios_native import IOSNativePlugin  # noqa: E402

_checks: list[tuple[str, bool]] = []
_test_evidence_dir = tempfile.TemporaryDirectory()
_test_evidence_counter = 0


def check(name: str, cond: bool) -> None:
    _checks.append((name, bool(cond)))


def observed(
    summary: str = "observed",
    *,
    capability: str = "logs",
    outcome: str = "success",
    metadata: dict = None,
    evidence_id: str = "",
) -> EvidenceArtifact:
    global _test_evidence_counter
    _test_evidence_counter += 1
    path = Path(_test_evidence_dir.name) / f"artifact-{_test_evidence_counter}.txt"
    path.write_text(summary, encoding="utf-8")
    bindings = {"trusted_producer": True}
    bindings.update(metadata or {})
    evidence = EvidenceArtifact(
        capability=capability,
        source="selftest",
        kind="observed",
        summary=summary,
        path=str(path),
        outcome=outcome,
        metadata=bindings,
        id=evidence_id,
    )
    receipt = Path(_test_evidence_dir.name) / f"receipt-{_test_evidence_counter}.json"
    receipt.write_text(
        __import__("json").dumps({
            "producer": "iloop-runtime",
            "capability": evidence.capability,
            "source": evidence.source,
            "outcome": evidence.outcome,
            "summary": evidence.summary,
            "path": evidence.path,
            "artifact_sha256": evidence.metadata["artifact_sha256"],
            "task_id": evidence.metadata.get("task_id", ""),
            "run_id": evidence.metadata.get("run_id", ""),
            "flow_id": evidence.metadata.get("flow_id", ""),
            "ui_flow_id": evidence.metadata.get("ui_flow_id", ""),
            "flow_run_id": evidence.metadata.get("flow_run_id", ""),
            "device": evidence.metadata.get("device", ""),
            "device_id": evidence.metadata.get("device_id", ""),
            "subjects": evidence.metadata.get("subjects", []),
            "gates": evidence.metadata.get("gates", []),
            "created_at": evidence.created_at,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    evidence.metadata["producer_receipt_path"] = str(receipt)
    evidence.metadata["producer_receipt_sha256"] = __import__("hashlib").sha256(
        receipt.read_bytes()
    ).hexdigest()
    return evidence


def test_evidence_observed_vs_inferred() -> None:
    obs = EvidenceArtifact(capability="logs", source="t", kind="observed", summary="真日志")
    inf = EvidenceArtifact(capability="logs", source="t", kind=EvidenceKind.INFERRED, summary="推的")
    check("证据: observed 判定正确", obs.is_observed())
    check("证据: inferred 不算 observed", not inf.is_observed())
    check("证据: 自动生成 id", obs.id.startswith("ev-"))
    with tempfile.TemporaryDirectory() as d:
        artifact = Path(d) / "artifact.log"
        artifact.write_text("original", encoding="utf-8")
        durable = EvidenceArtifact(
            capability="logs", source="test", kind="observed",
            outcome="success", summary="captured", path=str(artifact),
        )
        artifact.write_text("tampered", encoding="utf-8")
        check("证据: 产物被替换后成功证明失效", not durable.supports_success())
    copied = observed(
        "task-a proof",
        metadata={
            "task_id": "task-a",
            "flow_id": "core.verify",
            "run_id": "run-a",
            "gates": ["time"],
        },
    )
    check(
        "证据: 跨 task 复制后不能支持另一任务",
        not copied.supports_gate(
            "time",
            expected_bindings={"task_id": "task-b", "flow_id": "core.verify"},
        ),
    )
    copied.metadata["task_id"] = "task-b"
    copied.metadata["gates"] = ["scope"]
    check("证据: 直接扩大持久化主体或 Gate 后 receipt 复验失败",
          not copied.supports_gate(
              "scope",
              expected_bindings={"task_id": "task-b", "flow_id": "core.verify"},
          ))
    spoofed = EvidenceArtifact(
        capability="logs", source="test", kind="observed", summary="spoofed",
        metadata={"reproducible": True},
    )
    check("证据: reproducible 字段不再绕过产物与来源校验",
          not spoofed.supports_success())
    unsigned_internal = observed("unsigned internal")
    check("证据: trusted_producer 自签 receipt 无宿主 verifier 仍失败",
          not unsigned_internal.supports_success())
    check("证据: trusted_producer receipt 经宿主 verifier 后可用",
          unsigned_internal.supports_success(lambda path, row: True))


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


def test_task_ids_cannot_escape_data_dir() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = TaskStore(d)
        rejected = False
        try:
            store.path_for("../../outside")
        except ValueError:
            rejected = True
        check("任务: task_id 不能越出 data 目录", rejected)


def test_four_gate() -> None:
    gate = FourGate()
    ev = lambda g: observed(metadata={"gates": [g]})
    gate.bind("time", ev("time"))
    gate.bind("scope", ev("scope"))
    gate.bind("mechanism", ev("mechanism"))
    verifier = lambda path, row: True
    r3 = gate.evaluate(verifier)
    check("四关: 缺反证时不通过", not r3.passed and "counter_evidence" in r3.missing)
    gate.bind("counter_evidence", ev("counter_evidence"))
    r4 = gate.evaluate(verifier)
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
    ev = lambda s, g: observed(s, metadata={"gates": [g]})
    # 逐个排除，模拟 VDD 文章那个真实案例
    c.attach(h1.id, ev("抓包显示跳转地址在响应里", "scope"), refutes=True, gate="scope")
    c.attach(h2.id, ev("运行日志显示参数完整进了下一步", "time"), refutes=True, gate="time")
    c.attach(h3.id, ev("源码走了退回原直播间分支", "mechanism"), gate="mechanism")
    c.bind_gate(
        "counter_evidence",
        ev("从个人主页进同一直播间正常", "counter_evidence"),
    )
    ok, msg = c.try_resolve(lambda path, row: True)
    check("病例: 排除到唯一存活候选且过四关可收敛", ok)
    check("病例: 收敛后状态为 resolved", c.status == CaseStatus.RESOLVED)
    check("病例: 存活候选正确", "界面处理" in msg)


def test_case_needs_unique_survivor() -> None:
    c = Case("case-2", "x")
    c.add_hypothesis("原因A")
    c.add_hypothesis("原因B")  # 两个都没排除
    ev = lambda g: observed(capability="probe", metadata={"gates": [g]})
    for g in ("time", "scope", "mechanism", "counter_evidence"):
        c.bind_gate(g, ev(g))
    ok, msg = c.try_resolve(lambda path, row: True)
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
    reviewer2 = IndependentReviewer(lambda path, row: True)
    pkg_ok = AcceptancePackage("c", "上线支付", ["支付 成功"],
                               [observed("支付 成功截图", capability="screenshot")])
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
    operation = gate.require(
        "notify.send", "需要通知渠道授权", task_id="task-gate"
    )
    ok, _ = gate.can_wrapup()
    check("能力Gate: 有未完成必需操作时禁止收口", not ok)
    with tempfile.TemporaryDirectory() as d:
        evidence = Path(d) / "delivery.json"
        evidence.write_text(__import__("json").dumps({
            "operation_id": "notify.send",
            "task_id": "task-gate",
            "requirement_id": operation.requirement_id,
            "required_at": operation.created_at,
            "created_at": time.time(),
            "kind": "observed",
            "outcome": "success",
            "expires_at": time.time() + 3600,
        }), encoding="utf-8")
        gate.complete(
            "notify.send", str(evidence),
            verify_attestation=lambda path, row: True,
        )
        gate_path = Path(d) / "gate.json"
        gate.save(gate_path)
        restored = CapabilityGate.load(gate_path)
        verifier = lambda kind, path, row: True
        ok2, _ = restored.can_wrapup(verifier)
        check("能力Gate: 真实回读证据完成后允许收口并可恢复", ok2)
        tampered_row = __import__("json").loads(gate_path.read_text(encoding="utf-8"))
        tampered_row["operations"] = []
        gate_path.write_text(__import__("json").dumps(tampered_row), encoding="utf-8")
        check("能力Gate: 直接删除持久化 operation 会触发完整性失败",
              not CapabilityGate.load(gate_path).can_wrapup(verifier)[0])
        gate.save(gate_path)
        restored = CapabilityGate.load(gate_path)
        check("能力Gate: 绑定任务不匹配时拒绝复用",
              not restored.can_wrapup(verifier, expected_task_id="task-other")[0])
        evidence.unlink()
        ok3, _ = restored.can_wrapup(verifier)
        check("能力Gate: 回读证据删除后旧 completed 状态失效", not ok3)
        cancel_gate = CapabilityGate()
        cancel_operation = cancel_gate.require(
            "optional.notify", "用户决定是否放弃通知", task_id="task-gate"
        )
        cancellation = Path(d) / "cancel.json"
        cancellation.write_text(__import__("json").dumps({
            "operation_id": "optional.notify",
            "task_id": "task-gate",
            "requirement_id": cancel_operation.requirement_id,
            "required_at": cancel_operation.created_at,
            "created_at": time.time(),
            "confirmed": True,
            "reason": "用户明确改为手动通知",
            "user_id": "user-1",
            "expires_at": time.time() + 3600,
        }), encoding="utf-8")
        rejected = False
        try:
            cancel_gate.close(
                "optional.notify",
                attestation_path=str(cancellation),
                verify_attestation=None,
            )
        except ValueError:
            rejected = True
        cancel_gate.close(
            "optional.notify",
            attestation_path=str(cancellation),
            verify_attestation=lambda path, row: True,
        )
        check("能力Gate: 无宿主用户证明时取消被拒绝", rejected)
        check("能力Gate: 用户确认且有理由时允许取消",
              cancel_gate.can_wrapup(verifier)[0])


def test_extension_mechanism() -> None:
    from kernel import (scaffold_extension, load_extension, validate_extension,
                        has_errors, merge_into_registry, FlowRegistry,
                        load_extension_plugin, load_installed_plugins)
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
        manifest_path = ext.root / "manifest.json"
        manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provides"]["plugin"] = "plugin.py"
        manifest_path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
        (ext.root / "plugin.py").write_text(
            "from kernel import Capability, CapabilityResult, CapabilityStatus\n"
            "class Demo:\n"
            " platform_id='team_demo'\n"
            " def capabilities(self): return [Capability.PROBE]\n"
            " def invoke(self, capability, **kwargs):\n"
            "  return CapabilityResult(self.platform_id, capability.value, CapabilityStatus.SUCCESS, 'ok')\n"
            "def create_plugin(config): return Demo()\n",
            encoding="utf-8",
        )
        plugin = load_extension_plugin(load_extension(ext.root))
        check("扩展: 声明式 Capability Plugin 可动态加载",
              plugin is not None and plugin.platform_id == "team_demo")
        bad = Path(d) / "team.bad"
        bad.mkdir()
        (bad / "manifest.json").write_text("{broken", encoding="utf-8")
        plugin_issues = []
        plugins = load_installed_plugins(d, issues=plugin_issues)
        check(
            "扩展: 损坏相邻扩展不阻断合法插件加载",
            any(item.platform_id == "team_demo" for item in plugins)
            and any("team.bad" in issue.message for issue in plugin_issues),
        )


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
    bypasses = [
        ["git", "-C", "/tmp/repo", "reset", "--hard"],
        ["rm", "--recursive", "--force", "/tmp/x"],
        ["rm", "-fr", "/tmp/x"],
        ["bash", "-lc", "git -C /tmp/repo reset --hard"],
        ["git", "-c", "alias.wipe=reset --hard", "wipe"],
        ["git", "wipe"],
        ["git", "restore", "--worktree", "."],
        ["git", "checkout", "-f", "HEAD"],
        ["git", "switch", "--discard-changes", "main"],
        ["git", "reset", "--merge", "HEAD"],
        ["git", "clean", "-fd"],
        ["bash", "-lc", "true && git restore ."],
        ["bash", "-xc", "true && git restore ."],
        ["bash", "-lc", "true\ngit restore ."],
        ["git", "checkout", "HEAD", "tracked.txt"],
    ]
    check(
        "红线: 标准等价参数不能绕过危险命令守卫",
        all(not check_command(argv)[0] for argv in bypasses),
    )
    safe_git = [
        ["git", "checkout", "main"],
        ["git", "switch", "main"],
        ["git", "clean", "-nd"],
        ["git", "pull", "--ff-only"],
        ["bash", "-lc", "printf '%s' 'a|b'"],
    ]
    check(
        "红线: 常用非破坏性 Git 操作不被误伤",
        all(check_command(argv)[0] for argv in safe_git),
    )
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
    r = CommandRunner()
    out = r.run(["sudo", "whoami"])
    check("红线: runner 默认拦截危险命令", out.returncode == 126 and "redline" in out.stderr)


def test_runner_does_not_wait_for_detached_child_output() -> None:
    import time
    from kernel import CommandRunner

    runner = CommandRunner()
    start = time.time()
    out = runner.run([
        "/bin/sh",
        "-c",
        "(sleep 3; echo child) & echo parent",
    ], allow_dangerous=True)
    check(
        "运行器: 主进程退出后不被继承输出句柄的后台子进程阻塞",
        out.returncode == 0
        and "parent" in out.stdout
        and time.time() - start < 2,
    )


def test_runner_timeout_kills_remaining_process_group() -> None:
    import os
    import time
    from kernel import CommandRunner

    with tempfile.TemporaryDirectory() as d:
        child_pid_path = Path(d) / "child.pid"
        runner = CommandRunner()
        out = runner.run(
            [
                "/bin/sh",
                "-c",
                (
                    "(trap '' TERM; sleep 30) & "
                    f"echo $! > {child_pid_path}; wait"
                ),
            ],
            timeout=2,
            allow_dangerous=True,
        )
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        time.sleep(0.1)
        try:
            os.kill(child_pid, 0)
            child_alive = True
        except ProcessLookupError:
            child_alive = False
        check(
            "运行器: 超时后回收忽略 TERM 的剩余进程组成员",
            out.returncode == 124 and not child_alive,
        )


def test_runner_legacy_constructor_and_cross_platform_cleanup() -> None:
    import kernel
    import warnings
    from kernel import CommandRunner
    from unittest import mock

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        positional = CommandRunner(False)
        keyword = CommandRunner(auto_developer_dir=False)
    check(
        "运行器: 旧 auto_developer_dir 构造方式保持兼容",
        positional.environment_overrides == {}
        and keyword.environment_overrides == {},
    )
    check(
        "内核兼容层: discover_developer_dir 顶层导出仍可调用",
        callable(kernel.discover_developer_dir),
    )

    process = mock.Mock()
    process.pid = 42
    process.poll.return_value = None
    with mock.patch("kernel.runner.os.name", "nt"), mock.patch(
        "kernel.runner.subprocess.run"
    ) as taskkill:
        CommandRunner._stop_process_group(process)
    check(
        "运行器: Windows 清理使用进程树回收而非 POSIX killpg",
        taskkill.call_args.args[0]
        == ["taskkill", "/PID", "42", "/T", "/F"],
    )


def test_runner_cleanup_ignores_second_interrupt() -> None:
    import signal
    from kernel import CommandRunner
    from unittest import mock

    process = mock.Mock()
    process.pid = 42
    process.poll.side_effect = [None, None, 0, 0]
    process.wait.side_effect = [KeyboardInterrupt(), 0]
    with mock.patch("kernel.runner.os.name", "posix"), mock.patch(
        "kernel.runner.os.killpg"
    ) as killpg:
        CommandRunner._stop_process_group(process)
    signals = [call.args[1] for call in killpg.call_args_list]
    check(
        "运行器: 清理期间二次中断不跳过 SIGKILL",
        signal.SIGTERM in signals and signal.SIGKILL in signals,
    )


def test_direct_cli_uses_project_data_dir() -> None:
    import os
    import cli
    from unittest import mock
    from kernel import CapabilityResult, CapabilityStatus

    captured = []

    class FakePlugin:
        def __init__(self, *args, **kwargs):
            captured.append(kwargs.get("data_dir", ""))

        def invoke(self, capability, **kwargs):
            return CapabilityResult(
                "fake",
                capability.value,
                CapabilityStatus.SUCCESS,
                "ok",
            )

    with tempfile.TemporaryDirectory() as d, mock.patch.dict(
        os.environ,
        {"ILOOP_DATA_DIR": d},
    ), mock.patch.object(cli, "IOSNativePlugin", FakePlugin):
        cli.cmd_doctor(False)
        cli.cmd_invoke("probe", False, {})
        check(
            "CLI: direct doctor/invoke 证据统一写项目数据目录",
            captured == [str(Path(d) / "platform")] * 2,
        )


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
        evidence = observed(
            "目标控件缺失",
            capability="view_tree",
            metadata={"gates": ["mechanism"]},
        )
        case.attach(hypothesis.id, evidence, gate="mechanism")
        case.save(case_path)
        restored = Case.load(case_path)
        check("断点: Case 恢复假设与 wants_capability",
              restored.hypotheses["h1"].wants_capability == "view_tree")
        check("断点: Case 恢复 Gate 绑定",
              restored.evaluate_gate(lambda path, row: True).detail["mechanism"])

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
            "验证页面行为",
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
        raw_path = runtime.tasks.path_for(task.id)
        raw = __import__("json").loads(raw_path.read_text(encoding="utf-8"))
        raw["global_review_path"] = ""
        raw_path.write_text(__import__("json").dumps(raw), encoding="utf-8")
        migrated = runtime.load(task.id)
        check("断点: 旧 Task 缺新增路径字段时自动迁移",
              migrated.global_review_path.endswith("global-review.json"))


def test_runtime_logs_follow_successful_run_id() -> None:
    from kernel import CapabilityResult, CapabilityStatus, GlobalReview, Runtime

    with tempfile.TemporaryDirectory() as d:
        calls = []
        evidence_dir = Path(d) / "evidence"
        evidence_dir.mkdir()
        (evidence_dir / "artifact.log").write_text("ok", encoding="utf-8")

        class FakePlugin:
            platform_id = "fake"
            def capabilities(self):
                return list(Capability)
            def invoke(self, capability, **kwargs):
                calls.append((capability.value, kwargs.get("run_id", "")))
                return CapabilityResult(
                    "fake", capability.value, CapabilityStatus.SUCCESS,
                    "verified", str(evidence_dir), [],
                )

        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        runtime = Runtime(
            d,
            registry,
            FakePlugin(),
            attestation_verifier=lambda kind, path, row: True,
        )
        task = runtime.start(
            "验证运行日志",
            capabilities=["run", "logs"],
        )
        task = runtime.execute_capabilities(task, ["run", "logs"])
        evidence = runtime.evidence(task.id)
        run_call = next(item for item in calls if item[0] == "run")
        logs_call = next(item for item in calls if item[0] == "logs")
        logs_evidence = next(item for item in evidence if item.capability == "logs")
        check("运行时日志: logs 查询显式复用最近成功 run_id",
              run_call[1] == logs_call[1] and bool(run_call[1]))
        check("运行时日志: 日志证据同时记录采集 round 与 source run",
              logs_evidence.metadata["run_id"] != logs_evidence.metadata["source_run_id"]
              and logs_evidence.metadata["source_run_id"] == run_call[1])


def test_policy_and_constitution_cannot_be_forged_by_cli_state() -> None:
    from kernel import CapabilityResult, CapabilityStatus, ProjectMemory, Runtime

    class FakePlugin:
        platform_id = "fake"
        def capabilities(self):
            return list(Capability)
        def invoke(self, capability, **kwargs):
            return CapabilityResult(
                "fake", capability.value, CapabilityStatus.SUCCESS,
                "verified", "", [],
            )

    with tempfile.TemporaryDirectory() as d:
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=d, check=True)
        (Path(d) / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=d, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        runtime = Runtime(d, registry, FakePlugin(), project_root=d)
        task = runtime.start("重构公共模块", executor_id="main-agent")
        task.global_review_required = False
        task.independent_acceptance_required = False
        task.title = "只读分析"
        task.flow_id = "core.investigate"
        task.autonomy = "L1"
        task.project_root = ""
        runtime.tasks.save(task)
        restored = runtime.load(task.id)
        check("策略派生: 直接改 Task JSON 不能关闭重构全局复核",
              restored.global_review_required)
        check("策略派生: 直接改 Task JSON 不能关闭重构独立验收",
              restored.independent_acceptance_required)
        check("策略派生: title/flow/autonomy/project_root 从创建策略恢复",
              restored.title == "重构公共模块"
              and restored.flow_id == "core.refactor"
              and restored.autonomy == "L2"
              and restored.project_root == str(Path(d).resolve()))
        runtime.require_operation(restored, "platform.read", "需要平台回读")
        restored.required_operation_ids = []
        runtime.tasks.save(restored)
        CapabilityGate().save(restored.capability_gate_path)
        _, gate_blockers = runtime.can_wrapup(restored)
        check("策略派生: 即使重算 Gate 哈希也不能删除 Task 锚定的必需操作",
              any("lost required operations" in item for item in gate_blockers))
        runtime._requirements_path(restored.id).write_text(
            __import__("json").dumps({
                "task_id": restored.id,
                "revision": 0,
                "operations": [],
            }),
            encoding="utf-8",
        )
        _, fully_tampered = runtime.can_wrapup(restored)
        check("策略派生: 同时清空三份本地状态仍缺宿主 requirements attestation",
              any("requirements are not host attested" in item
                  for item in fully_tampered))

        # v0.2.2 #1: 普通 L2（局部 bug）不因带工程根就在恢复后被升级为全局复核
        local = runtime.start("修复局部 UI 显示 bug", executor_id="main-agent")
        check("过度复核: 普通 L2 局部修复创建时不要求全局复核",
              not local.global_review_required)
        local_restored = runtime.load(local.id)
        check("过度复核: 普通 L2 恢复后仍不被静默升级为全局复核",
              not local_restored.global_review_required
              and local_restored.global_review_status == "not_required")

        # v0.2.2 #3: 命中核心风险关键词的小改动强制独立验收（规模再小也要）
        risky = runtime.start("修复登录鉴权判断", executor_id="main-agent")
        check("关键词接线: 命中鉴权关键词的小改动强制独立验收",
              risky.independent_acceptance_required)
        risky_restored = runtime.load(risky.id)
        check("关键词接线: 核心关键词验收要求在恢复后仍然成立",
              risky_restored.independent_acceptance_required)

        # v0.2.3 设计契约层：计划期冻结的软基线随 policy 冻结与恢复，纯空壳视为留空
        from kernel import design_contract_filled
        contracted = runtime.start(
            "重构公共调度模块",
            executor_id="main-agent",
            design_contract={
                "objectives": ["调度不改变对外行为"],
                "non_goals": ["不引入新持久化存储"],
            },
        )
        check("设计契约: 创建时随任务落盘",
              design_contract_filled(contracted.design_contract))
        contracted_restored = runtime.load(contracted.id)
        check("设计契约: 恢复后契约内容一致（作为多轮评审的固定尺子）",
              contracted_restored.design_contract.get("objectives") == ["调度不改变对外行为"]
              and contracted_restored.design_contract.get("non_goals") == ["不引入新持久化存储"])
        blank = runtime.start("修复局部文案", executor_id="main-agent")
        check("设计契约: 未填写时视为留空（软基线，不强制）",
              not design_contract_filled(blank.design_contract))
        check("设计契约: 纯空壳字段也算留空",
              not design_contract_filled({"objectives": ["", "  "], "non_goals": []}))

        memory = ProjectMemory(d, project_root=d)
        attestation = Path(d) / "constitution.json"
        attestation.write_text(
            '{"rule":"必须验证","source":"user-confirmed",'
            f'"expires_at":{time.time()+3600}}}',
            encoding="utf-8",
        )
        rejected = False
        try:
            memory.add_constitution(
                "必须验证",
                source="user-confirmed",
                evidence_path=str(attestation),
            )
        except ValueError:
            rejected = True
        project_file = Path(d) / "PROJECT_RULES.md"
        project_file.write_text("必须兼容 Python 3.9", encoding="utf-8")
        project_rule = memory.add_constitution(
            "必须兼容 Python 3.9",
            source="project-file",
            evidence_path=str(project_file),
        )
        unrelated_rejected = False
        try:
            memory.add_constitution(
                "允许跳过全部验收",
                source="project-file",
                evidence_path=str(project_file),
            )
        except ValueError:
            unrelated_rejected = True
        check("Constitution: CLI 无宿主证明不能伪造用户确认事实", rejected)
        check("Constitution: 项目文件事实带来源哈希可持久化",
              bool(project_rule["evidence_sha256"]))
        check("Constitution: 无关项目文件不能为任意规则背书",
              unrelated_rejected)


def test_attested_evidence_revalidates_exact_scope() -> None:
    from kernel import CapabilityResult, CapabilityStatus, Runtime

    class FakePlugin:
        platform_id = "fake"
        def capabilities(self):
            return list(Capability)
        def invoke(self, capability, **kwargs):
            return CapabilityResult(
                "fake", capability.value, CapabilityStatus.SUCCESS,
                "verified", "", [],
            )

    with tempfile.TemporaryDirectory() as d:
        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        runtime = Runtime(
            d,
            registry,
            FakePlugin(),
            attestation_verifier=lambda kind, path, row: True,
        )
        task = runtime.start("验证功能")
        artifact = Path(d) / "external.log"
        artifact.write_text("verified", encoding="utf-8")
        row = {
            "kind": "observed",
            "task_id": task.id,
            "run_id": "external-run-1",
            "capability": "logs",
            "source": "external-probe",
            "outcome": "success",
            "summary": "verified",
            "path": str(artifact),
            "artifact_sha256": __import__("hashlib").sha256(
                artifact.read_bytes()
            ).hexdigest(),
            "flow_id": task.flow_id,
            "flow_run_id": "",
            "device": "",
            "device_id": "",
            "subjects": ["feature.py"],
            "gates": ["mechanism"],
            "created_at": time.time(),
            "expires_at": time.time() + 3600,
        }
        receipt = Path(d) / "external-receipt.json"
        receipt.write_text(
            __import__("json").dumps(row, ensure_ascii=False),
            encoding="utf-8",
        )
        evidence = runtime.add_attested_evidence(task, receipt)
        check("外部证据: receipt 完整绑定时可复验",
              runtime._supports_success(evidence, task))
        evidence.metadata["subjects"] = ["feature.py", "unreviewed.py"]
        check("外部证据: 直接扩大 subjects 后 receipt 复验失败",
              not runtime._supports_success(evidence, task))


def test_wrapup_cannot_bypass_vdd_gates() -> None:
    from kernel import CapabilityResult, CapabilityStatus, Runtime

    class FakePlugin:
        platform_id = "fake"
        def capabilities(self):
            return list(Capability)
        def invoke(self, capability, **kwargs):
            return CapabilityResult("fake", capability.value, CapabilityStatus.SUCCESS,
                                    "verified", "/tmp/evidence", ["a1"])

    with tempfile.TemporaryDirectory() as d:
        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        runtime = Runtime(d, registry, FakePlugin())
        task = runtime.start("验证一个功能")
        for step in task.steps:
            step.status = "done"
        runtime.tasks.save(task)
        blocked = False
        try:
            runtime.complete(task)
        except ValueError as error:
            blocked = "no evidence" in str(error) and "four gates" in str(error)
        check("绕过防护: 手工勾完步骤不能绕过证据与四关收口", blocked)


def test_failure_observation_does_not_pass_gate() -> None:
    gate = FourGate()
    gate.bind("time", EvidenceArtifact(
        capability="run", source="test", kind="observed",
        outcome="failure", summary="launch failed",
    ))
    check("证据语义: observed failure 不能支持四关通过",
          not gate.evaluate().detail["time"])


def test_external_acceptance_is_persistent_and_not_self_reviewed() -> None:
    from kernel import AcceptanceStore
    with tempfile.TemporaryDirectory() as d:
        store = AcceptanceStore(Path(d) / "acceptance.json")
        package = store.prepare(AcceptancePackage(
            "case-a", "目标", ["标准"],
            [observed("标准通过", capability="build")],
            executor_id="main-agent",
        ))
        result_path = Path(d) / "review.json"
        result_row = {
            "package_id": package.package_id,
            "case_id": package.case_id,
            "review_token": "wrong-token",
            "subject_fingerprint": package.subject_fingerprint,
            "reviewer": "external-agent",
            "verdict": "pass",
            "criteria_verdicts": ["pass"],
            "reasons": ["observed evidence"],
            "expires_at": time.time() + 3600,
        }
        result_path.write_text(__import__("json").dumps(result_row), encoding="utf-8")
        rejected = False
        try:
            store.record_file(result_path, verify_attestation=lambda path, row: True)
        except ValueError:
            rejected = True
        result_row["review_token"] = package.review_token
        result_path.write_text(__import__("json").dumps(result_row), encoding="utf-8")
        self_review_rejected = False
        try:
            result_row["reviewer"] = "main-agent"
            result_path.write_text(__import__("json").dumps(result_row), encoding="utf-8")
            store.record_file(result_path, verify_attestation=lambda path, row: True)
        except ValueError:
            self_review_rejected = True
        result_row["reviewer"] = "external-agent"
        result_path.write_text(__import__("json").dumps(result_row), encoding="utf-8")
        no_attestation = False
        try:
            store.record_file(result_path)
        except ValueError:
            no_attestation = True
        result = store.record_file(
            result_path,
            verify_attestation=lambda path, row: row.get("reviewer") == "external-agent",
        )
        restored = AcceptanceStore(Path(d) / "acceptance.json").result(
            lambda path, row: row.get("reviewer") == "external-agent"
        )
        check("独立验收: 无 challenge token 的伪回写被拒绝", rejected)
        check("独立验收: 无可信宿主 attestation 时 fail closed", no_attestation)
        check("独立验收: 执行者不能给自己验收", self_review_rejected)
        check("独立验收: 外部结果文件及哈希持久化",
              result.verdict == Verdict.PASS and restored is not None
              and restored.reviewer == "external-agent"
              and bool(restored.artifact_sha256))
        replay_store = AcceptanceStore(Path(d) / "replayed-acceptance.json")
        replay_store.path.write_bytes(store.path.read_bytes())
        check("独立验收: 相同 fingerprint 的结果不能跨 Task 重放",
              replay_store.result(
                  lambda path, row: True,
                  expected_case_id="case-b",
              ) is None)
        acceptance_data = store.load_raw()
        acceptance_data["package"]["subject_fingerprint"] = "new-diff"
        store.path.write_text(
            __import__("json").dumps(acceptance_data), encoding="utf-8"
        )
        check("独立验收: package fingerprint 变化后旧结果失效",
              store.result(lambda path, row: True) is None)
        expired_store = AcceptanceStore(Path(d) / "expired-acceptance.json")
        expired_package = AcceptancePackage(
            "case-expired", "目标", ["标准"],
            [observed("标准通过")],
            executor_id="main-agent",
            expires_at=time.time() - 1,
        )
        expired_store.prepare(expired_package)
        result_row.update({
            "package_id": expired_package.package_id,
            "case_id": expired_package.case_id,
            "review_token": expired_package.review_token,
            "subject_fingerprint": expired_package.subject_fingerprint,
        })
        result_path.write_text(__import__("json").dumps(result_row), encoding="utf-8")
        expired_rejected = False
        try:
            expired_store.record_file(
                result_path, verify_attestation=lambda path, row: True
            )
        except ValueError:
            expired_rejected = True
        check("独立验收: 过期验收包不能凭旧 challenge 生成新结论", expired_rejected)

        # v0.2.2 #2: 逐条验收 —— 总体 pass 必须覆盖每条 criterion
        per_store = AcceptanceStore(Path(d) / "per-criterion.json")
        per_pkg = per_store.prepare(AcceptancePackage(
            "case-per", "目标", ["标准A", "标准B"],
            [observed("标准通过", capability="build")],
            executor_id="main-agent",
        ))
        base_row = {
            "package_id": per_pkg.package_id,
            "case_id": per_pkg.case_id,
            "review_token": per_pkg.review_token,
            "subject_fingerprint": per_pkg.subject_fingerprint,
            "reviewer": "external-agent",
            "expires_at": time.time() + 3600,
            "reasons": ["ok"],
        }
        per_path = Path(d) / "per-review.json"

        def _try_record(extra: dict) -> bool:
            per_path.write_text(
                __import__("json").dumps({**base_row, **extra}), encoding="utf-8"
            )
            try:
                per_store.record_file(per_path, verify_attestation=lambda p, r: True)
                return True
            except ValueError:
                return False

        check("逐条验收: 缺 criteria_verdicts 的总体 pass 被拒",
              not _try_record({"verdict": "pass"}))
        check("逐条验收: criteria_verdicts 数量与标准不符被拒",
              not _try_record({"verdict": "pass", "criteria_verdicts": ["pass"]}))
        check("逐条验收: 有一条 fail 却报总体 pass 被拒",
              not _try_record({"verdict": "pass", "criteria_verdicts": ["pass", "fail"]}))
        check("逐条验收: 逐条全 pass 且顶层一致才通过",
              _try_record({"verdict": "pass", "criteria_verdicts": ["pass", "pass"]}))


def test_global_review_finds_shared_consumers_and_requires_record() -> None:
    import subprocess
    from kernel import analyze_global_impact, GlobalReview
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / "kernel").mkdir()
        (root / "app").mkdir()
        (root / "kernel" / "service.py").write_text("def shared_api():\n    return 1\n", encoding="utf-8")
        (root / "app" / "use.py").write_text(
            "from kernel.service import shared_api\nprint(shared_api())\n", encoding="utf-8"
        )
        (root / "kernel" / "config.py").write_text("PUBLIC_TIMEOUT: int = 10\n", encoding="utf-8")
        (root / "app" / "config_use.tsx").write_text(
            "export const value = PUBLIC_TIMEOUT;\n",
            encoding="utf-8",
        )
        (root / "app" / "script.py").write_text(
            "print('register side effect')\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        (root / "kernel" / "service.py").write_text("def shared_api():\n    return 2\n", encoding="utf-8")
        (root / "app" / "use.py").write_text(
            "from kernel.service import shared_api\nprint('changed', shared_api())\n",
            encoding="utf-8",
        )
        (root / "kernel" / "config.py").write_text("PUBLIC_TIMEOUT: int = 20\n", encoding="utf-8")
        (root / "app" / "script.py").unlink()
        review = analyze_global_impact(root)
        targets = [item.target for item in review.impacts]
        check("全局复核: 每个 impact target 唯一且可逐项记录",
              len(targets) == len(set(targets)))
        symbol = next(item for item in review.impacts if item.target == "kernel/service.py")
        check("全局复核: 公共定义改动能找到仓内调用方", "app/use.py" in symbol.consumers)
        constant = next(item for item in review.impacts if item.target == "kernel/config.py")
        check("全局复核: 模块级公共常量变更能找到调用方",
              "PUBLIC_TIMEOUT" in constant.reason
              and "app/config_use.tsx" in constant.consumers)
        check("全局复核: 删除仅含顶层副作用的源码仍生成文件级影响项",
              any(item.target == "app/script.py" for item in review.impacts))
        (root / "kernel" / "service.py").write_text("", encoding="utf-8")
        deleted_review = analyze_global_impact(root)
        deleted_surface = next(
            item for item in deleted_review.impacts if item.target == "kernel/service.py"
        )
        check("全局复核: 完整删除公共符号仍能找到调用方",
              "app/use.py" in deleted_surface.consumers
              and "shared_api" in deleted_surface.reason)
        (root / "kernel" / "service.py").write_text("def shared_api():\n    return 2\n", encoding="utf-8")
        check("全局复核: 未逐项记录前保持 pending", review.status == "pending")
        for item in review.impacts:
            review.verify(
                item.target, ["ev-review"],
                resolution=f"selftest covers {item.target}",
            )
        path = root / "review.json"
        review.save(path)
        check("全局复核: 所有影响项带证据后完成",
              GlobalReview.load(path).status == "completed")
        original_fingerprint = review.fingerprint
        (root / "app" / "use.py").write_text(
            "from kernel.service import shared_api\nprint(shared_api(), 'changed')\n",
            encoding="utf-8",
        )
        check("全局复核: 后续补丁会改变 fingerprint 使旧结论失效",
              analyze_global_impact(root).fingerprint != original_fingerprint)
        untracked = root / "new.py"
        untracked.write_text("def new_api():\n    return 1\n", encoding="utf-8")
        first_untracked = analyze_global_impact(root).fingerprint
        untracked.write_text("def new_api():\n    return 2\n", encoding="utf-8")
        check("全局复核: untracked 同行数同符号内容变化也使 fingerprint 失效",
              analyze_global_impact(root).fingerprint != first_untracked)
        legacy = review.to_dict()
        legacy.pop("fingerprint")
        path.write_text(__import__("json").dumps(legacy), encoding="utf-8")
        check("全局复核: 旧 review 可读取但 fingerprint 置空强制重验",
              GlobalReview.load(path).fingerprint == "")


def test_global_review_uses_task_base_and_explicit_subjects() -> None:
    import json
    import subprocess
    from kernel import (
        CapabilityResult,
        CapabilityStatus,
        Runtime,
        analyze_global_impact,
    )

    class FakePlugin:
        platform_id = "fake"
        def capabilities(self):
            return list(Capability)
        def invoke(self, capability, **kwargs):
            return CapabilityResult(
                "fake", capability.value, CapabilityStatus.SUCCESS,
                "verified", str(evidence_dir), [],
            )

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        data = Path(d) / "data"
        evidence_dir = Path(d) / "evidence"
        root.mkdir()
        evidence_dir.mkdir()
        (evidence_dir / "result.log").write_text("ok", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=root, check=True)
        source = root / "feature.py"
        source.write_text("def feature():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        no_diff_review = analyze_global_impact(root, base=base_commit)

        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        subject_attestation = {"allowed": False}

        def verifier(kind, path, row):
            return (
                subject_attestation["allowed"]
                if kind == "evidence_subjects"
                else True
            )

        runtime = Runtime(
            data, registry, FakePlugin(), project_root=root,
            attestation_verifier=verifier,
        )
        task = runtime.start(
            "重构公共模块",
            capabilities=["build"],
            executor_id="main-agent",
        )
        source.write_text("def feature():\n    return 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "task change"],
                       cwd=root, check=True)
        task.project_root = str(data)
        task.base_commit = "HEAD"
        review = runtime.prepare_global_review(task, root)
        task = runtime.execute_capabilities(
            task,
            ["build"],
            subjects="feature.py;tests/test_feature.py",
        )
        denied_evidence = runtime.evidence(task.id)[0]
        subject_attestation["allowed"] = True
        task = runtime.execute_capabilities(
            task,
            ["screenshot"],
            subjects="feature.py;tests/test_feature.py",
        )
        evidence = runtime.evidence(task.id)[-1]
        wrong_base_rejected = False
        try:
            runtime.prepare_global_review(task, root, base="HEAD")
        except ValueError:
            wrong_base_rejected = True
        review.base = "HEAD"
        review.status = "completed"
        review.save(task.global_review_path)
        _, forged_review_blockers = runtime.can_wrapup(task)
        check("全局复核: Task 创建时固定 Git base_commit",
              task.base_commit == base_commit
              and no_diff_review.status == "completed"
              and no_diff_review.impacts == [])
        check("全局复核: 改动提交后仍按任务起点发现完整 diff",
              "feature.py" in review.changed_files)
        check("全局复核: prepare 和最终 Gate 都不能偷换 base 或伪造完成",
              wrong_base_rejected
              and "global review base does not match task base_commit"
              in forged_review_blockers
              and any("global review incomplete:" in item
                      for item in forged_review_blockers))
        check("全局复核: CLI subjects 不能扩大任何能力的可信范围",
              denied_evidence.metadata["subjects"] == []
              and evidence.metadata["subjects"] == [])
        policy_path = runtime._task_policy_path(task.id)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy.pop("base_commit")
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        task.base_commit = ""
        Path(task.global_review_path).unlink()
        _, legacy_blockers = runtime.can_wrapup(task)
        check("全局复核: 旧 Task 缺不可变基线时结构化阻塞而非抛异常",
              any("task has no immutable Git base_commit" in item
                  for item in legacy_blockers))


def test_global_review_covers_behavior_dynamic_entries_and_tests() -> None:
    import subprocess
    from kernel import analyze_global_impact

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=root, check=True)
        (root / "Router.m").write_text(
            '- (void)refreshData { [router registerRoute:@"app://detail"]; }\n',
            encoding="utf-8",
        )
        (root / "Caller.m").write_text(
            '[router refreshData]; [app openURL:@"app://detail"];\n',
            encoding="utf-8",
        )
        (root / "Unrelated.m").write_text(
            "- (void)refreshData { NSLog(@\"unrelated\"); }\n",
            encoding="utf-8",
        )
        (root / "Wrapper.m").write_text(
            "- (void)wrapper { [router refreshData]; }\n",
            encoding="utf-8",
        )
        (root / "Loader.m").write_text(
            "- (void)load:(id)value\n"
            "    completion:(id)block\n"
            "{ return; }\n",
            encoding="utf-8",
        )
        (root / "RealLoaderCaller.m").write_text(
            "[loader load:value completion:block];\n",
            encoding="utf-8",
        )
        (root / "NestedLoaderCaller.m").write_text(
            "[loader load:[factory value] completion:block];\n",
            encoding="utf-8",
        )
        (root / "SplitSelector.m").write_text(
            "[first load:value]; [second completion:block];\n",
            encoding="utf-8",
        )
        (root / "routes.json").write_text(
            '{"route":"app://detail","enabled":true}\n',
            encoding="utf-8",
        )
        (root / "routes.yaml").write_text(
            'route: "app://yaml"\n',
            encoding="utf-8",
        )
        (root / "BUILD").write_text(
            'swift_library(name = "Feature")\n',
            encoding="utf-8",
        )
        (root / "Main.storyboard").write_text(
            '<view restorationIdentifier="app://detail"/>\n',
            encoding="utf-8",
        )
        tests = root / "tests"
        tests.mkdir()
        (tests / "RouterTests.swift").write_text(
            'func testRefreshData() { refreshData() }\n',
            encoding="utf-8",
        )
        (root / "Feature.swift").write_text(
            "final class Feature\n"
            "{\n"
            "    let title = \"old\"\n"
            "    let template = \"{ not a scope }\"\n"
            "    fileprivate var token = \"old\"\n"
            "    public private(set) var state = 1\n"
            "    func compute()\n"
            "    {\n"
            "        /* outer {\n"
            "           func phantom() {}\n"
            "           /* nested } */\n"
            "           } */\n"
            "        func helper() -> Int { return 1 }\n"
            "        let result = 1\n"
            "        print(result)\n"
            "    }\n"
            "}\n"
            "func topLevel() {\n"
            "    let localValue = 1\n"
            "    print(localValue)\n"
            "}\n",
            encoding="utf-8",
        )
        fixtures = root / "Fixtures"
        fixtures.mkdir()
        (fixtures / "RouterFixture.m").write_text(
            'registerRoute(@"app://fixture");\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        (root / "Router.m").write_text(
            '- (void)refreshData { [router registerRoute:@"app://detail-v2"]; }\n',
            encoding="utf-8",
        )
        (root / "Caller.m").write_text(
            '[router refreshData]; [app openURL:@"app://detail-v2"]; '
            '[app openURL:@"app://yaml-v2"];\n',
            encoding="utf-8",
        )
        (root / "Loader.m").write_text(
            "- (void)load:(id)value\n"
            "    completion:(id)block\n"
            "{ NSLog(@\"v2\"); }\n",
            encoding="utf-8",
        )
        (root / "routes.json").write_text(
            '{"route":"app://detail-v2","enabled":true}\n',
            encoding="utf-8",
        )
        (root / "routes.yaml").write_text(
            "route: app://yaml-v2\n",
            encoding="utf-8",
        )
        (root / "BUILD").write_text(
            'swift_library(name = "FeatureV2")\n',
            encoding="utf-8",
        )
        (root / "Main.storyboard").write_text(
            '<view restorationIdentifier="app://detail-v2"/>\n',
            encoding="utf-8",
        )
        (root / "Feature.swift").write_text(
            "final class Feature\n"
            "{\n"
            "    let title = \"new\"\n"
            "    let template = \"{ not a scope }\"\n"
            "    fileprivate var token = \"new\"\n"
            "    public private(set) var state = 2\n"
            "    func compute()\n"
            "    {\n"
            "        /* outer {\n"
            "           func phantom() {}\n"
            "           /* nested } */\n"
            "           } */\n"
            "        func helper() -> Int { return 2 }\n"
            "        let result = 2\n"
            "        print(result)\n"
            "    }\n"
            "}\n"
            "func topLevel() {\n"
            "    let localValue = 2\n"
            "    print(localValue)\n"
            "}\n",
            encoding="utf-8",
        )
        (fixtures / "RouterFixture.m").write_text(
            'registerRoute(@"app://fixture-v2");\n',
            encoding="utf-8",
        )
        review = analyze_global_impact(root)
        router = next(item for item in review.impacts
                      if item.target == "Router.m")
        loader = next(item for item in review.impacts
                      if item.target == "Loader.m")
        routes = next(item for item in review.impacts
                      if item.target == "routes.json")
        yaml_routes = next(item for item in review.impacts
                           if item.target == "routes.yaml")
        build_file = next(item for item in review.impacts
                          if item.target == "BUILD")
        storyboard = next(item for item in review.impacts
                          if item.target == "Main.storyboard")
        check("全局复核: Objective-C selector 和消息式入口找到调用方",
              "refreshData" in review.changed_symbols
              and "app://detail-v2" in router.entry_points
              and "Caller.m" in router.consumers
              and "Wrapper.m" in router.consumers
              and "Unrelated.m" not in router.consumers)
        check("全局复核: Objective-C 多段 selector 限定同一条消息",
              "load:completion:" in review.changed_symbols
              and "RealLoaderCaller.m" in loader.consumers
              and "NestedLoaderCaller.m" in loader.consumers
              and "SplitSelector.m" not in loader.consumers)
        check("全局复核: 字符串路由形成动态入口和调用方",
              "app://detail-v2" in router.entry_points
              and "routes.json" in router.consumers
              and "app://yaml-v2" in yaml_routes.entry_points
              and "Caller.m" in yaml_routes.consumers)
        check("全局复核: 配置与 iOS 界面行为文件逐项建 impact",
              routes.kind == "behavioral_file"
              and storyboard.kind == "behavioral_file"
              and build_file.kind == "behavioral_file")
        check("全局复核: 影响项给出确定性测试建议",
              "tests/RouterTests.swift" in router.suggested_tests)
        fixture = next(item for item in review.impacts
                       if item.target == "Fixtures/RouterFixture.m")
        check("全局复核: Swift 成员属性保留并过滤注释定义与局部变量",
              "title" in review.changed_symbols
              and "token" in review.changed_symbols
              and "state" in review.changed_symbols
              and "compute" in review.changed_symbols
              and "phantom" not in review.changed_symbols
              and "helper" not in review.changed_symbols
              and "result" not in review.changed_symbols
              and "localValue" not in review.changed_symbols)
        check("全局复核: Fixtures/Mocks 不冒充生产动态入口",
              fixture.entry_points == []
              and "Fixtures/RouterFixture.m" not in router.consumers)


def test_global_review_requires_consumer_evidence() -> None:
    import subprocess
    from kernel import CapabilityResult, CapabilityStatus, GlobalReview, Runtime

    class FakePlugin:
        platform_id = "fake"
        def capabilities(self):
            return list(Capability)
        def invoke(self, capability, **kwargs):
            return CapabilityResult(
                "fake", capability.value, CapabilityStatus.SUCCESS,
                "verified", "", [],
            )

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        data = Path(d) / "data"
        (root / "kernel").mkdir(parents=True)
        (root / "app").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=root, check=True)
        (root / "kernel" / "service.py").write_text(
            "def shared_api():\n    return 1\n", encoding="utf-8"
        )
        (root / "app" / "use.py").write_text(
            "from kernel.service import shared_api\nprint(shared_api())\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        (root / "kernel" / "service.py").write_text(
            "def shared_api():\n    return 2\n", encoding="utf-8"
        )
        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        runtime = Runtime(data, registry, FakePlugin(), project_root=root)
        task = runtime.start("重构公共模块", executor_id="main-agent")
        review = runtime.prepare_global_review(task, root)
        evidence = observed(
            "service verified",
            metadata={
                "task_id": task.id,
                "run_id": "review-run-1",
                "flow_id": task.flow_id,
                "subjects": ["kernel/service.py"],
            },
            evidence_id="ev-review-target-only",
        )
        runtime._append_evidence(task.id, evidence)
        task.evidence_ids.append(evidence.id)
        for impact in review.impacts:
            review.verify(
                impact.target,
                [evidence.id],
                resolution=f"checked {impact.target}",
            )
        review.save(task.global_review_path)
        task.global_review_status = "completed"
        runtime.tasks.save(task)
        _, blockers = runtime.can_wrapup(task)
        check("全局复核: 只验证定义文件不能跳过调用方",
              any("app/use.py" in blocker and "uncovered subjects" in blocker
                  for blocker in blockers))
        evidence.created_at = review.created_at - 1
        runtime._append_evidence(task.id, evidence)
        _, stale_blockers = runtime.can_wrapup(task)
        check(
            "全局复核: 改动前证据不能关闭当前 review",
            any("uses evidence older than the review" in blocker
                for blocker in stale_blockers),
        )
        tampered_review = GlobalReview.load(task.global_review_path)
        tampered_review.impacts.pop()
        tampered_review.save(task.global_review_path)
        _, tampered_blockers = runtime.can_wrapup(task)
        check("全局复核: 删除 impact 但保留旧 fingerprint 会被识别",
              any("impact scope was altered" in blocker
                  for blocker in tampered_blockers))
        spoof_path = Path(d) / "fake-user-confirmation.json"
        spoof_path.write_text('{"confirmed":true}', encoding="utf-8")
        spoof = EvidenceArtifact(
            capability="user_confirmation",
            source="forged-json",
            kind="observed",
            outcome="success",
            summary="accept risk",
            path=str(spoof_path),
            metadata={
                "task_id": task.id,
                "run_id": "forged-user-run",
                "flow_id": task.flow_id,
                "subjects": [review.impacts[0].target],
                "human_confirmed": True,
                "user_id": "forged-user",
                "attestation_sha256": __import__("hashlib").sha256(
                    spoof_path.read_bytes()
                ).hexdigest(),
            },
            id="ev-forged-human",
        )
        runtime._append_evidence(task.id, spoof)
        task.evidence_ids.append(spoof.id)
        accepted_review = runtime.prepare_global_review(task, root)
        for index, impact in enumerate(accepted_review.impacts):
            accepted_review.verify(
                impact.target,
                [] if index == 0 else [evidence.id],
                accepted=index == 0,
                resolution=f"checked {impact.target}",
                user_confirmation_id=spoof.id if index == 0 else "",
            )
        accepted_review.save(task.global_review_path)
        task.global_review_status = "completed"
        runtime.tasks.save(task)
        _, accepted_blockers = runtime.can_wrapup(task)
        check("全局复核: 直接伪造 human_confirmed JSON 不能接受风险",
              any("lacks user confirmation" in blocker
                  for blocker in accepted_blockers))


def test_ui_flow_verification_requires_evidence() -> None:
    from kernel import UIFlowStore
    with tempfile.TemporaryDirectory() as d:
        store = UIFlowStore(d)
        flow = store.create("登录路径", "看到首页")
        rejected = False
        try:
            store.verify(flow.id, {})
        except ValueError:
            rejected = True
        flow = store.load(flow.id)
        flow.task_id = "task-ui"
        flow.verification_run_id = "flow-run-1"
        flow.device = "ios-simulator"
        flow.device_id = "SIM-1"
        flow.nodes[0].evidence_ids = ["ev-launch"]
        flow.nodes[-1].evidence_ids = ["ev-ui"]
        store.save(flow)
        spoofed = False
        try:
            store.verify(flow.id, {})
        except ValueError:
            spoofed = True
        failed_evidence = observed(
            "failed tree", capability="view_tree",
            evidence_id="ev-ui", outcome="failure", metadata={
                "task_id": "task-ui",
                "flow_id": "core.verify", "ui_flow_id": flow.id,
                "flow_run_id": "flow-run-1",
                "device": "ios-simulator", "device_id": "SIM-1",
            },
        )
        failed_rejected = False
        try:
            store.verify(
                flow.id,
                {"ev-ui": failed_evidence},
                verify_attestation=lambda path, row: True,
            )
        except ValueError:
            failed_rejected = True
        good_evidence = observed(
            "target visible", capability="view_tree",
            evidence_id="ev-ui", metadata={
                "task_id": "task-ui",
                "flow_id": "core.verify", "ui_flow_id": flow.id,
                "flow_run_id": "flow-run-1",
                "device": "ios-simulator", "device_id": "SIM-1",
            },
        )
        launch_evidence = observed(
            "app launched", capability="launch",
            evidence_id="ev-launch", metadata={
                "task_id": "task-ui",
                "flow_id": "core.verify", "ui_flow_id": flow.id,
                "flow_run_id": "flow-run-1",
                "device": "ios-simulator", "device_id": "SIM-1",
            },
        )
        verified = store.verify(
            flow.id,
            {
                "ev-ui": good_evidence,
                "ev-launch": launch_evidence,
            },
            verify_attestation=lambda path, row: True,
        )
        check("UI Flow: 无验证节点证据不能标 verified", rejected)
        check("UI Flow: 任意字符串 evidence id 不能标 verified", spoofed)
        check("UI Flow: 失败证据不能标 verified", failed_rejected)
        check("UI Flow: 补证后可验证并持久化", verified.status == "verified")
        auto = store.create("自动绑定", "看到详情")
        auto_launch = observed(
            "launched",
            capability="launch",
            metadata={
                "task_id": "task-auto",
                "run_id": "run-launch",
                "flow_id": "core.verify",
                "ui_flow_id": auto.id,
                "flow_run_id": "flow-auto",
                "device": "ios-simulator",
                "device_id": "SIM-2",
            },
        )
        auto_tree = observed(
            "details visible",
            capability="view_tree",
            metadata={
                "task_id": "task-auto",
                "run_id": "run-tree",
                "flow_id": "core.verify",
                "ui_flow_id": auto.id,
                "flow_run_id": "flow-auto",
                "device": "ios-simulator",
                "device_id": "SIM-2",
            },
        )
        auto = store.verify(
            auto.id,
            {auto_launch.id: auto_launch, auto_tree.id: auto_tree},
            verify_attestation=lambda path, row: True,
            bindings={
                "task_id": "task-auto",
                "verification_run_id": "flow-auto",
                "device": "ios-simulator",
                "device_id": "SIM-2",
            },
        )
        check(
            "UI Flow: 任务证据按节点能力自动回绑定",
            [node.evidence_ids for node in auto.nodes]
            == [[auto_launch.id], [auto_tree.id]],
        )
        before = auto.to_dict()
        try:
            store.verify(
                auto.id,
                {auto_launch.id: auto_launch, auto_tree.id: auto_tree},
                verify_attestation=lambda path, row: True,
                bindings={"task_id": "wrong-task"},
            )
        except ValueError:
            pass
        check(
            "UI Flow: 失败验证不污染已验证状态",
            store.load(auto.id).to_dict() == before,
        )
        rebound = False
        try:
            store.bind_task(
                auto.id,
                "another-task",
                "another-run",
                "ios-simulator",
                "SIM-3",
            )
        except ValueError:
            rebound = True
        check("UI Flow: 已绑定 Task/Run 不可被二次覆盖", rebound)
        reserved = store.create("并发占位", "看到页面")
        store.reserve_task(reserved.id, "owner-a")
        competing_reservation_rejected = False
        try:
            store.reserve_task(reserved.id, "owner-b")
        except ValueError:
            competing_reservation_rejected = True
        stale = store.load(reserved.id)
        stale.binding_started_at = 1.0
        store.save(stale)
        reclaimed = store.reserve_task(reserved.id, "owner-b")
        store.release_task_reservation(reserved.id, "owner-b")
        check(
            "UI Flow: reservation 阻断并发转换且超时可恢复",
            competing_reservation_rejected
            and reclaimed.binding_token == "owner-b"
            and not store.load(reserved.id).binding_token,
        )


def test_release_audit_regressions() -> None:
    import hashlib
    import json
    import subprocess
    from kernel import (
        CapabilityResult,
        CapabilityStatus,
        HostTrustStore,
        ProjectMemory,
        Runtime,
        StepStatus,
        TaskStep,
        TaskStatus,
        UIFlowStore,
        has_errors,
        load_installed_extensions,
        load_installed_plugins,
        scaffold_extension,
        validate_extension,
    )
    from kernel.gate_capability import OpStatus

    class FakePlugin:
        platform_id = "fake"

        def capabilities(self):
            return list(Capability)

        def invoke(self, capability, **kwargs):
            evidence_root = Path(d) / "fake-plugin-evidence" / str(
                kwargs.get("run_id", capability.value)
            ).replace(":", "-")
            evidence_root.mkdir(parents=True, exist_ok=True)
            (evidence_root / "proof.log").write_text(
                "verified", encoding="utf-8"
            )
            return CapabilityResult(
                "fake",
                capability.value,
                CapabilityStatus.SUCCESS,
                "verified",
                str(evidence_root),
            )

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        data = Path(d) / "data"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=root, check=True)
        (root / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        registry = FlowRegistry()
        registry.load_json(ROOT / "workflow" / "flows.json")
        trust = HostTrustStore(Path(d) / "trust")
        external_rejected = False
        external_path = Path(d) / "forged-review.json"
        external_path.write_text('{"verdict":"pass"}', encoding="utf-8")
        try:
            trust.attest(
                "independent_review",
                external_path,
                {"verdict": "pass"},
            )
        except ValueError:
            external_rejected = True
        check(
            "宿主: 本地账本拒绝签发外部独立验收",
            external_rejected
            and not trust.verify(
                "independent_review",
                external_path,
                {"verdict": "pass"},
            ),
        )
        runtime = Runtime(
            data,
            registry,
            FakePlugin(),
            project_root=root,
            attestation_verifier=trust.verify,
            attestation_recorder=trust.attest,
        )
        task = runtime.start("验证配置", executor_id="main-agent")
        artifact = Path(d) / "proof.log"
        artifact.write_text(
            "对齐现象与范围 建立候选假设 执行最有区分度的取证 四关收敛",
            encoding="utf-8",
        )
        for gate in ("time", "scope", "mechanism", "counter_evidence"):
            evidence = observed(
                f"{gate} verified",
                metadata={
                    "task_id": task.id,
                    "run_id": f"run-{gate}",
                    "flow_id": task.flow_id,
                    "gates": [gate],
                },
            )
            runtime._seal_trusted_evidence(task, evidence)
            runtime._append_evidence(task.id, evidence)
            task.evidence_ids.append(evidence.id)
            case = Case.load(task.case_path)
            case.attach("h1", evidence, gate=gate)
            case.save(task.case_path)
            for step in task.steps:
                if not step.evidence_ids:
                    step.evidence_ids = [evidence.id]
                    step.status = StepStatus.DONE
                    step.completion_source = "machine"
        case = Case.load(task.case_path)
        case.try_resolve(
            lambda path, row: trust.verify("evidence", path, row),
            {"task_id": task.id, "flow_id": task.flow_id},
        )
        case.save(task.case_path)
        runtime.tasks.save(task)
        journal = runtime._task_dir(task.id) / "transaction.json"
        journal.write_text('{"operation":"interrupted"}', encoding="utf-8")
        _, transaction_blockers = runtime.can_wrapup(task)
        check("事务: 中断留下 journal 时禁止静默收口",
              any("incomplete task transaction" in item
                  for item in transaction_blockers))
        journal.unlink()
        completed = runtime.complete(task)
        check("宿主: managed trust store 可完成真实 wrapup",
              completed.status == TaskStatus.DONE)

        task_path = runtime.tasks.path_for(task.id)
        raw = json.loads(task_path.read_text(encoding="utf-8"))
        raw["steps"] = []
        raw["acceptance"] = []
        task_path.write_text(json.dumps(raw), encoding="utf-8")
        restored = runtime.load(task.id)
        check("策略派生: 删除步骤和验收标准会从宿主证明恢复",
              len(restored.steps) == 4)

        first = runtime.tasks.create(
            "same", goal="one", flow_id="core.verify", autonomy="L1"
        )
        second = runtime.tasks.create(
            "same", goal="two", flow_id="core.verify", autonomy="L1"
        )
        check("任务: 同秒同标题仍生成唯一 ID", first.id != second.id)
        stale = runtime.tasks.load(first.id)
        current = runtime.tasks.load(first.id)
        current.goal = "updated"
        runtime.tasks.save(current)
        stale_rejected = False
        try:
            runtime.tasks.save(stale)
        except RuntimeError:
            stale_rejected = True
        check("任务: 并发陈旧写入被 revision CAS 拒绝", stale_rejected)

        l2_rejected = False
        runtime_without_root = Runtime(data / "no-root", registry, FakePlugin())
        try:
            runtime_without_root.start("实现功能")
        except ValueError:
            l2_rejected = True
        check("任务: L2 缺 project_root 时创建前拒绝", l2_rejected)

        gate = CapabilityGate()
        gate.require("op-b", "required", task_id="task-b")
        receipt = Path(d) / "cancel.json"
        receipt.write_text(json.dumps({
            "operation_id": "op-a",
            "task_id": "task-a",
            "confirmed": True,
            "reason": "cancel",
            "user_id": "user",
            "expires_at": time.time() + 3600,
        }), encoding="utf-8")
        operation = gate._ops["op-b"]
        operation.status = OpStatus.CANCELLED
        operation.cancelled_by_user = True
        operation.cancellation_reason = "cancel"
        operation.cancellation_attestation_path = str(receipt)
        operation.cancellation_attestation_sha256 = hashlib.sha256(
            receipt.read_bytes()
        ).hexdigest()
        check("能力Gate: 跨任务取消凭证不能重放",
              not gate.can_wrapup(lambda kind, path, row: True,
                                  expected_task_id="task-b")[0])
        replay_gate = CapabilityGate()
        first_requirement = replay_gate.require(
            "platform.read", "read", task_id="task-replay"
        )
        completion = Path(d) / "completion.json"
        completion.write_text(json.dumps({
            "operation_id": "platform.read",
            "task_id": "task-replay",
            "requirement_id": first_requirement.requirement_id,
            "required_at": first_requirement.created_at,
            "created_at": time.time(),
            "kind": "observed",
            "outcome": "success",
            "expires_at": time.time() + 3600,
        }), encoding="utf-8")
        replay_gate.complete(
            "platform.read",
            str(completion),
            verify_attestation=lambda path, row: True,
        )
        replay_gate.require("platform.read", "read again", task_id="task-replay")
        replay_rejected = False
        try:
            replay_gate.complete(
                "platform.read",
                str(completion),
                verify_attestation=lambda path, row: True,
            )
        except ValueError:
            replay_rejected = True
        check("能力Gate: 旧 receipt 不能关闭重新签发的 requirement",
              replay_rejected)

        attestation_path = Path(d) / "package.json"
        attestation_path.write_text("{}", encoding="utf-8")
        package_a = {"criteria": ["A"]}
        package_b = {"criteria": ["B"]}
        trust.attest("acceptance_package", attestation_path, package_a)
        trust.attest("acceptance_package", attestation_path, package_b)
        check("宿主: 同一路径只接受最新证明防止 package rollback",
              not trust.verify(
                  "acceptance_package", attestation_path, package_a
              )
              and trust.verify(
                  "acceptance_package", attestation_path, package_b
              ))

        future = observed(
            "future",
            metadata={
                "task_id": task.id,
                "run_id": "future",
                "flow_id": task.flow_id,
            },
        )
        future.created_at = time.time() + 3600
        check("证据: 未来时间戳不能绕过 review freshness",
              not future.supports_success(lambda path, row: True))

        interrupted = runtime._task_dir(task.id) / "partial.txt"
        try:
            with runtime._transaction(task.id, "partial-test"):
                interrupted.write_text("partial", encoding="utf-8")
                raise OSError("simulated interruption")
        except OSError:
            pass
        check("事务: 部分写入异常保留恢复 journal",
              (runtime._task_dir(task.id) / "transaction.json").is_file())
        (runtime._task_dir(task.id) / "transaction.json").unlink()

        flow_store = UIFlowStore(data)
        traversal_rejected = False
        try:
            flow_store.load("../../outside")
        except ValueError:
            traversal_rejected = True
        blocker_rejected = False
        memory = ProjectMemory(data)
        try:
            memory.emit_blocker(
                "../outside",
                reason="reason",
                evidence=["e"],
                options=["o"],
                recommendation="r",
            )
        except ValueError:
            blocker_rejected = True
        check("路径: UI Flow 和 blocker 标识不能越界",
              traversal_rejected and blocker_rejected)
        blocker_one = memory.emit_blocker(
            task.id,
            reason="first",
            evidence=["e1"],
            options=["o1"],
            recommendation="r1",
        )
        blocker_two = memory.emit_blocker(
            task.id,
            reason="second",
            evidence=["e2"],
            options=["o2"],
            recommendation="r2",
        )
        check(
            "blocker: 同秒连续记录不覆盖",
            blocker_one != blocker_two
            and blocker_one.is_file()
            and blocker_two.is_file(),
        )

        extensions = Path(d) / "extensions"
        extension = scaffold_extension("team.audit", extensions)
        core_ids = {flow.flow_id for flow in registry.all()}
        check("扩展: 脚手架可立即通过校验",
              not has_errors(validate_extension(extension, core_flow_ids=core_ids)))
        malformed = extensions / "team.bad"
        malformed.mkdir()
        (malformed / "manifest.json").write_text(
            '{"name":"team.bad","provides":[]}',
            encoding="utf-8",
        )
        (malformed / "flows.json").write_text("[]", encoding="utf-8")
        isolated_registry = FlowRegistry()
        isolated_registry.load_json(ROOT / "workflow" / "flows.json")
        _, issues = load_installed_extensions(isolated_registry, extensions)
        check("扩展: 畸形扩展只隔离自身不阻断其他 flow",
              bool(issues) and isolated_registry.plan("示例") is not None)

        future = scaffold_extension("team.future", extensions)
        future.manifest["iloop_kernel"] = ">=999.0.0"
        (future.root / "manifest.json").write_text(
            json.dumps(future.manifest),
            encoding="utf-8",
        )
        check("扩展: 不兼容内核版本被拒绝",
              has_errors(validate_extension(future)))

        for name in ("team.plugin_a", "team.plugin_b"):
            duplicate = scaffold_extension(name, extensions)
            duplicate.manifest["provides"]["plugin"] = "plugin.py"
            (duplicate.root / "manifest.json").write_text(
                json.dumps(duplicate.manifest),
                encoding="utf-8",
            )
            (duplicate.root / "plugin.py").write_text(
                "from kernel import Capability, CapabilityResult, CapabilityStatus\n"
                "class Duplicate:\n"
                " platform_id='duplicate_platform'\n"
                " def capabilities(self): return [Capability.PROBE]\n"
                " def invoke(self, capability, **kwargs):\n"
                "  return CapabilityResult(self.platform_id, capability.value, CapabilityStatus.SUCCESS, 'ok')\n"
                "def create_plugin(config): return Duplicate()\n",
                encoding="utf-8",
            )
        plugin_issues = []
        duplicate_plugins = load_installed_plugins(
            extensions,
            issues=plugin_issues,
        )
        check(
            "扩展: 重复 platform_id fail closed 并报告双方来源",
            not any(
                item.platform_id == "duplicate_platform"
                for item in duplicate_plugins
            )
            and any(
                "duplicate platform_id 'duplicate_platform'" in issue.message
                for issue in plugin_issues
            ),
        )

        check("路由: 单独回归命中 L1 验证 flow",
              registry.plan("回归").flow_id == "core.verify")
        custom_steps = [
            TaskStep(title="launch: app", capability="launch"),
            TaskStep(title="assert: home", capability="view_tree"),
        ]
        custom_task = runtime_without_root.start(
            "验证 UI 路径",
            steps=custom_steps,
        )
        restored_custom = runtime_without_root.load(custom_task.id)
        check("UI Flow: 自定义步骤在 policy 创建时固化",
              [step.title for step in restored_custom.steps]
              == ["launch: app", "assert: home"])

        scoped_task = runtime.start(
            "重构范围证明",
            executor_id="main-agent",
        )
        (root / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        runtime.prepare_global_review(scoped_task, root)
        scoped_task = runtime.execute_capabilities(
            scoped_task,
            ["probe"],
            subjects="feature.py",
        )
        scoped_evidence = runtime.evidence(scoped_task.id)[-1]
        check("宿主: 本地 host 不替调用者自动证明 subjects",
              scoped_evidence.metadata["subjects"] == [])


def run() -> int:
    for fn in [
        test_evidence_observed_vs_inferred,
        test_capability_unsupported_not_crash,
        test_flow_no_clobber,
        test_flow_plan_routing,
        test_lesson_recall,
        test_task_ids_cannot_escape_data_dir,
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
        test_runner_does_not_wait_for_detached_child_output,
        test_runner_timeout_kills_remaining_process_group,
        test_runner_legacy_constructor_and_cross_platform_cleanup,
        test_runner_cleanup_ignores_second_interrupt,
        test_direct_cli_uses_project_data_dir,
        test_dashboard_metrics_and_render,
        test_flow_next_suggest,
        test_case_and_ledger_resume,
        test_runtime_task_resume_and_evidence,
        test_runtime_logs_follow_successful_run_id,
        test_policy_and_constitution_cannot_be_forged_by_cli_state,
        test_attested_evidence_revalidates_exact_scope,
        test_wrapup_cannot_bypass_vdd_gates,
        test_failure_observation_does_not_pass_gate,
        test_external_acceptance_is_persistent_and_not_self_reviewed,
        test_global_review_finds_shared_consumers_and_requires_record,
        test_global_review_uses_task_base_and_explicit_subjects,
        test_global_review_covers_behavior_dynamic_entries_and_tests,
        test_global_review_requires_consumer_evidence,
        test_ui_flow_verification_requires_evidence,
        test_release_audit_regressions,
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
