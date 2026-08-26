"""Deployment contracts that stay orthogonal to assistant recipes."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

from .capability import CapabilityId, CapabilityLike, capability_id
from .provider import ProviderRegistry
from .recipe import AssistantAssembly, RecipeCatalog


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")


@dataclass(frozen=True)
class DeploymentProfile:
    deployment_id: str
    target_node: str
    provider_ids: Tuple[str, ...]
    provider_bindings: Mapping[CapabilityLike, str] = field(default_factory=dict)
    features: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.deployment_id):
            raise ValueError(
                "deployment_id must be a namespaced identifier"
            )
        if not self.target_node.strip():
            raise ValueError("deployment target_node cannot be empty")
        providers = tuple(str(item) for item in self.provider_ids)
        if not providers or len(set(providers)) != len(providers):
            raise ValueError(
                "deployment provider_ids must be non-empty and unique"
            )
        object.__setattr__(self, "provider_ids", providers)
        object.__setattr__(
            self,
            "provider_bindings",
            {
                capability_id(capability): str(platform_id)
                for capability, platform_id in self.provider_bindings.items()
            },
        )
        object.__setattr__(self, "features", dict(self.features))


@dataclass(frozen=True)
class DeploymentAssembly:
    profile: DeploymentProfile
    assistant: AssistantAssembly
    providers: ProviderRegistry
    recipes: RecipeCatalog

    def __post_init__(self) -> None:
        if not self.recipes.owns(self.assistant):
            raise ValueError(
                "DeploymentAssembly assistant and RecipeCatalog must share "
                "the same assembly source"
            )

    def fingerprint(self) -> str:
        payload = {
            "deployment_id": self.profile.deployment_id,
            "target_node": self.profile.target_node,
            "provider_ids": list(self.profile.provider_ids),
            "provider_bindings": {
                capability.value: platform_id
                for capability, platform_id
                in self.profile.provider_bindings.items()
            },
            "features": dict(self.profile.features),
            "resolved_bindings": self.providers.bindings(),
            "provider_capabilities": self.providers.declarations(),
            "provider_runtime_fingerprints": (
                self.providers.runtime_fingerprints()
            ),
        }
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()


def assemble_deployment(
    profile: DeploymentProfile,
    recipes: RecipeCatalog,
    providers: ProviderRegistry,
    assistant_id: str,
) -> DeploymentAssembly:
    """Scope providers to a node, then validate the recipe before dispatch."""
    available = {item.platform_id: item for item in providers.providers()}
    missing = sorted(set(profile.provider_ids) - set(available))
    if missing:
        raise ValueError(
            f"deployment '{profile.deployment_id}' missing providers: "
            f"{', '.join(missing)}"
        )
    scoped = ProviderRegistry(
        [available[platform_id] for platform_id in profile.provider_ids],
        capability_catalog=providers.capability_catalog,
    )
    for capability, platform_id in profile.provider_bindings.items():
        if platform_id not in profile.provider_ids:
            raise ValueError(
                f"deployment binding '{capability.value}' references unavailable "
                f"provider '{platform_id}'"
            )
        scoped.bind(capability, platform_id)
    assistant = recipes.assemble(assistant_id)
    unavailable = sorted(
        capability.value
        for capability in assistant.required_capabilities
        if (
            providers.capability_catalog.get(
                capability
            ).supported_deployments
            and profile.deployment_id not in (
                providers.capability_catalog.get(
                    capability
                ).supported_deployments
            )
        )
    )
    if unavailable:
        raise ValueError(
            f"deployment '{profile.deployment_id}' does not support "
            f"capabilities: {', '.join(unavailable)}"
        )
    scoped.validate_capabilities(assistant.required_capabilities)
    scoped.freeze()
    return DeploymentAssembly(
        profile=profile,
        assistant=assistant,
        providers=scoped,
        recipes=recipes,
    )
