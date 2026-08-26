"""协议 2：可扩展 Driver Capability 契约。

插件对内核暴露的统一动作面。不支持的能力返回 unsupported，而不是崩。
判成功看 status + artifacts，不只看 exit code。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Protocol, Tuple, Union, runtime_checkable


class Capability(str, Enum):
    DOCTOR = "doctor"
    BUILD = "build"
    RUN = "run"
    INSTALL = "install"
    LAUNCH = "launch"
    LOGS = "logs"
    VIEW_TREE = "view_tree"
    SCREENSHOT = "screenshot"
    CRASH = "crash"
    PROBE = "probe"
    COUNTER_PROBE = "counter_probe"
    TAP = "tap"
    SWIPE = "swipe"
    TYPE_TEXT = "type_text"
    UI_PREPARE = "ui_prepare"
    UI_STATUS = "ui_status"
    UI_STOP = "ui_stop"


BUILTIN_CAPABILITY_SIDE_EFFECTS = {
    Capability.DOCTOR: "read",
    Capability.BUILD: "workspace_write",
    Capability.RUN: "process",
    Capability.INSTALL: "external_write",
    Capability.LAUNCH: "process",
    Capability.LOGS: "read",
    Capability.VIEW_TREE: "read",
    Capability.SCREENSHOT: "read",
    Capability.CRASH: "read",
    Capability.PROBE: "read",
    Capability.COUNTER_PROBE: "read",
    Capability.TAP: "external_write",
    Capability.SWIPE: "external_write",
    Capability.TYPE_TEXT: "external_write",
    Capability.UI_PREPARE: "external_write",
    Capability.UI_STATUS: "read",
    Capability.UI_STOP: "process",
}


_CAPABILITY_ID = re.compile(
    r"^[a-z][a-z0-9_]*(?:[.-][a-z][a-z0-9_]*)*$"
)


class CapabilityId(str):
    """String capability identifier with Enum-compatible ``.value`` access."""

    def __new__(cls, value: object):
        normalized = (
            value.value if isinstance(value, Capability)
            else str(value)
        ).strip()
        if not _CAPABILITY_ID.fullmatch(normalized):
            raise ValueError(f"invalid capability id: {normalized!r}")
        return str.__new__(cls, normalized)

    @property
    def value(self) -> str:
        return str(self)


CapabilityLike = Union[Capability, CapabilityId, str]


def capability_id(value: CapabilityLike) -> CapabilityId:
    return value if isinstance(value, CapabilityId) else CapabilityId(value)


@dataclass(frozen=True)
class CapabilitySpec:
    """A Driver Capability declaration independent of its Provider."""

    capability_id: CapabilityId
    description: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    side_effect: str = "none"
    required_tools: Tuple[Tuple[str, ...], ...] = ()
    supported_deployments: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capability_id", capability_id(self.capability_id)
        )
        if not self.description.strip():
            raise ValueError(
                f"capability '{self.capability_id}' requires a description"
            )
        if self.side_effect not in {
            "none", "read", "workspace_write", "external_write", "process",
        }:
            raise ValueError(
                f"capability '{self.capability_id}' has invalid side_effect "
                f"'{self.side_effect}'"
            )
        if any(
            isinstance(group, (str, bytes))
            for group in self.required_tools
        ):
            raise ValueError(
                f"capability '{self.capability_id}' tool groups must be arrays"
            )
        groups = tuple(
            tuple(str(item).strip() for item in group if str(item).strip())
            for group in self.required_tools
        )
        if any(not group for group in groups):
            raise ValueError(
                f"capability '{self.capability_id}' has an empty tool group"
            )
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "outputs", dict(self.outputs))
        object.__setattr__(self, "required_tools", groups)
        deployments = tuple(
            str(item).strip() for item in self.supported_deployments
        )
        if (
            any(not item for item in deployments)
            or len(set(deployments)) != len(deployments)
        ):
            raise ValueError(
                f"capability '{self.capability_id}' deployments must "
                "be non-empty and unique"
            )
        object.__setattr__(self, "supported_deployments", deployments)

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id.value,
            "description": self.description,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "side_effect": self.side_effect,
            "required_tools": [
                list(group) for group in self.required_tools
            ],
            "supported_deployments": list(
                self.supported_deployments
            ),
        }

class CapabilityCatalog:
    """Fail-closed registry for built-in and extension Driver Capabilities."""

    def __init__(
        self,
        specs: Iterable[CapabilitySpec] = (),
        *,
        include_builtins: bool = True,
    ) -> None:
        self._specs: Dict[CapabilityId, CapabilitySpec] = {}
        self._frozen = False
        if include_builtins:
            for item in Capability:
                self.register(CapabilitySpec(
                    capability_id=CapabilityId(item),
                    description=f"Built-in {item.value} capability",
                    side_effect=BUILTIN_CAPABILITY_SIDE_EFFECTS[item],
                ))
        for spec in specs:
            self.register(spec)

    def register(self, spec: CapabilitySpec) -> None:
        if self._frozen:
            raise ValueError("CapabilityCatalog is frozen")
        key = capability_id(spec.capability_id)
        if key in self._specs:
            raise ValueError(f"duplicate capability id '{key}'")
        self._specs[key] = spec

    def get(self, value: CapabilityLike) -> CapabilitySpec:
        key = capability_id(value)
        try:
            return self._specs[key]
        except KeyError as error:
            raise KeyError(f"unknown capability '{key}'") from error

    def contains(self, value: CapabilityLike) -> bool:
        try:
            self.get(value)
        except (KeyError, ValueError):
            return False
        return True

    def all(self) -> Tuple[CapabilitySpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def clone(self) -> "CapabilityCatalog":
        return CapabilityCatalog(self.all(), include_builtins=False)

    def freeze(self) -> None:
        self._frozen = True


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
    metadata: Dict[str, object] = field(default_factory=dict)

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
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class Plugin(Protocol):
    """插件契约。platform_id 唯一标识，capabilities 声明支持哪些能力。

    invoke 对任何 Capability 都必须能被调用：不支持就返回 UNSUPPORTED。
    """

    platform_id: str

    def capabilities(self) -> List[CapabilityLike]: ...

    def invoke(
        self, capability: CapabilityLike, **kwargs
    ) -> CapabilityResult: ...


def unsupported(
    platform: str, capability: CapabilityLike
) -> CapabilityResult:
    """给插件的便捷助手：统一构造 unsupported 返回。"""
    identifier = capability_id(capability)
    return CapabilityResult(
        platform=platform,
        capability=identifier.value,
        status=CapabilityStatus.UNSUPPORTED,
        summary=f"{platform} does not support capability '{identifier.value}'",
    )
