"""iLoop kernel — 平台无关的验证驱动闭环内核。

内核只认协议，不认识任何具体平台（见 SPEC.md）。插件通过实现 Capability 契约插进来。

子系统：
  evidence   证据（observed vs inferred）      —— 一切结论的地基
  capability 能力契约 + Plugin 协议            —— 插件对内核暴露的统一动作面
  flow       任务路由与自治分级                —— 先诊断、再选流、后取证
  lesson     错题本                            —— 踩过的坑召回并前置
  gate       四道关卡（时间/范围/机制/反证）   —— 收敛必须过四关
  experts    诊断方法专家                      —— 只回答"怎么想"，不绑平台
  case       病例状态机                        —— 任务是档案，不是对话
  ledger     记账 + 外显 + 反循环闸门          —— 【iLoop】前缀协议
  acceptance 独立验收                          —— 谁做的不能由谁判
  channel    事件源 + 通知接口                 —— oncall 的通用抽取
  gate_capability 能力 Gate（阻塞状态机）      —— 权限缺失即停，不伪装收口
"""

from .evidence import EvidenceArtifact, EvidenceKind
from .capability import Capability, CapabilityResult, CapabilityStatus, Plugin, unsupported
from .flow import Flow, Autonomy, FlowRegistry
from .lesson import Lesson, LessonBook
from .gate import FourGate, GateResult
from .experts import Expert, ExpertRegistry
from .case import Case, CaseStatus, Hypothesis, HypothesisStatus, TestSpec
from .ledger import Ledger, Round, RoundStatus, render, BRAND, PHASES
from .acceptance import (
    RiskLevel, assess_risk, needs_independent_review,
    AcceptancePackage, AcceptanceResult, Verdict, IndependentReviewer,
    ChangeScore, score_change, AcceptanceStore,
)
from .channel import (
    Event, EventSource, Notifier, StdoutNotifier, WebhookNotifier, StaticEventSource,
)
from .gate_capability import CapabilityGate, RequiredOperation, OpStatus
from .runner import CommandRunner, CommandOutput
from .extension import (
    Extension, scaffold_extension, load_extension, validate_extension,
    merge_into_registry, load_installed_extensions, load_extension_plugin,
    load_installed_plugins, has_errors, ValidationIssue,
)
from .redline import (
    check_command, guard_command, guard_write_path, RedlineViolation,
)
from .dashboard import Dashboard
from .task import TaskRecord, TaskStatus, TaskStep, StepStatus, TaskStore
from .runtime import Runtime
from .global_review import GlobalReview, ImpactItem, analyze_global_impact
from .project import ProjectMemory
from .ui_flow import UIFlow, UINode, UIFlowStore, ACTION_CAPABILITY


def discover_developer_dir():
    """Deprecated compatibility facade for the former kernel export."""
    import warnings
    warnings.warn(
        "kernel.discover_developer_dir is deprecated; use "
        "plugins.ios_native.environment.discover_developer_dir",
        DeprecationWarning,
        stacklevel=2,
    )
    from plugins.ios_native.environment import (
        discover_developer_dir as discover,
    )
    return discover()


__all__ = [
    "EvidenceArtifact", "EvidenceKind",
    "Capability", "CapabilityResult", "CapabilityStatus", "Plugin", "unsupported",
    "Flow", "Autonomy", "FlowRegistry",
    "Lesson", "LessonBook",
    "FourGate", "GateResult",
    "Expert", "ExpertRegistry",
    "Case", "CaseStatus", "Hypothesis", "HypothesisStatus", "TestSpec",
    "Ledger", "Round", "RoundStatus", "render", "BRAND", "PHASES",
    "RiskLevel", "assess_risk", "needs_independent_review",
    "AcceptancePackage", "AcceptanceResult", "Verdict", "IndependentReviewer",
    "ChangeScore", "score_change", "AcceptanceStore",
    "Event", "EventSource", "Notifier", "StdoutNotifier", "WebhookNotifier", "StaticEventSource",
    "CapabilityGate", "RequiredOperation", "OpStatus",
    "CommandRunner", "CommandOutput", "discover_developer_dir",
    "Extension", "scaffold_extension", "load_extension", "validate_extension",
    "merge_into_registry", "load_installed_extensions", "load_extension_plugin",
    "load_installed_plugins", "has_errors", "ValidationIssue",
    "check_command", "guard_command", "guard_write_path", "RedlineViolation",
    "Dashboard",
    "TaskRecord", "TaskStatus", "TaskStep", "StepStatus", "TaskStore", "Runtime",
    "GlobalReview", "ImpactItem", "analyze_global_impact",
    "ProjectMemory",
    "UIFlow", "UINode", "UIFlowStore", "ACTION_CAPABILITY",
]

__version__ = "0.1.0"
