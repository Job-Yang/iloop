"""Application action contracts, kept separate from platform driver capabilities."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

from .capability import Capability


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class ActionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionSideEffect(str, Enum):
    NONE = "none"
    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_WRITE = "external_write"
    PROCESS = "process"


@dataclass(frozen=True)
class ActionSpec:
    """A typed application action independent of any concrete provider."""

    action_id: str
    description: str
    risk: ActionRisk = ActionRisk.LOW
    side_effects: Tuple[ActionSideEffect, ...] = (ActionSideEffect.NONE,)
    allowed_assistants: Tuple[str, ...] = ()
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    required_capabilities: Tuple[Capability, ...] = ()
    disposition_kind: str = ""
    lifecycle_stage: str = "diagnosis"

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.action_id):
            raise ValueError(
                "action_id must be a namespaced identifier such as 'diagnosis.route'"
            )
        if not self.description.strip():
            raise ValueError(f"action '{self.action_id}' requires a description")
        object.__setattr__(self, "risk", ActionRisk(self.risk))
        effects = tuple(ActionSideEffect(item) for item in self.side_effects)
        if ActionSideEffect.NONE in effects and len(effects) > 1:
            raise ValueError(
                f"action '{self.action_id}' cannot combine side effect 'none'"
            )
        if len(set(effects)) != len(effects):
            raise ValueError(f"action '{self.action_id}' has duplicate side effects")
        object.__setattr__(self, "side_effects", effects)
        capabilities = tuple(Capability(item) for item in self.required_capabilities)
        if len(set(capabilities)) != len(capabilities):
            raise ValueError(
                f"action '{self.action_id}' has duplicate driver capabilities"
            )
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(
            self,
            "allowed_assistants",
            tuple(str(item) for item in self.allowed_assistants),
        )
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "outputs", dict(self.outputs))
        if self.disposition_kind not in {
            "", "code_change", "isolation", "human_handoff", "observe",
        }:
            raise ValueError(
                f"action '{self.action_id}' has invalid disposition_kind "
                f"'{self.disposition_kind}'"
            )
        if self.lifecycle_stage not in {
            "diagnosis", "disposition", "verification", "observation",
        }:
            raise ValueError(
                f"action '{self.action_id}' has invalid lifecycle_stage "
                f"'{self.lifecycle_stage}'"
            )
        if (
            self.disposition_kind
            and self.lifecycle_stage != "disposition"
        ):
            raise ValueError(
                f"action '{self.action_id}' with disposition_kind must use "
                "lifecycle_stage='disposition'"
            )

    def validate_input(self, payload: Mapping[str, object]) -> None:
        missing = sorted(set(self.inputs) - set(payload))
        if missing:
            raise ValueError(
                f"action '{self.action_id}' missing inputs: {', '.join(missing)}"
            )


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    assistant_id: str
    outputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", dict(self.outputs))


ActionHandler = Callable[[Mapping[str, object]], Mapping[str, object]]


class ActionCatalog:
    """Fail-closed registry for application action contracts and handlers."""

    def __init__(self) -> None:
        self._specs: Dict[str, ActionSpec] = {}
        self._handlers: Dict[str, ActionHandler] = {}

    def register(
        self,
        spec: ActionSpec,
        handler: Optional[ActionHandler] = None,
    ) -> None:
        if spec.action_id in self._specs:
            raise ValueError(f"duplicate action_id '{spec.action_id}'")
        if handler is not None and not callable(handler):
            raise TypeError(f"handler for '{spec.action_id}' must be callable")
        self._specs[spec.action_id] = spec
        if handler is not None:
            self._handlers[spec.action_id] = handler

    def get(self, action_id: str) -> ActionSpec:
        try:
            return self._specs[action_id]
        except KeyError as error:
            raise KeyError(f"unknown action '{action_id}'") from error

    def handler(self, action_id: str) -> ActionHandler:
        self.get(action_id)
        try:
            return self._handlers[action_id]
        except KeyError as error:
            raise ValueError(f"action '{action_id}' has no handler") from error

    def bind_handler(self, action_id: str, handler: ActionHandler) -> None:
        self.get(action_id)
        if action_id in self._handlers:
            raise ValueError(f"action '{action_id}' already has a handler")
        if not callable(handler):
            raise TypeError(f"handler for '{action_id}' must be callable")
        self._handlers[action_id] = handler

    def all(self) -> Tuple[ActionSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def required_capabilities(
        self,
        action_ids: Iterable[str],
    ) -> Tuple[Capability, ...]:
        capabilities = {
            capability
            for action_id in action_ids
            for capability in self.get(action_id).required_capabilities
        }
        return tuple(sorted(capabilities, key=lambda item: item.value))
