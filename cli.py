#!/usr/bin/env python3
"""iLoop CLI —— 内核的命令入口（薄封装，把四协议 + 插件跑起来）。

用法:
  python3 -m cli plan "<任务>"              # flow 路由 + 自治分级
  python3 -m cli run "<任务>" [caps=build,run,logs] [k=v ...]
  python3 -m cli resume <task_id> [caps=...] [k=v ...]
  python3 -m cli tasks [--all]              # 可恢复任务与下一步
  python3 -m cli task show|step|complete ...
  python3 -m cli case show|tick|evidence|gate|resolve <task_id>
  python3 -m cli next <task_id>
  python3 -m cli capability require|complete|cancel|status <task_id>
  python3 -m cli global-review prepare|show|record <task_id>
  python3 -m cli accept prepare|record|status <task_id>
  python3 -m cli wrapup <task_id>
  python3 -m cli round start|end <task_id> [...]
  python3 -m cli lessons search|add ...
  python3 -m cli context manifest
  python3 -m cli constitution list|add ...
  python3 -m cli blocker <task_id> reason=... evidence=... options=... recommendation=...
  python3 -m cli ui-flow new|list|show|verify|to-task ...
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
    Runtime, TaskStore, TaskStatus, TaskStep, StepStatus, Lesson, LessonBook, Ledger, RoundStatus,
    AcceptancePackage, AcceptanceStore, Verdict, EvidenceKind, CapabilityGate,
    GlobalReview, ProjectMemory, UIFlowStore, ACTION_CAPABILITY,
    load_installed_plugins,
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
    platform = kwargs.get("platform", "ios_native")
    if platform != "ios_native":
        extensions_dir = os.environ.get(
            "ILOOP_EXTENSIONS_DIR", str(Path.home() / ".iloop" / "extensions")
        )
        candidates = load_installed_plugins(extensions_dir, kwargs)
        plugin = next((candidate for candidate in candidates
                       if candidate.platform_id == platform), None)
        if plugin is None:
            raise ValueError(f"extension platform plugin not found: {platform}")
    return Runtime(data_dir, _registry(), plugin, project_root=project_root)


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
        executor_id=kwargs.pop("executor_id", ""),
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
    try:
        TaskStore.validate_id(task_id)
    except ValueError as error:
        print(render("blocked", str(error)))
        return 1
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
    runtime = _runtime(False, {})
    store = runtime.tasks
    if action == "list":
        return cmd_tasks("--all" in rest)
    if not rest:
        print("usage: task show|step|complete <task_id> [...]")
        return 2
    task = runtime.load(rest[0])
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
        requested = StepStatus(values.get("status", "done"))
        evidence_ids = _split(values.get("evidence", ""))
        human_confirmed = values.get("human_confirmed", "").lower() in ("1", "true", "yes")
        known_evidence = {item.id for item in runtime.evidence(task.id)}
        unknown = [item for item in evidence_ids if item not in known_evidence]
        if unknown:
            print(render("blocked", f"步骤引用未知 evidence ids: {unknown}"))
            return 1
        if requested == StepStatus.DONE and not evidence_ids and not human_confirmed:
            print(render("blocked", "步骤完成需要 evidence=<id> 或 human_confirmed=true"))
            return 1
        if human_confirmed:
            print(render(
                "blocked",
                "CLI 不可信，不能写入用户确认；由宿主通过 Runtime attestation_verifier API 注入",
            ))
            return 1
        step.status = requested
        step.summary = values.get("summary", step.summary)
        step.evidence_ids = evidence_ids or step.evidence_ids
        step.completion_source = "human_confirmed" if human_confirmed else (
            "machine" if step.evidence_ids else step.completion_source
        )
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
    try:
        TaskStore.validate_id(task_id)
    except ValueError as error:
        print(render("blocked", str(error)))
        return 1
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


def cmd_case_write(action: str, task_id: str, rest: list[str]) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    path = Path(task.case_path)
    case = Case.load(path)
    values = _parse_kv(rest)
    if action == "evidence":
        requested_kind = EvidenceKind(values.get("kind", "inferred"))
        required = () if requested_kind == EvidenceKind.OBSERVED else (
            "capability", "source", "summary"
        )
        missing = [key for key in required if not values.get(key)]
        if missing:
            print("usage: case evidence <task_id> capability=... source=... summary=... "
                  "[kind=observed|inferred] [outcome=success|failure|neutral] "
                  "[path=...] [hypothesis=h1] [gate=time|scope|mechanism|counter_evidence]")
            return 2
        kind = requested_kind
        evidence_path = values.get("path") or None
        attestation_metadata = {}
        if kind == EvidenceKind.OBSERVED:
            print(render(
                "blocked",
                "CLI 不可信，不能手工写 observed evidence；请执行受信 Capability",
            ))
            return 1
        else:
            capability = values["capability"]
            source = values["source"]
            summary = values["summary"]
            outcome = values.get("outcome", "neutral")
        evidence = EvidenceArtifact(
            capability=capability,
            source=source,
            kind=kind,
            summary=summary,
            path=evidence_path,
            outcome=outcome,
            metadata=attestation_metadata,
        )
        hypothesis = values.get("hypothesis", "h1")
        case.attach(
            hypothesis,
            evidence,
            refutes=values.get("refutes", "").lower() in ("1", "true", "yes"),
        )
        gate = values.get("gate", "")
        if gate:
            if kind != EvidenceKind.OBSERVED or gate not in evidence.metadata.get("gates", []):
                print(render("blocked", f"evidence attestation does not claim gate={gate}"))
                return 1
            case.bind_gate(gate, evidence)
        case.save(path)
        runtime._append_evidence(task.id, evidence)
        task.evidence_ids.append(evidence.id)
        runtime.tasks.save(task)
        print(json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if action == "gate":
        gate = values.get("gate", "")
        evidence_id = values.get("evidence", "")
        evidence = case.evidence.get(evidence_id)
        if not gate or evidence is None:
            print("usage: case gate <task_id> gate=<name> evidence=<evidence-id>")
            return 2
        if gate not in evidence.metadata.get("gates", []):
            print(render("blocked", f"evidence does not attest gate={gate}"))
            return 1
        already_bound = {
            bound_id
            for bound_gate, ids in case.gate_bindings().items()
            if bound_gate != gate
            for bound_id in ids
        }
        if evidence_id in already_bound and len(evidence.metadata.get("gates", [])) < 2:
            print(render("blocked", "single-gate evidence cannot be reused for another gate"))
            return 1
        case.bind_gate(gate, evidence)
        case.save(path)
        print(render("verify", f"{gate} 已绑定证据 {evidence_id}"))
        return 0
    return 2


def cmd_next(task_id: str) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    print(json.dumps(runtime.next_action(task), ensure_ascii=False, indent=2))
    return 0


def cmd_capability_gate(action: str, task_id: str, rest: list[str]) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    gate = CapabilityGate.load(task.capability_gate_path)
    values = _parse_kv(rest)
    if action == "require":
        op_id = values.get("id", "")
        reason = values.get("reason", "")
        if not op_id or not reason:
            print("usage: capability require <task_id> id=<operation-id> reason=<why>")
            return 2
        runtime.require_operation(task, op_id, reason)
        gate = CapabilityGate.load(task.capability_gate_path)
    elif action == "complete":
        print(render(
            "blocked",
            "CLI 不可信，不能完成平台 Gate；由受信宿主 Adapter 通过 API 回写",
        ))
        return 1
    elif action == "cancel":
        print(render(
            "blocked",
            "CLI 不可信，不能取消平台 Gate；由受信宿主根据用户确认通过 API 回写",
        ))
        return 1
    elif action != "status":
        print("usage: capability require|complete|cancel|status <task_id> ...")
        return 2
    gate.save(task.capability_gate_path)
    print(json.dumps(gate.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_global_review(action: str, task_id: str, rest: list[str]) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    values = _parse_kv(rest)
    if action == "prepare":
        project_root = values.get("project_root") or os.environ.get("ILOOP_PROJECT_ROOT")
        if not project_root:
            print("global-review prepare requires project_root=... or ILOOP_PROJECT_ROOT")
            return 2
        review = runtime.prepare_global_review(
            task,
            project_root,
            base=values.get("base", ""),
        )
    else:
        if not Path(task.global_review_path).exists():
            print(render("blocked", "global review has not been prepared"))
            return 1
        review = GlobalReview.load(task.global_review_path)
        if action == "record":
            evidence_ids = _split(values.get("evidence", ""))
            evidence_by_id = {item.id: item for item in runtime.evidence(task.id)}
            unknown = [item for item in evidence_ids if item not in evidence_by_id]
            if unknown:
                print(render("blocked", f"全局复核引用未知 evidence ids: {unknown}"))
                return 1
            invalid = [
                item for item in evidence_ids
                if not runtime._supports_success(evidence_by_id[item], task)
            ]
            if invalid:
                print(render("blocked", f"全局复核只接受成功 observed evidence: {invalid}"))
                return 1
            stale = [
                item for item in evidence_ids
                if evidence_by_id[item].created_at < review.created_at
            ]
            if stale:
                print(render(
                    "blocked",
                    f"全局复核证据必须生成于本次 review 之后: {stale}",
                ))
                return 1
            unrelated = [
                item for item in evidence_ids
                if values.get("target", "")
                not in evidence_by_id[item].metadata.get("subjects", [])
            ]
            if unrelated and values.get("accepted", "").lower() not in ("1", "true", "yes"):
                print(render(
                    "blocked",
                    f"全局复核证据未声明覆盖 target={values.get('target', '')}: {unrelated}",
                ))
                return 1
            user_confirmation = values.get("user_confirmation", "")
            if user_confirmation:
                confirmation = evidence_by_id.get(user_confirmation)
                if (
                    confirmation is None
                    or confirmation.metadata.get("human_confirmed") is not True
                    or not runtime._supports_success(confirmation, task)
                    or values.get("target", "")
                    not in confirmation.metadata.get("subjects", [])
                ):
                    print(render("blocked", "风险接受需要真实 human confirmation evidence"))
                    return 1
            try:
                review.verify(
                    values.get("target", ""),
                    evidence_ids,
                    accepted=values.get("accepted", "").lower() in ("1", "true", "yes"),
                    resolution=values.get("reason", ""),
                    user_confirmation_id=user_confirmation,
                )
            except (KeyError, ValueError) as error:
                print(render("blocked", str(error)))
                return 1
            review.save(task.global_review_path)
            task.global_review_status = review.status
            runtime.tasks.save(task)
        elif action != "show":
            print("usage: global-review prepare|show|record <task_id> ...")
            return 2
    print(json.dumps(review.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_accept(action: str, task_id: str, rest: list[str]) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    store = AcceptanceStore(task.acceptance_path)
    values = _parse_kv(rest)
    if action == "prepare":
        fingerprint = ""
        if task.global_review_path and Path(task.global_review_path).exists():
            fingerprint = GlobalReview.load(task.global_review_path).fingerprint
        package = AcceptancePackage(
            case_id=task.id,
            goal=task.goal,
            criteria=task.acceptance or ["目标与完整 diff 一致", "关键路径有成功 observed 证据"],
            evidence=runtime.evidence(task.id),
            subject_fingerprint=fingerprint,
            executor_id=task.executor_id,
        )
        store.prepare(package)
        task.independent_acceptance_required = True
        task.acceptance_status = "prepared"
        runtime.tasks.save(task)
        print(json.dumps(package.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if action == "record":
        print(render(
            "blocked",
            "CLI 不可信，不能回写独立验收；由宿主通过 AcceptanceStore.record_file(verifier) API 写入",
        ))
        return 1
    if action == "status":
        print(json.dumps(store.load_raw(), ensure_ascii=False, indent=2))
        # CLI has no trusted host attestation verifier, so status is informational.
        return 1
    print("usage: accept prepare|record|status <task_id> [result=/path/to/reviewer-result.json]")
    return 2


def cmd_wrapup(task_id: str) -> int:
    runtime = _runtime(False, {})
    task = runtime.load(task_id)
    try:
        runtime.complete(task)
    except ValueError as error:
        print(render("blocked", str(error)))
        return 1
    dashboard = runtime.write_dashboard(task_id)
    record_dir = runtime.data_dir / "records"
    record_dir.mkdir(parents=True, exist_ok=True)
    record = record_dir / f"{task.id}.json"
    record.write_text(json.dumps({
        "task": runtime.load(task.id).to_dict(),
        "case": Case.load(task.case_path).to_dict(),
        "evidence": [item.to_dict() for item in runtime.evidence(task.id)],
        "dashboard": dashboard,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render("wrapup", f"task={task.id} 已收口", result=f"看板={dashboard}"))
    return 0


def cmd_context(action: str) -> int:
    memory = ProjectMemory(_data_dir())
    if action != "manifest":
        print("usage: context manifest")
        return 2
    print(json.dumps(memory.context_manifest(), ensure_ascii=False, indent=2))
    return 0


def cmd_constitution(action: str, rest: list[str]) -> int:
    project_root = os.environ.get("ILOOP_PROJECT_ROOT") or str(Path.cwd())
    memory = ProjectMemory(_data_dir(project_root), project_root=project_root)
    if action == "list":
        print(json.dumps(memory.constitution(), ensure_ascii=False, indent=2))
        return 0
    if action == "add":
        values = _parse_kv(rest)
        try:
            row = memory.add_constitution(
                values.get("rule", ""),
                source=values.get("source", ""),
                evidence_path=values.get("evidence_path", ""),
            )
        except ValueError as error:
            print(render("blocked", str(error)))
            return 1
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    print(
        "usage: constitution list | constitution add rule=... "
        "source=project-file evidence_path=..."
    )
    return 2


def cmd_blocker(task_id: str, rest: list[str]) -> int:
    values = _parse_kv(rest)
    try:
        path = ProjectMemory(_data_dir()).emit_blocker(
            task_id,
            reason=values.get("reason", ""),
            evidence=_split(values.get("evidence", "")),
            options=_split(values.get("options", "")),
            recommendation=values.get("recommendation", ""),
        )
    except ValueError as error:
        print(render("blocked", str(error)))
        return 1
    print(render("blocked", f"结构化 blocker 已落盘: {path}"))
    return 0


def cmd_ui_flow(action: str, rest: list[str]) -> int:
    store = UIFlowStore(_data_dir())
    if action == "new":
        values = _parse_kv(rest)
        name = values.get("name", "")
        goal = values.get("goal", "")
        if not name or not goal:
            print("usage: ui-flow new name=... goal=... [device=auto]")
            return 2
        flow = store.create(name, goal, values.get("device", "auto"))
        print(json.dumps(flow.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if action == "list":
        print(json.dumps([flow.to_dict() for flow in store.list()], ensure_ascii=False, indent=2))
        return 0
    if not rest:
        print("usage: ui-flow show|verify|to-task <flow-id>")
        return 2
    flow_id = rest[0]
    if action == "show":
        print(json.dumps(store.load(flow_id).to_dict(), ensure_ascii=False, indent=2))
        return 0
    if action == "verify":
        values = _parse_kv(rest[1:])
        flow = store.load(flow_id)
        flow.task_id = values.get("task_id", flow.task_id)
        flow.verification_run_id = values.get("flow_run_id", flow.verification_run_id)
        flow.device_id = values.get("device_id", flow.device_id)
        store.save(flow)
        evidence_by_id = {}
        for path in _data_dir().glob("runtime/*/evidence.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    evidence_by_id[row["id"]] = EvidenceArtifact(**row)
        try:
            flow = store.verify(flow_id, evidence_by_id)
        except ValueError as error:
            print(render("blocked", str(error)))
            return 1
        print(render("verify", f"UI Flow {flow.id} 已由节点证据验证"))
        return 0
    if action == "to-task":
        flow = store.load(flow_id)
        runtime = _runtime(False, {})
        task = runtime.start(
            f"验证 UI 路径 {flow.name}",
            acceptance=[flow.goal],
        )
        task.steps = [
            TaskStep(
                title=f"{node.action}: {node.target}",
                capability=ACTION_CAPABILITY[node.action],
                summary=f"ui-flow={flow.id} node={node.id}",
            )
            for node in flow.nodes
        ]
        runtime.tasks.save(task)
        _print_resume_card(runtime, task)
        return 0
    print("usage: ui-flow new|list|show|verify|to-task ...")
    return 2


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
    decision = _registry().plan_details(task)
    flow = decision["flow"]
    if not flow:
        print(render("plan", f"命中flow=none · 任务=<{task}> · 请澄清方向"))
        return 0
    print(render("plan", f"命中flow={flow.flow_id}（{flow.name}）· 自治档={flow.autonomy.value}",
                 basis=", ".join(flow.when_keywords), result=flow.evidence_strategy))
    print(f"  required_docs={flow.required_docs} · 升级条件={flow.escalate_when}")
    active_gates = [
        name for name in ("complexity_gate", "lessons_gate",
                          "global_review_gate", "acceptance_gate")
        if decision[name]
    ]
    print(f"  active_gates={active_gates}")
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
    plugin = IOSNativePlugin(
        mode="real" if real else "simulator",
        data_dir=str(_data_dir() / "platform"),
    )
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
    project_root = kwargs.get("project_root", "")
    plugin = IOSNativePlugin(
        mode="real" if real else "simulator",
        data_dir=str(_data_dir(project_root) / "platform"),
        config=kwargs,
    )
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
        attested_receipts = set()

        def demo_host_verifier(kind, path, row):
            return (
                kind == "evidence"
                and hashlib.sha256(path.read_bytes()).hexdigest()
                in attested_receipts
            )

        data_dir = _data_dir()
        runtime = Runtime(
            data_dir,
            _registry(),
            IOSNativePlugin(
                mode="simulator",
                data_dir=str(data_dir / "platform"),
            ),
            attestation_verifier=demo_host_verifier,
        )
        task = runtime.start(f"oncall 诊断：{ev.title}")
        case = Case.load(task.case_path)
        hypothesis = case.hypotheses["h1"]
        hypothesis.text = "客户端代码路径触发崩溃"
        for gate, capability, summary in (
            ("mechanism", "crash", "堆栈指向订单详情控制器"),
            ("time", "logs", "告警窗口内崩溃聚类上涨"),
            ("scope", "logs", "影响范围仅 iOS 6.3.0 订单详情"),
            ("counter_evidence", "logs", "其他版本同入口无同类堆栈"),
        ):
            artifact = runtime._task_dir(task.id) / f"oncall-{gate}.log"
            artifact.write_text(summary, encoding="utf-8")
            evidence = EvidenceArtifact(
                capability=capability,
                source="oncall.demo",
                kind="observed",
                outcome="success",
                summary=summary,
                path=str(artifact),
                metadata={
                    "task_id": task.id,
                    "run_id": f"{task.id}:demo:{gate}",
                    "flow_id": task.flow_id,
                    "flow_run_id": "",
                    "device": "ios-device",
                    "device_id": "demo-device",
                    "subjects": ["订单详情崩溃链路"],
                    "gates": [gate],
                    "trusted_producer": True,
                },
            )
            runtime._seal_trusted_evidence(task, evidence)
            receipt_path = Path(evidence.metadata["producer_receipt_path"])
            attested_receipts.add(hashlib.sha256(receipt_path.read_bytes()).hexdigest())
            runtime._append_evidence(task.id, evidence)
            task.evidence_ids.append(evidence.id)
            case.attach(hypothesis.id, evidence, gate=gate)
        ok, msg = case.try_resolve(
            lambda path, row: demo_host_verifier("evidence", path, row),
            expected_bindings={"task_id": task.id, "flow_id": task.flow_id}
        )
        case.save(task.case_path)
        runtime.tasks.save(task)
        print(render("verify" if ok else "blocked", msg))
        notifier.send(f"[iLoop] {ev.title} 诊断结论", msg)
    return 0 if ok else 1


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
            print("usage: case show|tick|evidence|gate|resolve <task_id>")
            return 2
        if rest[0] in ("evidence", "gate"):
            return cmd_case_write(rest[0], rest[1], rest[2:])
        return cmd_case(rest[0], rest[1])
    if cmd == "next":
        if not rest:
            print("usage: next <task_id>")
            return 2
        return cmd_next(rest[0])
    if cmd == "capability":
        if len(rest) < 2:
            print("usage: capability require|complete|cancel|status <task_id> ...")
            return 2
        return cmd_capability_gate(rest[0], rest[1], rest[2:])
    if cmd == "global-review":
        if len(rest) < 2:
            print("usage: global-review prepare|show|record <task_id> ...")
            return 2
        return cmd_global_review(rest[0], rest[1], rest[2:])
    if cmd == "accept":
        if len(rest) < 2:
            print("usage: accept prepare|record|status <task_id> ...")
            return 2
        return cmd_accept(rest[0], rest[1], rest[2:])
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
    if cmd == "context":
        return cmd_context(rest[0] if rest else "")
    if cmd == "constitution":
        if not rest:
            print("usage: constitution list|add ...")
            return 2
        return cmd_constitution(rest[0], rest[1:])
    if cmd == "blocker":
        if not rest:
            print("usage: blocker <task_id> reason=... evidence=... options=... recommendation=...")
            return 2
        return cmd_blocker(rest[0], rest[1:])
    if cmd == "ui-flow":
        if not rest:
            print("usage: ui-flow new|list|show|verify|to-task ...")
            return 2
        return cmd_ui_flow(rest[0], rest[1:])
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
