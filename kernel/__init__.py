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
from .capability import (
    Capability, CapabilityCatalog, CapabilityId, CapabilityLike,
    CapabilityResult, CapabilitySpec, CapabilityStatus, Plugin,
    capability_id, unsupported,
)
from .action import (
    ActionCatalog, ActionHandler, ActionResult, ActionRisk, ActionSideEffect,
    ActionSpec,
)
from .recipe import (
    AssistantAssembly, AssistantRecipe, RecipeCatalog,
)
from .provider import ProviderRegistry
from .deployment import (
    DeploymentAssembly, DeploymentProfile, assemble_deployment,
)
from .execution import (
    LocalRecipeWorker, ReplayGuard, TaskEnvelope, WorkerEvidence, WorkerReceipt,
)
from .authorization import (
    AuthorizationGrant, AuthorizationVerifier, HMACAuthorizationAuthority,
)
from .source import (
    CandidateLineage, CandidateRevision, ChangeRequestReceipt,
    ChangeSnapshot, CICheckReceipt,
)
from .suite import (
    AssistantSuite, SmokeCheck, SmokeReceipt, SuiteManifest, SuiteMember,
)
from .tool_guard import authorize_tool_use
from .flow import Flow, Autonomy, FlowRegistry
from .lesson import Lesson, LessonBook
from .gate import FourGate, GateResult
from .experts import Expert, ExpertRegistry
from .case import (
    Case, CaseStatus, DiagnosisRevision, DiagnosisStatus, DispositionKind,
    DispositionPlan, DispositionStatus, Hypothesis, HypothesisStatus,
    ObservationStatus, TestSpec, VerificationRecord, VerificationStatus,
)
from .ledger import (
    Ledger, Round, RoundStatus, TimingEvent, render, BRAND, PHASES,
)
from .acceptance import (
    RiskLevel, assess_risk, needs_independent_review,
    AcceptancePackage, AcceptanceResult, Verdict, IndependentReviewer,
    AcceptanceBatch, AcceptanceShard, ChangeScore, score_change,
    AcceptanceStore, aggregate_criteria_verdicts,
)
from .channel import (
    Event, EventSource, Notifier, StdoutNotifier, WebhookNotifier, StaticEventSource,
)
from .gate_capability import CapabilityGate, RequiredOperation, OpStatus
from .runner import CommandRunner, CommandOutput
from .extension import (
    Extension, scaffold_extension, load_extension, validate_extension,
    merge_into_registry, load_installed_extensions, load_extension_plugin,
    load_installed_plugins, load_extension_application,
    load_extension_action_handlers, load_installed_application,
    extension_provider_bindings, has_errors, ValidationIssue,
)
from .redline import (
    check_command, guard_command, guard_write_path, RedlineViolation,
)
from .dashboard import Dashboard
from .task import TaskRecord, TaskStatus, TaskStep, StepStatus, TaskStore
from .runtime import Runtime
from .global_review import (
    GlobalReview, ImpactItem, analyze_global_impact,
    THREE_VERDICT_PROTOCOL, DESIGN_CONTRACT_FIELDS, design_contract_filled,
)
from .project import ProjectMemory
from .ui_flow import UIFlow, UINode, UIFlowStore, ACTION_CAPABILITY
from .host import HostTrustStore


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
    "Capability", "CapabilityCatalog", "CapabilityId", "CapabilityLike",
    "CapabilityResult", "CapabilitySpec", "CapabilityStatus", "Plugin",
    "capability_id", "unsupported",
    "ActionCatalog", "ActionHandler", "ActionResult", "ActionRisk",
    "ActionSideEffect", "ActionSpec",
    "AssistantAssembly", "AssistantRecipe", "RecipeCatalog",
    "ProviderRegistry",
    "DeploymentAssembly", "DeploymentProfile", "assemble_deployment",
    "LocalRecipeWorker", "ReplayGuard", "TaskEnvelope", "WorkerEvidence",
    "WorkerReceipt",
    "AuthorizationGrant", "AuthorizationVerifier",
    "HMACAuthorizationAuthority",
    "CandidateLineage", "CandidateRevision", "ChangeRequestReceipt",
    "ChangeSnapshot", "CICheckReceipt",
    "AssistantSuite", "SmokeCheck", "SmokeReceipt", "SuiteManifest",
    "SuiteMember", "authorize_tool_use",
    "Flow", "Autonomy", "FlowRegistry",
    "Lesson", "LessonBook",
    "FourGate", "GateResult",
    "Expert", "ExpertRegistry",
    "Case", "CaseStatus", "DiagnosisRevision", "DiagnosisStatus",
    "DispositionKind", "DispositionPlan", "DispositionStatus",
    "Hypothesis", "HypothesisStatus", "ObservationStatus", "TestSpec",
    "VerificationRecord", "VerificationStatus",
    "Ledger", "Round", "RoundStatus", "TimingEvent",
    "render", "BRAND", "PHASES",
    "RiskLevel", "assess_risk", "needs_independent_review",
    "AcceptancePackage", "AcceptanceResult", "Verdict", "IndependentReviewer",
    "ChangeScore", "score_change", "AcceptanceStore",
    "AcceptanceBatch", "AcceptanceShard", "aggregate_criteria_verdicts",
    "Event", "EventSource", "Notifier", "StdoutNotifier", "WebhookNotifier", "StaticEventSource",
    "CapabilityGate", "RequiredOperation", "OpStatus",
    "CommandRunner", "CommandOutput", "discover_developer_dir",
    "Extension", "scaffold_extension", "load_extension", "validate_extension",
    "merge_into_registry", "load_installed_extensions", "load_extension_plugin",
    "load_installed_plugins", "load_extension_application",
    "load_extension_action_handlers", "load_installed_application",
    "extension_provider_bindings", "has_errors", "ValidationIssue",
    "check_command", "guard_command", "guard_write_path", "RedlineViolation",
    "Dashboard",
    "TaskRecord", "TaskStatus", "TaskStep", "StepStatus", "TaskStore", "Runtime",
    "GlobalReview", "ImpactItem", "analyze_global_impact",
    "THREE_VERDICT_PROTOCOL", "DESIGN_CONTRACT_FIELDS", "design_contract_filled",
    "ProjectMemory",
    "UIFlow", "UINode", "UIFlowStore", "ACTION_CAPABILITY",
    "HostTrustStore",
]

__version__ = "0.4.0"
