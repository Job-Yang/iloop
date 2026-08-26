"""扩展机制 —— 二次开发的唯一入口，核心只读硬边界。

任何业务二开只改扩展目录（~/.iloop/extensions/<team.ext>/），核心整体只读。
extension_validate 检查：越界改核心、flow 命名空间冲突、manifest 合法性。

对应内部版 EXTENDING.md + extension_manager.py 的开源精简实现。
"""

from __future__ import annotations

import json
import importlib.util
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .flow import Flow, FlowRegistry
from .capability import (
    CapabilityId, CapabilitySpec, Plugin, capability_id,
)
from .action import (
    ActionCatalog, ActionRisk, ActionSideEffect, ActionSpec,
)
from .recipe import AssistantRecipe, RecipeCatalog

MANIFEST_NAME = "manifest.json"
FLOWS_NAME = "flows.json"
ACTIONS_NAME = "actions.json"
RECIPES_NAME = "recipes.json"
CAPABILITIES_NAME = "capabilities.json"


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
        "version": "0.4.0",
        "iloop_kernel": ">=0.4.0",
        "description": f"{name} iLoop extension",
        "provides": {
            "flows": FLOWS_NAME,
            "capabilities": CAPABILITIES_NAME,
            "actions": ACTIONS_NAME,
            "recipes": RECIPES_NAME,
            "plugin": None,
            "application": None,
            "provider_bindings": {},
        },
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
    (root / ACTIONS_NAME).write_text("[]\n", encoding="utf-8")
    (root / RECIPES_NAME).write_text("[]\n", encoding="utf-8")
    (root / CAPABILITIES_NAME).write_text("[]\n", encoding="utf-8")
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

    provides = ext.manifest.get("provides", {})
    provides_valid = isinstance(provides, dict)
    if not provides_valid:
        issues.append(ValidationIssue("error", "manifest.provides must be an object"))
        provides = {}
    requirement = str(ext.manifest.get("iloop_kernel", "")).strip()
    if requirement:
        from . import __version__
        match = re.fullmatch(r">=\s*(\d+)\.(\d+)\.(\d+)", requirement)
        current = tuple(int(part) for part in __version__.split(".")[:3])
        if not match:
            issues.append(ValidationIssue(
                "error", f"unsupported iloop_kernel requirement: {requirement}"
            ))
        elif current < tuple(int(part) for part in match.groups()):
            issues.append(ValidationIssue(
                "error",
                f"extension requires iLoop {requirement}, current={__version__}",
            ))

    flows_file = ext.root / FLOWS_NAME
    if flows_file.exists():
        try:
            payload = json.loads(flows_file.read_text(encoding="utf-8"))
            flows = payload if isinstance(payload, list) else payload.get("flows", [])
            if not isinstance(flows, list):
                raise TypeError("flows must be an array")
        except Exception as e:
            issues.append(ValidationIssue("error", f"flows.json 解析失败: {e}"))
            flows = []
        seen = set()
        for f in flows:
            if not isinstance(f, dict):
                issues.append(ValidationIssue("error", "each flow must be an object"))
                continue
            fid = f.get("flow_id", "")
            if fid in seen:
                issues.append(ValidationIssue("error", f"duplicate flow_id '{fid}'"))
            seen.add(fid)
            if not fid.startswith(ext.name + "."):
                issues.append(ValidationIssue("error",
                    f"flow_id '{fid}' 必须以扩展名 '{ext.name}.' 为前缀（防覆盖核心）"))
            if fid in core_flow_ids:
                issues.append(ValidationIssue("error", f"flow_id '{fid}' 与核心 flow 冲突"))
            try:
                Flow(**f)
            except (TypeError, ValueError) as error:
                issues.append(ValidationIssue(
                    "error", f"flow '{fid}' schema invalid: {error}"
                ))
    for code_key in ("plugin", "application"):
        code_path = provides.get(code_key)
        if not code_path:
            continue
        candidate = (ext.root / code_path).resolve()
        if ext.root.resolve() not in candidate.parents:
            issues.append(ValidationIssue(
                "error", f"{code_key} path escapes extension root"
            ))
        elif not candidate.is_file() or candidate.suffix != ".py":
            issues.append(ValidationIssue(
                "error",
                f"{code_key} file missing or not Python: {code_path}",
            ))
    bindings = provides.get("provider_bindings", {})
    if not isinstance(bindings, dict):
        issues.append(ValidationIssue(
            "error", "manifest.provides.provider_bindings must be an object"
        ))
    else:
        for capability, platform_id in bindings.items():
            try:
                capability_id(capability)
            except ValueError:
                issues.append(ValidationIssue(
                    "error", f"unknown provider binding capability: {capability}"
                ))
            if not str(platform_id).strip():
                issues.append(ValidationIssue(
                    "error", f"provider binding '{capability}' has empty platform_id"
                ))
    for key in ("capabilities", "actions", "recipes"):
        relative = provides.get(key)
        if not relative:
            continue
        candidate = (ext.root / str(relative)).resolve()
        if ext.root.resolve() not in candidate.parents:
            issues.append(ValidationIssue("error", f"{key} path escapes extension root"))
        elif not candidate.is_file() or candidate.suffix != ".json":
            issues.append(ValidationIssue(
                "error", f"{key} file missing or not JSON: {relative}"
            ))
    paths_valid = not any(
        issue.level == "error"
        and ("path escapes" in issue.message or "file missing" in issue.message)
        for issue in issues
    )
    if paths_valid and provides_valid:
        try:
            _parse_capability_contribution(ext)
            _parse_application_contribution(ext)
        except (
            TypeError, ValueError, KeyError, OSError, json.JSONDecodeError,
        ) as error:
            issues.append(ValidationIssue(
                "error", f"application contribution invalid: {error}"
            ))
    return issues


def _json_array(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError(f"{path.name} must contain an array")
    return payload


def _parse_capability_contribution(
    ext: Extension,
) -> List[CapabilitySpec]:
    provides = ext.manifest.get("provides", {})
    rows = (
        _json_array(ext.root / provides["capabilities"])
        if provides.get("capabilities")
        else []
    )
    specs = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("each capability must be an object")
        spec = CapabilitySpec(
            capability_id=capability_id(row["capability_id"]),
            description=str(row["description"]),
            inputs=dict(row.get("inputs", {})),
            outputs=dict(row.get("outputs", {})),
            side_effect=str(row.get("side_effect", "none")),
            required_tools=tuple(row.get("required_tools", [])),
            supported_deployments=tuple(
                row.get("supported_deployments", [])
            ),
        )
        if not spec.capability_id.startswith(ext.name + "."):
            raise ValueError(
                f"capability_id '{spec.capability_id}' must start with "
                f"'{ext.name}.'"
            )
        if spec.capability_id in seen:
            raise ValueError(
                f"duplicate capability_id '{spec.capability_id}'"
            )
        seen.add(spec.capability_id)
        specs.append(spec)
    return specs


def _parse_application_contribution(
    ext: Extension,
) -> Tuple[List[ActionSpec], List[AssistantRecipe]]:
    provides = ext.manifest.get("provides", {})
    action_rows = (
        _json_array(ext.root / provides["actions"])
        if provides.get("actions")
        else []
    )
    recipe_rows = (
        _json_array(ext.root / provides["recipes"])
        if provides.get("recipes")
        else []
    )
    actions = []
    seen_actions = set()
    for row in action_rows:
        if not isinstance(row, dict):
            raise TypeError("each action must be an object")
        spec = ActionSpec(
            action_id=str(row["action_id"]),
            description=str(row["description"]),
            risk=ActionRisk(row.get("risk", "low")),
            side_effects=tuple(
                ActionSideEffect(item)
                for item in row.get("side_effects", ["none"])
            ),
            allowed_assistants=tuple(row.get("allowed_assistants", [])),
            inputs=dict(row.get("inputs", {})),
            outputs=dict(row.get("outputs", {})),
            required_capabilities=tuple(
                capability_id(item)
                for item in row.get("required_capabilities", [])
            ),
            disposition_kind=str(row.get("disposition_kind", "")),
            lifecycle_stage=str(row.get(
                "lifecycle_stage",
                "disposition" if row.get("disposition_kind") else "diagnosis",
            )),
        )
        if not spec.action_id.startswith(ext.name + "."):
            raise ValueError(
                f"action_id '{spec.action_id}' must start with '{ext.name}.'"
            )
        if spec.action_id in seen_actions:
            raise ValueError(f"duplicate action_id '{spec.action_id}'")
        seen_actions.add(spec.action_id)
        actions.append(spec)
    recipes = []
    seen_recipes = set()
    for row in recipe_rows:
        if not isinstance(row, dict):
            raise TypeError("each recipe must be an object")
        recipe = AssistantRecipe(
            assistant_id=str(row["assistant_id"]),
            actions=tuple(row["actions"]),
            version=str(row.get("version", "1")),
            ingress=tuple(row.get("ingress", [])),
            continuous_observation=bool(
                row.get("continuous_observation", False)
            ),
            action_risks={
                str(key): ActionRisk(value)
                for key, value in row.get("action_risks", {}).items()
            },
        )
        if not recipe.assistant_id.startswith(ext.name + "."):
            raise ValueError(
                f"assistant_id '{recipe.assistant_id}' must start with '{ext.name}.'"
            )
        if recipe.assistant_id in seen_recipes:
            raise ValueError(
                f"duplicate assistant_id '{recipe.assistant_id}'"
            )
        seen_recipes.add(recipe.assistant_id)
        recipes.append(recipe)
    return actions, recipes


def load_extension_application(
    ext: Extension,
    actions: ActionCatalog,
    recipes: RecipeCatalog,
    config: Optional[dict] = None,
) -> Tuple[int, int]:
    """Load namespaced action and recipe declarations after validation."""
    issues = validate_extension(ext)
    if has_errors(issues):
        raise ValueError("; ".join(issue.message for issue in issues))
    capability_specs = _parse_capability_contribution(ext)
    action_specs, assistant_recipes = _parse_application_contribution(ext)
    handlers = load_extension_action_handlers(ext, config)
    unknown_handlers = sorted(set(handlers) - {
        item.action_id for item in action_specs
    })
    if unknown_handlers:
        raise ValueError(
            "application declares handlers for unknown actions: "
            + ", ".join(unknown_handlers)
        )
    staged_capabilities = actions.capabilities.clone()
    for spec in capability_specs:
        staged_capabilities.register(spec)
    staged_actions = ActionCatalog(staged_capabilities)
    for spec in actions.all():
        staged_actions.register(spec)
    for spec in action_specs:
        staged_actions.register(spec, handlers.get(spec.action_id))
    staged_recipes = RecipeCatalog(staged_actions)
    for recipe in recipes.all():
        staged_recipes.register(recipe)
    for recipe in assistant_recipes:
        staged_recipes.register(recipe)
    for spec in capability_specs:
        actions.capabilities.register(spec)
    for spec in action_specs:
        actions.register(spec, handlers.get(spec.action_id))
    for recipe in assistant_recipes:
        recipes.register(recipe)
    return len(action_specs), len(assistant_recipes)


def _load_extension_module(ext: Extension, relative: str, role: str):
    path = (ext.root / relative).resolve()
    spec = importlib.util.spec_from_file_location(
        f"iloop_extension_{ext.name.replace('.', '_')}_{role}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load extension {role}: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_extension_action_handlers(
    ext: Extension,
    config: Optional[dict] = None,
) -> Dict[str, Callable]:
    relative = ext.manifest.get("provides", {}).get("application")
    if not relative:
        return {}
    module = _load_extension_module(ext, str(relative), "application")
    factory = getattr(module, "create_action_handlers", None)
    if not callable(factory):
        raise TypeError(
            f"{relative} must export create_action_handlers(config)"
        )
    handlers = factory(config or {})
    if not isinstance(handlers, Mapping):
        raise TypeError("create_action_handlers(config) must return a mapping")
    return {str(key): value for key, value in handlers.items()}


def extension_provider_bindings(ext: Extension) -> Dict[CapabilityId, str]:
    issues = validate_extension(ext)
    if has_errors(issues):
        raise ValueError("; ".join(issue.message for issue in issues))
    rows = ext.manifest.get("provides", {}).get("provider_bindings", {})
    return {
        capability_id(capability): str(platform_id)
        for capability, platform_id in rows.items()
    }


def load_installed_application(
    base_dir: str | Path,
    actions: ActionCatalog,
    recipes: RecipeCatalog,
    config: Optional[dict] = None,
) -> Tuple[Tuple[int, int], Dict[CapabilityId, str], List[ValidationIssue]]:
    """Load valid application declarations while isolating broken neighbors."""
    base = Path(base_dir).expanduser()
    if not base.exists():
        return (0, 0), {}, []
    valid = []
    issues: List[ValidationIssue] = []
    for root in sorted(path for path in base.iterdir() if path.is_dir()):
        if not (root / MANIFEST_NAME).is_file():
            continue
        try:
            ext = load_extension(root)
            ext_issues = validate_extension(ext)
        except (Exception, SystemExit) as error:
            issues.append(ValidationIssue(
                "error", f"{root.name}: application load failed: {error}"
            ))
            continue
        if has_errors(ext_issues):
            issues.extend(
                ValidationIssue(item.level, f"{ext.name}: {item.message}")
                for item in ext_issues
            )
            continue
        valid.append(ext)

    parsed = [
        (
            ext,
            _parse_capability_contribution(ext),
            *_parse_application_contribution(ext),
        )
        for ext in valid
    ]
    usable = []
    handlers_by_extension = {}
    for ext, capability_specs, action_specs, assistant_recipes in parsed:
        try:
            handlers = load_extension_action_handlers(ext, config)
        except (Exception, SystemExit) as error:
            issues.append(ValidationIssue(
                "error", f"{ext.name}: action handler load failed: {error}"
            ))
            continue
        unknown = sorted(
            set(handlers) - {item.action_id for item in action_specs}
        )
        if unknown:
            issues.append(ValidationIssue(
                "error",
                f"{ext.name}: handlers for unknown actions: "
                f"{', '.join(unknown)}",
            ))
            continue
        non_callable = sorted(
            action_id for action_id, handler in handlers.items()
            if not callable(handler)
        )
        if non_callable:
            issues.append(ValidationIssue(
                "error",
                f"{ext.name}: non-callable handlers: "
                f"{', '.join(non_callable)}",
            ))
            continue
        handlers_by_extension[ext.name] = handlers
        usable.append((
            ext, capability_specs, action_specs, assistant_recipes,
        ))

    existing_capability_ids = {
        spec.capability_id for spec in actions.capabilities.all()
    }
    capability_counts = Counter(
        spec.capability_id
        for _, capability_specs, _, _ in usable
        for spec in capability_specs
    )
    invalid_extensions = set()
    valid_capabilities = []
    staging_capabilities = actions.capabilities.clone()
    for ext, capability_specs, _, _ in usable:
        duplicates = [
            spec.capability_id for spec in capability_specs
            if (
                spec.capability_id in existing_capability_ids
                or capability_counts[spec.capability_id] > 1
            )
        ]
        if duplicates:
            invalid_extensions.add(ext.name)
            for capability in duplicates:
                issues.append(ValidationIssue(
                    "error",
                    f"{ext.name}: duplicate capability_id '{capability}'",
                ))
            continue
        try:
            for spec in capability_specs:
                staging_capabilities.register(spec)
        except ValueError as error:
            issues.append(ValidationIssue(
                "error",
                f"{ext.name}: capability assembly failed: {error}",
            ))
            invalid_extensions.add(ext.name)
            continue
        valid_capabilities.extend((ext, spec) for spec in capability_specs)

    staging_actions = ActionCatalog(staging_capabilities)
    for spec in actions.all():
        staging_actions.register(spec)
    existing_action_ids = {spec.action_id for spec in actions.all()}
    action_counts = Counter(
        spec.action_id
        for _, _, action_specs, _ in usable
        for spec in action_specs
    )
    valid_actions = []
    for ext, _, action_specs, _ in usable:
        if ext.name in invalid_extensions:
            continue
        duplicates = [
            spec.action_id for spec in action_specs
            if (
                spec.action_id in existing_action_ids
                or action_counts[spec.action_id] > 1
            )
        ]
        if duplicates:
            invalid_extensions.add(ext.name)
            for action_id in duplicates:
                issues.append(ValidationIssue(
                    "error", f"{ext.name}: duplicate action_id '{action_id}'"
                ))
            continue
        try:
            for spec in action_specs:
                staging_actions.register(spec)
        except (TypeError, ValueError) as error:
            issues.append(ValidationIssue(
                "error", f"{ext.name}: action assembly failed: {error}"
            ))
            invalid_extensions.add(ext.name)
            continue
        for spec in action_specs:
            valid_actions.append((ext, spec))
    existing_recipe_ids = {recipe.assistant_id for recipe in recipes.all()}
    recipe_counts = Counter(
        recipe.assistant_id
        for _, _, _, assistant_recipes in usable
        for recipe in assistant_recipes
    )
    for ext, _, _, assistant_recipes in usable:
        duplicates = [
            recipe.assistant_id for recipe in assistant_recipes
            if (
                recipe.assistant_id in existing_recipe_ids
                or recipe_counts[recipe.assistant_id] > 1
            )
        ]
        if duplicates:
            invalid_extensions.add(ext.name)
            for assistant_id in duplicates:
                issues.append(ValidationIssue(
                    "error",
                    f"{ext.name}: duplicate assistant_id '{assistant_id}'",
                ))

    while True:
        current_capabilities = actions.capabilities.clone()
        for ext, capability_specs, _, _ in usable:
            if ext.name not in invalid_extensions:
                for spec in capability_specs:
                    if not current_capabilities.contains(
                        spec.capability_id
                    ):
                        current_capabilities.register(spec)
        current_actions = ActionCatalog(current_capabilities)
        for spec in actions.all():
            current_actions.register(spec)
        newly_invalid = set()
        for ext, _, action_specs, _ in usable:
            if ext.name in invalid_extensions:
                continue
            try:
                for spec in action_specs:
                    current_actions.register(spec)
            except (KeyError, TypeError, ValueError) as error:
                issues.append(ValidationIssue(
                    "error",
                    f"{ext.name}: action dependency failed: {error}",
                ))
                newly_invalid.add(ext.name)
        if newly_invalid:
            invalid_extensions.update(newly_invalid)
            continue
        current_recipes = RecipeCatalog(current_actions)
        for recipe in recipes.all():
            current_recipes.register(recipe)
        newly_invalid = set()
        for ext, _, _, assistant_recipes in usable:
            if ext.name in invalid_extensions:
                continue
            try:
                for recipe in assistant_recipes:
                    current_recipes.register(recipe)
            except (KeyError, TypeError, ValueError) as error:
                issues.append(ValidationIssue(
                    "error", f"{ext.name}: recipe assembly failed: {error}"
                ))
                newly_invalid.add(ext.name)
        if not newly_invalid:
            break
        invalid_extensions.update(newly_invalid)

    valid_recipes = [
        (ext, recipe)
        for ext, _, _, assistant_recipes in usable
        if ext.name not in invalid_extensions
        for recipe in assistant_recipes
    ]

    action_count = recipe_count = 0
    bindings: Dict[CapabilityId, str] = {}
    binding_conflicts = set()
    for ext, spec in valid_capabilities:
        if ext.name in invalid_extensions:
            continue
        actions.capabilities.register(spec)
    for ext, spec in valid_actions:
        if ext.name in invalid_extensions:
            continue
        actions.register(
            spec, handlers_by_extension[ext.name].get(spec.action_id)
        )
        action_count += 1
    for ext, recipe in valid_recipes:
        if ext.name in invalid_extensions:
            continue
        recipes.register(recipe)
        recipe_count += 1
    for ext, _, _, _ in usable:
        if ext.name in invalid_extensions:
            continue
        for capability, platform_id in extension_provider_bindings(ext).items():
            if not actions.capabilities.contains(capability):
                issues.append(ValidationIssue(
                    "error",
                    f"{ext.name}: provider binding references unknown "
                    f"capability '{capability.value}'",
                ))
                continue
            if capability in binding_conflicts:
                continue
            previous = bindings.get(capability)
            if previous is not None and previous != platform_id:
                issues.append(ValidationIssue(
                    "error",
                    f"conflicting provider binding for '{capability.value}': "
                    f"{previous} and {platform_id}",
                ))
                bindings[capability] = ""
                binding_conflicts.add(capability)
                continue
            bindings[capability] = platform_id
    return (action_count, recipe_count), bindings, issues


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

        try:
            ext_issues = validate_extension(ext, core_flow_ids=core_ids)
        except Exception as exc:
            issues.append(ValidationIssue(
                "error", f"{root.name}: validation failed: {exc}"
            ))
            continue
        if has_errors(ext_issues):
            issues.extend(
                ValidationIssue(i.level, f"{ext.name}: {i.message}") for i in ext_issues
            )
            continue
        try:
            staging = FlowRegistry()
            for flow in reg.all():
                staging.register(flow)
            count = merge_into_registry(staging, ext)
            for flow in staging.all():
                if flow.flow_id not in core_ids:
                    reg.register(flow)
                    core_ids.add(flow.flow_id)
            loaded += count
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
    module = _load_extension_module(ext, relative, "plugin")
    factory = getattr(module, "create_plugin", None)
    if not callable(factory):
        raise TypeError(f"{path} must export create_plugin(config)")
    plugin = factory(config or {})
    if not isinstance(plugin, Plugin):
        raise TypeError(f"{path} create_plugin did not return a Plugin")
    return plugin


def load_installed_plugins(
    base_dir: str | Path,
    config: Optional[dict] = None,
    issues: Optional[List[ValidationIssue]] = None,
) -> List[Plugin]:
    base = Path(base_dir).expanduser()
    if not base.exists():
        return []
    plugins = []
    owners = {}
    for root in sorted(path for path in base.iterdir() if path.is_dir()):
        if not (root / MANIFEST_NAME).exists():
            continue
        try:
            plugin = load_extension_plugin(load_extension(root), config)
        except (Exception, SystemExit) as error:
            message = (
                f"{root.name}: plugin load failed: "
                f"{type(error).__name__}: {error}"
            )
            if issues is not None:
                issues.append(ValidationIssue("error", message))
            else:
                warnings.warn(message, RuntimeWarning, stacklevel=2)
            continue
        if plugin is not None:
            previous = owners.get(plugin.platform_id)
            if previous is not None:
                message = (
                    f"duplicate platform_id '{plugin.platform_id}': "
                    f"{previous} and {root}"
                )
                if issues is not None:
                    issues.append(ValidationIssue("error", message))
                else:
                    warnings.warn(message, RuntimeWarning, stacklevel=2)
                plugins = [
                    item for item in plugins
                    if item.platform_id != plugin.platform_id
                ]
                continue
            owners[plugin.platform_id] = root
            plugins.append(plugin)
    return plugins
