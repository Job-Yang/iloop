"""Declarative assistant recipes and application action execution."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

from .action import ActionCatalog, ActionResult, ActionRisk, ActionSpec
from .capability import Capability


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class AssistantRecipe:
    assistant_id: str
    actions: Tuple[str, ...]
    version: str = "1"
    ingress: Tuple[str, ...] = ()
    continuous_observation: bool = False
    action_risks: Mapping[str, ActionRisk] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.assistant_id):
            raise ValueError(
                "assistant_id must be a namespaced identifier such as 'team.oncall'"
            )
        actions = tuple(str(item) for item in self.actions)
        if not actions:
            raise ValueError(f"assistant '{self.assistant_id}' requires actions")
        if len(set(actions)) != len(actions):
            raise ValueError(f"assistant '{self.assistant_id}' has duplicate actions")
        object.__setattr__(self, "actions", actions)
        if not str(self.version).strip():
            raise ValueError(f"assistant '{self.assistant_id}' requires a version")
        object.__setattr__(self, "version", str(self.version))
        object.__setattr__(self, "ingress", tuple(str(item) for item in self.ingress))
        object.__setattr__(
            self,
            "action_risks",
            {
                str(action_id): ActionRisk(risk)
                for action_id, risk in self.action_risks.items()
            },
        )


@dataclass(frozen=True)
class AssistantAssembly:
    recipe: AssistantRecipe
    actions: Tuple[ActionSpec, ...]
    required_capabilities: Tuple[Capability, ...]
    catalog_token: object = field(repr=False)

    def fingerprint(self) -> str:
        payload = {
            "assistant_id": self.recipe.assistant_id,
            "version": self.recipe.version,
            "actions": [
                {
                    "action_id": spec.action_id,
                    "description": spec.description,
                    "risk": spec.risk.value,
                    "side_effects": [
                        item.value for item in spec.side_effects
                    ],
                    "allowed_assistants": list(spec.allowed_assistants),
                    "inputs": dict(spec.inputs),
                    "outputs": dict(spec.outputs),
                    "required_capabilities": [
                        item.value for item in spec.required_capabilities
                    ],
                    "disposition_kind": spec.disposition_kind,
                    "lifecycle_stage": spec.lifecycle_stage,
                }
                for spec in self.actions
            ],
            "ingress": list(self.recipe.ingress),
            "continuous_observation": self.recipe.continuous_observation,
        }
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def driver_plan(self) -> Tuple[str, ...]:
        return tuple(
            capability.value
            for spec in self.actions
            for capability in spec.required_capabilities
        )

    def disposition_actions(self) -> Dict[str, str]:
        return {
            spec.action_id: spec.disposition_kind
            for spec in self.actions
            if spec.disposition_kind
        }

    def evidence_plan(self) -> Tuple[str, ...]:
        return tuple(
            item
            for spec in self.actions
            for item in (
                *(
                    f"capability:{capability.value}"
                    for capability in spec.required_capabilities
                ),
                f"action:{spec.action_id}",
            )
        )


class RecipeCatalog:
    """Validates recipes against the available application action catalog."""

    def __init__(self, actions: ActionCatalog) -> None:
        self.actions = actions
        self._recipes: Dict[str, AssistantRecipe] = {}
        self._token = object()

    def register(self, recipe: AssistantRecipe) -> None:
        if recipe.assistant_id in self._recipes:
            raise ValueError(f"duplicate assistant_id '{recipe.assistant_id}'")
        specs = tuple(self.actions.get(action_id) for action_id in recipe.actions)
        stage_order = {
            "diagnosis": 0,
            "disposition": 1,
            "verification": 2,
            "observation": 3,
        }
        stages = [stage_order[spec.lifecycle_stage] for spec in specs]
        if stages != sorted(stages):
            raise ValueError(
                f"assistant '{recipe.assistant_id}' actions violate "
                "lifecycle order"
            )
        extra_risks = sorted(set(recipe.action_risks) - set(recipe.actions))
        if extra_risks:
            raise ValueError(
                f"assistant '{recipe.assistant_id}' declares risk for unknown actions: "
                f"{', '.join(extra_risks)}"
            )
        for spec in specs:
            if (
                spec.allowed_assistants
                and recipe.assistant_id not in spec.allowed_assistants
            ):
                raise ValueError(
                    f"action '{spec.action_id}' does not allow assistant "
                    f"'{recipe.assistant_id}'"
                )
            declared_risk = recipe.action_risks.get(spec.action_id)
            if declared_risk is not None and declared_risk != spec.risk:
                raise ValueError(
                    f"assistant '{recipe.assistant_id}' risk mismatch for "
                    f"'{spec.action_id}': recipe={declared_risk.value}, "
                    f"action={spec.risk.value}"
                )
        self._recipes[recipe.assistant_id] = recipe

    def get(self, assistant_id: str) -> AssistantRecipe:
        try:
            return self._recipes[assistant_id]
        except KeyError as error:
            raise KeyError(f"unknown assistant '{assistant_id}'") from error

    def assemble(self, assistant_id: str) -> AssistantAssembly:
        recipe = self.get(assistant_id)
        specs = tuple(self.actions.get(action_id) for action_id in recipe.actions)
        return AssistantAssembly(
            recipe=recipe,
            actions=specs,
            required_capabilities=self.actions.required_capabilities(recipe.actions),
            catalog_token=self._token,
        )

    def all(self) -> Tuple[AssistantRecipe, ...]:
        return tuple(self._recipes[key] for key in sorted(self._recipes))

    def owns(self, assembly: AssistantAssembly) -> bool:
        if assembly.catalog_token is not self._token:
            return False
        try:
            current = self.assemble(assembly.recipe.assistant_id)
        except (KeyError, ValueError):
            return False
        return (
            current.recipe == assembly.recipe
            and current.actions == assembly.actions
            and current.required_capabilities
            == assembly.required_capabilities
        )

    def execute(
        self,
        assistant_id: str,
        action_id: str,
        payload: Mapping[str, object],
    ) -> ActionResult:
        assembly = self.assemble(assistant_id)
        if action_id not in assembly.recipe.actions:
            raise ValueError(
                f"assistant '{assistant_id}' does not include action '{action_id}'"
            )
        spec = self.actions.get(action_id)
        spec.validate_input(payload)
        outputs = dict(self.actions.handler(action_id)(dict(payload)))
        missing = sorted(set(spec.outputs) - set(outputs))
        if missing:
            raise ValueError(
                f"action '{action_id}' missing outputs: {', '.join(missing)}"
            )
        return ActionResult(
            action_id=action_id,
            assistant_id=assistant_id,
            outputs=outputs,
        )
