#!/usr/bin/env python3
"""iLoop CLI —— 内核的命令入口（薄封装，把四协议 + 插件跑起来）。

用法:
  python3 -m cli plan "<任务>"              # flow 路由 + 自治分级
  python3 -m cli flows                      # 列出已加载 flow
  python3 -m cli experts "<任务>"           # 诊断方法专家路由
  python3 -m cli doctor [--real]            # iOS 插件依赖体检
  python3 -m cli invoke <capability> [--real] [k=v ...]   # 真调 iOS 能力
  python3 -m cli oncall-demo                # 演示：同一内核驱动 oncall 诊断
  python3 -m cli extension-init <team.ext> [dir]   # 创建业务扩展包骨架
  python3 -m cli extension-validate <dir>          # 校验扩展包（二开硬边界）
  python3 -m cli selftest                   # 内核 + 插件自测，全绿才算完成
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kernel import (  # noqa: E402
    FlowRegistry, Capability, ExpertRegistry, render,
    Case, EvidenceArtifact, StaticEventSource, Event, StdoutNotifier,
)
from plugins.ios_native import IOSNativePlugin  # noqa: E402

FLOWS_JSON = ROOT / "workflow" / "flows.json"


def _registry() -> FlowRegistry:
    reg = FlowRegistry()
    reg.load_json(FLOWS_JSON)
    return reg


def cmd_plan(task: str) -> int:
    flow = _registry().plan(task)
    if not flow:
        print(render("plan", f"命中flow=none · 任务=<{task}> · 请澄清方向"))
        return 0
    print(render("plan", f"命中flow={flow.flow_id}（{flow.name}）· 自治档={flow.autonomy.value}",
                 basis=", ".join(flow.when_keywords), result=flow.evidence_strategy))
    print(f"  required_docs={flow.required_docs} · 升级条件={flow.escalate_when}")
    if flow.next_suggest:
        print(render("wrapup", f"🧭 下一步预备：{flow.next_suggest}"))
    return 0


def cmd_flows() -> int:
    for f in _registry().all():
        print(f"  {f.flow_id} [{f.autonomy.value}]: {f.name} · 触发={f.when_keywords}")
    return 0


def cmd_experts(task: str) -> int:
    hits = ExpertRegistry().route(task)
    if not hits:
        print(render("evidence", f"无专家命中 · 任务=<{task}>"))
        return 0
    for e in hits:
        print(f"  {e.id}（{e.name}）· 想要证据={e.wants_capabilities} · 候选假设={e.default_hypotheses[:1]}")
    return 0


def cmd_doctor(real: bool) -> int:
    plugin = IOSNativePlugin(mode="real" if real else "simulator")
    res = plugin.invoke(Capability.DOCTOR)
    icon = "✅" if res.ok() else "⛔"
    print(render("connect", f"{icon} [{res.platform}] {res.summary}"))
    return 0 if res.ok() else 1


def cmd_invoke(cap_name: str, real: bool, kwargs: dict) -> int:
    try:
        cap = Capability(cap_name)
    except ValueError:
        print(f"unknown capability: {cap_name}；可选: {[c.value for c in Capability]}")
        return 2
    plugin = IOSNativePlugin(mode="real" if real else "simulator", config=kwargs)
    res = plugin.invoke(cap, **kwargs)
    icon = {"success": "✅", "unsupported": "⚠️", "error": "⛔"}[res.status.value]
    print(render("build" if cap == Capability.BUILD else "evidence",
                 f"{icon} [{res.platform}] {res.capability}: {res.summary}",
                 result=(f"证据={res.evidence_dir}" if res.evidence_dir else "")))
    return 0 if res.status.value != "error" else 1


def cmd_oncall_demo() -> int:
    """演示：同一内核驱动一个事件驱动的 oncall 诊断 Agent（无平台绑定）。"""
    src = StaticEventSource([Event(id="alarm-1", title="下单页崩溃率上涨",
                                   body="iOS 6.3.0 崩溃聚类，涉及订单详情")])
    notifier = StdoutNotifier()
    for ev in src.poll():
        print(render("connect", f"收到事件 {ev.id}: {ev.title}"))
        case = Case(ev.id, ev.title)
        h = case.add_hypothesis("客户端代码路径触发崩溃")
        obs = EvidenceArtifact(capability="crash", source="oncall.demo",
                               kind="observed", summary="堆栈指向订单详情控制器")
        case.attach(h.id, obs, gate="mechanism")
        for g in ("time", "scope", "counter_evidence"):
            case.bind_gate(g, EvidenceArtifact(capability="logs", source="oncall.demo",
                                               kind="observed", summary=f"{g} 证据"))
        ok, msg = case.try_resolve()
        print(render("verify" if ok else "blocked", msg))
        notifier.send(f"[iLoop] {ev.title} 诊断结论", msg)
    return 0


def cmd_selftest() -> int:
    from selftest import run
    return run()


def cmd_extension_init(name: str, base_dir: str) -> int:
    from kernel import scaffold_extension
    ext = scaffold_extension(name, base_dir)
    print(render("connect", f"扩展包已创建: {ext.root}", result="只改此目录，核心整体只读"))
    print(f"  下一步：编辑 {ext.root}/flows.json（flow_id 须以 {name}. 为前缀），再 extension-validate")
    return 0


def cmd_extension_validate(root: str) -> int:
    from kernel import load_extension, validate_extension, has_errors
    reg = _registry()
    core_ids = {f.flow_id for f in reg.all()}
    ext = load_extension(root)
    issues = validate_extension(ext, core_flow_ids=core_ids)
    if not issues:
        print(render("verify", f"✅ 扩展 {ext.name} 校验通过"))
        return 0
    for i in issues:
        icon = "⛔" if i.level == "error" else "⚠️"
        print(f"  {icon} [{i.level}] {i.message}")
    return 1 if has_errors(issues) else 0


def _parse_kv(rest: list[str]) -> dict:
    kv = {}
    for tok in rest:
        if "=" in tok and not tok.startswith("--"):
            k, v = tok.split("=", 1)
            kv[k] = v
    return kv


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, *rest = argv
    real = "--real" in rest
    if cmd == "plan":
        return cmd_plan(" ".join(t for t in rest if not t.startswith("--")))
    if cmd == "flows":
        return cmd_flows()
    if cmd == "experts":
        return cmd_experts(" ".join(t for t in rest if not t.startswith("--")))
    if cmd == "doctor":
        return cmd_doctor(real)
    if cmd == "invoke":
        if not rest:
            print("usage: invoke <capability> [--real] [k=v ...]")
            return 2
        return cmd_invoke(rest[0], real, _parse_kv(rest[1:]))
    if cmd == "oncall-demo":
        return cmd_oncall_demo()
    if cmd == "extension-init":
        if not rest:
            print("usage: extension-init <team.ext> [dir]")
            return 2
        pos = [t for t in rest if not t.startswith("--")]
        base = pos[1] if len(pos) > 1 else str(Path.home() / ".iloop" / "extensions")
        return cmd_extension_init(pos[0], base)
    if cmd == "extension-validate":
        pos = [t for t in rest if not t.startswith("--")]
        if not pos:
            print("usage: extension-validate <dir>")
            return 2
        return cmd_extension_validate(pos[0])
    if cmd == "selftest":
        return cmd_selftest()
    print(f"unknown command: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
