"""协议 2：能力契约 Capability Interface。

插件对内核暴露的统一动作面。不支持的能力返回 unsupported，而不是崩。
判成功看 status + artifacts，不只看 exit code。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Protocol, runtime_checkable


class Capability(str, Enum):
    DOCTOR = "doctor"
    BUILD = "build"
    INSTALL = "install"
    LAUNCH = "launch"
    LOGS = "logs"
    VIEW_TREE = "view_tree"
    SCREENSHOT = "screenshot"
    CRASH = "crash"
    PROBE = "probe"


class CapabilityStatus(str, Enum):
    SUCCESS = "success"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass
class CapabilityResult:
    platform: str
    capability: str
    status: CapabilityStatus
    summary: str
    evidence_dir: str = ""
    artifacts: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = CapabilityStatus(self.status)
        if isinstance(self.capability, Capability):
            self.capability = self.capability.value

    def ok(self) -> bool:
        return self.status == CapabilityStatus.SUCCESS

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "capability": self.capability,
            "status": self.status.value,
            "summary": self.summary,
            "evidence_dir": self.evidence_dir,
            "artifacts": list(self.artifacts),
        }


@runtime_checkable
class Plugin(Protocol):
    """插件契约。platform_id 唯一标识，capabilities 声明支持哪些能力。

    invoke 对任何 Capability 都必须能被调用：不支持就返回 UNSUPPORTED。
    """

    platform_id: str

    def capabilities(self) -> List[Capability]: ...

    def invoke(self, capability: Capability, **kwargs) -> CapabilityResult: ...


def unsupported(platform: str, capability: Capability) -> CapabilityResult:
    """给插件的便捷助手：统一构造 unsupported 返回。"""
    return CapabilityResult(
        platform=platform,
        capability=capability.value,
        status=CapabilityStatus.UNSUPPORTED,
        summary=f"{platform} does not support capability '{capability.value}'",
    )
