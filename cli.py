#!/usr/bin/env python3
"""iLoop CLI —— 内核的命令入口（薄封装，把四协议 + 插件跑起来）。

用法:
  python3 -m cli plan "<任务>"              # flow 路由 + 自治分级
  python3 -m cli run "<任务>" [caps=build,run,logs] [k=v ...]
  python3 -m cli resume <task_id> [caps=...] [k=v ...]
  python3 -m cli tasks [--all]              # 可恢复任务与下一步
  python3 -m cli task show|step|complete ...
  python3 -m cli case show|tick|resolve <task_id>
  python3 -m cli accept <task_id>
  python3 -m cli wrapup <task_id>
  python3 -m cli round start|end <task_id> [...]
  python3 -m cli lessons search|add ...
  python3 -m cli dashboard <task_id>
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

import os
import sys
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kernel import (  # noqa: E402
    FlowRegistry, Capability, ExpertRegistry, render,
    Case, EvidenceArtifact, StaticEventSource, Event, StdoutNotifier,
    Runtime, TaskStore, TaskStatus, StepStatus, Lesson, LessonBook, Ledger, RoundStatus,
    AcceptancePackage, IndependentReviewer, Verdict,
)
from plugins.ios_native import IOSNativePlugin  # noqa: E402

FLOWS_JSON = ROOT / "workflow" / "flows.json"


def _data_dir(project_root: str = "") -> Path:
    explicit = os.environ.get("ILOOP_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    project = str(Path(project_root or os.environ.get("ILOOP_PROJECT_ROOT") or Path.cwd()).expanduser().resolve())
    project_id = f"{Path(project).name}-{hashlib.sha1(project.encode()).hexdigest()[:8]}"
    return Path.home() / ".iloop" / "data" / project_id


def _runtime(real: bool, kwargs: dict) -> Runtime:
    project_root = kwargs.get("project_root") or os.environ.get("ILOOP_PROJECT_ROOT", "")
    data_dir = _data_dir(project_root)
    if project_root:
        for key in ("project", "workspace"):
            value = kwargs.get(key)
            if value and not Path(value).expanduser().is_absolute():
                kwargs[key] = str(Path(project_root).expanduser() / value)
    plugin = IOSNativePlugin(
        mode="real" if real else "simulator",
        data_dir=str(data_dir / "platform"),
        config=kwargs,
    )
    return Runtime(data_dir, _registry(), plugin)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", ";").split(";") if part.strip()]


def _print_resume_card(runtime: Runtime, task) -> None:
    card = runtime.tasks.resume_card(task)
    print(render("plan", f"task={task.id} · flow={task.flow_id} · 状态={task.status.value}",
                 result=f"当前阶段={task.current_stage} · 下一步={card['next_step'] or '待收口'}"))
    if card["constraints"]:
        print(f"  不可遗忘约束: {'；'.join(card['constraints'])}")
    print(f"  data={runtime.data_dir}")


def cmd_run(task_text: str, real: bool, kwargs: dict) -> int:
    runtime = _runtime(real, kwargs)
    capabilities = _split(kwargs.pop("caps", kwargs.pop("capabilities", "")))
    task = runtime.start(
        task_text,
        constraints=_split(kwargs.pop("constraints", "")),
        acceptance=_split(kwargs.pop("acceptance", "")),
        capabilities=capabilities,
    )
    if capabilities:
        task = runtime.execute_capabilities(task, capabilities, **kwargs)
    _print_resume_card(runtime, task)
    return 1 if task.status == TaskStatus.BLOCKED else 0


def cmd_resume(task_id: str, real: bool, kwargs: dict) -> int:
    runtime = _runtime(real, kwargs)
    task = runtime.load(task_id)
    capabilities = _split(kwargs.pop("caps", kwargs.pop("capabilities", "")))
    if capabilities:
        task = runtime.execute_capabilities(task, capabilities, **kwargs)
    _print_resume_card(runtime, task)
    return 1 if task.status == TaskStatus.BLOCKED else 0


def cmd_tasks(include_done: bool = False) -> int:
    store = TaskStore(_data_dir())
    tasks = store.list(include_done=include_done)
    if not tasks:
        print(render("plan", f"暂无任务 · data={_data_dir()}"))
        return 0
    for task in tasks:
        card = store.resume_card(task)
        print(f"  [{task.status.value}] {task.id} · {task.title}")
        print(f"      当前阶段={task.current_stage} · 下一步={card['next_step'] or '待收口'}")
    return 0


def cmd_lessons(action: str, rest: list[str]) -> int:
    book = LessonBook(_data_dir() / "lessons.jsonl")
    if action == "search":
        query = " ".join(rest)
        for lesson in book.search(query):
            print(f"  {lesson.title} · 根因={lesson.root_cause} · 修复={lesson.fix}")
        return 0
    if action == "add":
        values = _parse_kv(rest)
        required = ("title", "symptom", "root_cause", "fix")
        missing = [key for key in required if not values.get(key)]
        if missing:
            print(f"usage: lessons add title=... symptom=... root_cause=... fix=... [keywords=a,b]")
            return 2
        lesson = Lesson(
            title=values["title"],
            symptom=values["symptom"],
            root_cause=values["root_cause"],
            fix=values["fix"],
            keywords=_split(values.get("keywords", "")),
        )
        book.add(lesson)
        print(render("wrapup", f"错题本已写入: {lesson.title}"))
        return 0
    print("usage: lessons search <query> | lessons add key=value ...")
    return 2


def cmd_round(action: str, task_id: str, text: str = "") -> int:
    runtime_dir = _data_dir() / "runtime" / task_id
    ledger = Ledger.load(runtime_dir)
    if action == "start":
        round_ = ledger.log_round_start(text or "继续任务")
        ledger.flush()
        print(json.dumps({"task_id": task_id, "round": round_.index}, ensure_ascii=False))
        return 0
    if action == "end":
        status = RoundStatus(text or "success")
        round_ = ledger.log_round_end(status)
        ledger.flush()
        print(json.dumps({"task_id": task_id, "round": round_.index,
                          "status": round_.status.value}, ensure_ascii=False))
        return 0
    print("usage: round start <task_id> [goal] | round end <task_id> [success|failed]")
    return 2


def cmd_dashboard(task_id: str) -> int:
    runtime = _runtime(False, {})
    path = runtime.write_dashboard(task_id)
    print(render("wrapup", f"看板已生成: {path}"))
    return 0


def cmd_task(action: str, rest: list[str]) -> int:
    store = TaskStore(_data_dir())
    if action == "list":
        return cmd_tasks("--all" in rest)
    if not rest:
        print("usage: task show|step|complete <task_id> [...]")
        return 2
    task = store.load(rest[0])
    if action == "show":
        print(json.dumps(store.resume_card(task), ensure_ascii=False, indent=2))
        return 0
    if action == "step":
        values = _parse_kv(rest[1:])
        index = int(values.get("index", "0"))
        if index < 1 or index > len(task.steps):
            print(f"step index out of range: 1..{len(task.steps)}")
            return 2
        step = task.steps[index - 1]
        step.status = StepStatus(values.get("status", "done"))
        step.summary = values.get("summary", step.summary)
        task.current_stage = values.get("stage", task.current_stage)
        store.save(task)
        print(render("plan", f"task={task.id} · step={index} -> {step.status.value}",
                     result=step.summary))
        return 0
    if action == "complete":
        runtime = _runtime(False, {})
        try:
            runtime.complete(task)
        except ValueError as error:
            print(render("blocked", str(error)))
            return 1
        print(render("wrapup", f"task={task.id} 已完成"))
        return 0
    print("usage: task list|show|step|complete ...")
    return 2


def cmd_case(action: str, task_id: str) -> int:
    path = _data_dir() / "runtime" / task_id / "case.json"
    case = Case.load(path)
    if action == "show":
        print(json.dumps(case.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if action == "tick":
        spec = case.tick()
        print(json.dumps(spec.__dict__ if spec else {"next": "resolve_or_fill_gates"},
                         ensure_ascii=False, indent=2))
        return 0
    if action == "resolve":
        ok, message = case.try_resolve()
        case.save(path)
        print(render("verify" if ok else "blocked", message))
        return 0 if ok else 1
    print("usage: case show|tick|resolve <task_id>")
    return 2


def cmd_accept(task_id: str) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    criteria = task.acceptance or ["任务目标有 observed 证据"]
    result = IndependentReviewer().review(AcceptancePackage(
        case_id=task.id,
        goal=task.goal,
        criteria=criteria,
        evidence=runtime.evidence(task.id),
    ))
    path = runtime._task_dir(task.id) / "acceptance.json"
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(render("verify" if result.verdict == Verdict.PASS else "blocked",
                 f"独立验收={result.verdict.value}", result="；".join(result.reasons)))
    return 0 if result.verdict == Verdict.PASS else 1


def cmd_wrapup(task_id: str) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    if any(step.status != StepStatus.DONE for step in task.steps):
        print(render("blocked", "仍有未完成步骤，禁止收口"))
        return 1
    if task.acceptance and cmd_accept(task_id) != 0:
        return 1
    runtime.complete(task)
    dashboard = runtime.write_dashboard(task_id)
    print(render("wrapup", f"task={task.id} 已收口", result=f"看板={dashboard}"))
    return 0


def _registry() -> FlowRegistry:
    reg = FlowRegistry()
    reg.load_json(FLOWS_JSON)
    from kernel import load_installed_extensions
    extensions_dir = os.environ.get(
        "ILOOP_EXTENSIONS_DIR",
        str(Path.home() / ".iloop" / "extensions"),
    )
    _, issues = load_installed_extensions(reg, extensions_dir)
    for issue in issues:
        print(f"  ⚠️ 扩展未加载: {issue.message}", file=sys.stderr)
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
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    real = "--real" in rest
    if cmd == "plan":
        return cmd_plan(" ".join(t for t in rest if not t.startswith("--")))
    if cmd == "run":
        text = " ".join(t for t in rest if not t.startswith("--") and "=" not in t)
        if not text:
            print("usage: run '<task>' [--real] [caps=build,run,logs] [k=v ...]")
            return 2
        return cmd_run(text, real, _parse_kv(rest))
    if cmd == "resume":
        pos = [t for t in rest if not t.startswith("--") and "=" not in t]
        if not pos:
            print("usage: resume <task_id> [--real] [caps=...] [k=v ...]")
            return 2
        return cmd_resume(pos[0], real, _parse_kv(rest))
    if cmd == "tasks":
        return cmd_tasks("--all" in rest)
    if cmd == "task":
        if not rest:
            print("usage: task list|show|step|complete ...")
            return 2
        return cmd_task(rest[0], rest[1:])
    if cmd == "case":
        if len(rest) < 2:
            print("usage: case show|tick|resolve <task_id>")
            return 2
        return cmd_case(rest[0], rest[1])
    if cmd == "accept":
        if not rest:
            print("usage: accept <task_id>")
            return 2
        return cmd_accept(rest[0])
    if cmd == "wrapup":
        if not rest:
            print("usage: wrapup <task_id>")
            return 2
        return cmd_wrapup(rest[0])
    if cmd == "lessons":
        if not rest:
            print("usage: lessons search <query> | lessons add key=value ...")
            return 2
        return cmd_lessons(rest[0], rest[1:])
    if cmd == "round":
        if len(rest) < 2:
            print("usage: round start|end <task_id> [goal|status]")
            return 2
        return cmd_round(rest[0], rest[1], " ".join(rest[2:]))
    if cmd == "dashboard":
        if not rest:
            print("usage: dashboard <task_id>")
            return 2
        return cmd_dashboard(rest[0])
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
