"""扩展机制 —— 二次开发的唯一入口，核心只读硬边界。

任何业务二开只改扩展目录（~/.iloop/extensions/<team.ext>/），核心整体只读。
extension_validate 检查：越界改核心、flow 命名空间冲突、manifest 合法性。

对应内部版 EXTENDING.md + extension_manager.py 的开源精简实现。
"""

from __future__ import annotations

import json
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .flow import Flow, FlowRegistry
from .capability import Plugin

MANIFEST_NAME = "manifest.json"
FLOWS_NAME = "flows.json"


@dataclass
class ValidationIssue:
    level: str   # error | warning
    message: str


@dataclass
class Extension:
    name: str            # 命名空间，如 "team.oncall"
    root: Path
    manifest: dict = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return self.name


def scaffold_extension(name: str, base_dir: str | Path) -> Extension:
    """创建一个扩展包骨架。name 必须是 <team>.<ext> 形式做命名空间。"""
    if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", name):
        raise ValueError(f"扩展名必须是 '<team>.<ext>' 形式（小写/数字/下划线），得到: {name}")
    root = Path(base_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "0.1.1",
        "iloop_kernel": ">=0.1.1",
        "description": f"{name} iLoop extension",
        "provides": {"flows": FLOWS_NAME, "plugin": None},
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # flow 必须带命名空间前缀，防覆盖核心
    (root / FLOWS_NAME).write_text(json.dumps([{
        "flow_id": f"{name}.example",
        "name": f"{name} 示例 flow",
        "autonomy": "L1",
        "when_keywords": ["示例"],
        "priority": 10,
        "guidance": "在此实现你的业务 flow；flow_id 必须以扩展名为前缀。",
        "required_docs": [],
        "evidence_strategy": "按你的领域定",
        "escalate_when": "命中你定义的卡口"
    }], ensure_ascii=False, indent=2), encoding="utf-8")
    return Extension(name=name, root=root, manifest=manifest)


def load_extension(root: str | Path) -> Extension:
    root = Path(root)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    return Extension(name=manifest["name"], root=root, manifest=manifest)


def validate_extension(ext: Extension, *, core_flow_ids: Optional[set[str]] = None) -> List[ValidationIssue]:
    """校验扩展包：命名空间、flow 前缀、不与核心 flow_id 冲突。"""
    issues: List[ValidationIssue] = []
    core_flow_ids = core_flow_ids or set()

    if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", ext.name):
        issues.append(ValidationIssue("error", f"扩展名非法: {ext.name}"))

    flows_file = ext.root / FLOWS_NAME
    if flows_file.exists():
        try:
            flows = json.loads(flows_file.read_text(encoding="utf-8"))
        except Exception as e:
            issues.append(ValidationIssue("error", f"flows.json 解析失败: {e}"))
            flows = []
        for f in flows:
            fid = f.get("flow_id", "")
            if not fid.startswith(ext.name + "."):
                issues.append(ValidationIssue("error",
                    f"flow_id '{fid}' 必须以扩展名 '{ext.name}.' 为前缀（防覆盖核心）"))
            if fid in core_flow_ids:
                issues.append(ValidationIssue("error", f"flow_id '{fid}' 与核心 flow 冲突"))
    plugin_path = ext.manifest.get("provides", {}).get("plugin")
    if plugin_path:
        candidate = (ext.root / plugin_path).resolve()
        if ext.root.resolve() not in candidate.parents:
            issues.append(ValidationIssue("error", "plugin path escapes extension root"))
        elif not candidate.is_file() or candidate.suffix != ".py":
            issues.append(ValidationIssue("error", f"plugin file missing or not Python: {plugin_path}"))
    return issues


def merge_into_registry(reg: FlowRegistry, ext: Extension) -> int:
    """把扩展的 flow 合并进注册表。只增不覆盖核心（register 会拒绝重复 id）。"""
    flows_file = ext.root / FLOWS_NAME
    if not flows_file.exists():
        return 0
    return reg.load_json(flows_file)


def load_installed_extensions(reg: FlowRegistry, base_dir: str | Path) -> tuple[int, List[ValidationIssue]]:
    """扫描扩展目录，把校验通过的扩展 flow 合并进注册表。

    plan/flows 调用它后，业务扩展无需修改核心注册表即可自动生效。
    非法扩展不加载，并返回问题供 CLI 外显。
    """
    base = Path(base_dir).expanduser()
    if not base.exists():
        return 0, []

    core_ids = {f.flow_id for f in reg.all()}
    loaded = 0
    issues: List[ValidationIssue] = []
    for root in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = root / MANIFEST_NAME
        if not manifest.exists():
            continue
        try:
            ext = load_extension(root)
        except Exception as exc:
            issues.append(ValidationIssue("error", f"{root.name}: manifest 加载失败: {exc}"))
            continue

        ext_issues = validate_extension(ext, core_flow_ids=core_ids)
        if has_errors(ext_issues):
            issues.extend(
                ValidationIssue(i.level, f"{ext.name}: {i.message}") for i in ext_issues
            )
            continue
        try:
            loaded += merge_into_registry(reg, ext)
            core_ids.update(f.flow_id for f in reg.all())
        except Exception as exc:
            issues.append(ValidationIssue("error", f"{ext.name}: flow 合并失败: {exc}"))
    return loaded, issues


def has_errors(issues: List[ValidationIssue]) -> bool:
    return any(i.level == "error" for i in issues)


def load_extension_plugin(ext: Extension, config: Optional[dict] = None) -> Optional[Plugin]:
    """Load an explicitly declared plugin factory from a validated extension."""
    relative = ext.manifest.get("provides", {}).get("plugin")
    if not relative:
        return None
    issues = validate_extension(ext)
    if has_errors(issues):
        raise ValueError("; ".join(issue.message for issue in issues))
    path = (ext.root / relative).resolve()
    spec = importlib.util.spec_from_file_location(
        f"iloop_extension_{ext.name.replace('.', '_')}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load extension plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "create_plugin", None)
    if not callable(factory):
        raise TypeError(f"{path} must export create_plugin(config)")
    plugin = factory(config or {})
    if not isinstance(plugin, Plugin):
        raise TypeError(f"{path} create_plugin did not return a Plugin")
    return plugin


def load_installed_plugins(base_dir: str | Path, config: Optional[dict] = None) -> List[Plugin]:
    base = Path(base_dir).expanduser()
    if not base.exists():
        return []
    plugins = []
    for root in sorted(path for path in base.iterdir() if path.is_dir()):
        if not (root / MANIFEST_NAME).exists():
            continue
        try:
            plugin = load_extension_plugin(load_extension(root), config)
        except Exception:
            continue
        if plugin is not None:
            plugins.append(plugin)
    return plugins
