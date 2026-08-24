"""Driver capability routing across one or more platform providers."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .capability import (
    Capability, CapabilityResult, CapabilityStatus, Plugin, unsupported,
)


class ProviderRegistry:
    """Fail-closed capability router.

    A capability with multiple providers is ambiguous until explicitly bound.
    This prevents extension load order from silently selecting an executor.
    """

    def __init__(
        self,
        providers: Iterable[Plugin] = (),
        bindings: Optional[Mapping[Capability, str]] = None,
    ) -> None:
        self._providers: Dict[str, Plugin] = {}
        self._owners: Dict[Capability, List[str]] = defaultdict(list)
        self._bindings: Dict[Capability, str] = {}
        self._blocked: Dict[Capability, str] = {}
        self._frozen = False
        for provider in providers:
            self.register(provider)
        for capability, platform_id in (bindings or {}).items():
            self.bind(Capability(capability), platform_id)

    def register(self, provider: Plugin) -> None:
        if self._frozen:
            raise ValueError("ProviderRegistry is frozen")
        platform_id = str(provider.platform_id).strip()
        if not platform_id:
            raise ValueError("provider platform_id cannot be empty")
        if platform_id in self._providers:
            raise ValueError(f"duplicate platform_id '{platform_id}'")
        capabilities = tuple(Capability(item) for item in provider.capabilities())
        if len(set(capabilities)) != len(capabilities):
            raise ValueError(
                f"provider '{platform_id}' declares duplicate capabilities"
            )
        self._providers[platform_id] = provider
        for capability in capabilities:
            self._owners[capability].append(platform_id)

    def bind(self, capability: Capability, platform_id: str) -> None:
        if self._frozen:
            raise ValueError("ProviderRegistry is frozen")
        capability = Capability(capability)
        if platform_id not in self._providers:
            raise ValueError(f"unknown provider '{platform_id}'")
        if platform_id not in self._owners.get(capability, []):
            raise ValueError(
                f"provider '{platform_id}' does not support '{capability.value}'"
            )
        self._bindings[capability] = platform_id
        self._blocked.pop(capability, None)

    def block(self, capability: Capability, reason: str) -> None:
        if self._frozen:
            raise ValueError("ProviderRegistry is frozen")
        capability = Capability(capability)
        self._bindings.pop(capability, None)
        self._blocked[capability] = reason

    def resolve(self, capability: Capability) -> Optional[Plugin]:
        capability = Capability(capability)
        if capability in self._blocked:
            raise ValueError(
                f"provider for '{capability.value}' is blocked: "
                f"{self._blocked[capability]}"
            )
        bound = self._bindings.get(capability)
        if bound:
            return self._providers[bound]
        owners = self._owners.get(capability, [])
        if not owners:
            return None
        if len(owners) > 1:
            raise ValueError(
                f"ambiguous provider for '{capability.value}': "
                f"{', '.join(sorted(owners))}; add an explicit binding"
            )
        return self._providers[owners[0]]

    def validate_capabilities(
        self,
        capabilities: Iterable[Capability],
    ) -> None:
        errors = []
        for capability in sorted(
            {Capability(item) for item in capabilities},
            key=lambda item: item.value,
        ):
            try:
                provider = self.resolve(capability)
            except ValueError as error:
                errors.append(str(error))
                continue
            if provider is None:
                errors.append(f"no provider for '{capability.value}'")
        if errors:
            raise ValueError("provider assembly failed: " + "; ".join(errors))

    def invoke(self, capability: Capability, **kwargs) -> CapabilityResult:
        capability = Capability(capability)
        try:
            provider = self.resolve(capability)
        except ValueError as error:
            return CapabilityResult(
                platform="provider_registry",
                capability=capability.value,
                status=CapabilityStatus.ERROR,
                summary=str(error),
            )
        if provider is None:
            return unsupported("provider_registry", capability)
        result = provider.invoke(capability, **kwargs)
        if result.platform != provider.platform_id:
            return CapabilityResult(
                platform=provider.platform_id,
                capability=capability.value,
                status=CapabilityStatus.ERROR,
                summary=(
                    f"provider '{provider.platform_id}' returned mismatched "
                    f"platform '{result.platform}'"
                ),
            )
        if result.capability != capability.value:
            return CapabilityResult(
                platform=provider.platform_id,
                capability=capability.value,
                status=CapabilityStatus.ERROR,
                summary=(
                    f"provider '{provider.platform_id}' returned mismatched "
                    f"capability '{result.capability}'"
                ),
            )
        return result

    def providers(self) -> Tuple[Plugin, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def freeze(self) -> None:
        self._frozen = True

    def get(self, platform_id: str) -> Plugin:
        try:
            return self._providers[platform_id]
        except KeyError as error:
            raise KeyError(f"unknown provider '{platform_id}'") from error

    def bindings(self) -> Dict[str, str]:
        result = {}
        for capability in Capability:
            try:
                provider = self.resolve(capability)
            except ValueError:
                continue
            if provider is not None:
                result[capability.value] = provider.platform_id
        return result
