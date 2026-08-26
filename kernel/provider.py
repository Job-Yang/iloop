"""Driver capability routing across one or more platform providers."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .capability import (
    Capability, CapabilityCatalog, CapabilityId, CapabilityLike,
    CapabilityResult, CapabilityStatus, Plugin, capability_id, unsupported,
)


class ProviderRegistry:
    """Fail-closed capability router.

    A capability with multiple providers is ambiguous until explicitly bound.
    This prevents extension load order from silently selecting an executor.
    """

    def __init__(
        self,
        providers: Iterable[Plugin] = (),
        bindings: Optional[Mapping[CapabilityLike, str]] = None,
        *,
        capability_catalog: Optional[CapabilityCatalog] = None,
    ) -> None:
        self.capability_catalog = capability_catalog or CapabilityCatalog()
        self._providers: Dict[str, Plugin] = {}
        self._owners: Dict[CapabilityId, List[str]] = defaultdict(list)
        self._declared: Dict[str, Tuple[CapabilityId, ...]] = {}
        self._bindings: Dict[CapabilityId, str] = {}
        self._blocked: Dict[CapabilityId, str] = {}
        self._frozen = False
        for provider in providers:
            self.register(provider)
        for capability, platform_id in (bindings or {}).items():
            self.bind(capability, platform_id)

    def register(self, provider: Plugin) -> None:
        if self._frozen:
            raise ValueError("ProviderRegistry is frozen")
        platform_id = str(provider.platform_id).strip()
        if not platform_id:
            raise ValueError("provider platform_id cannot be empty")
        if platform_id in self._providers:
            raise ValueError(f"duplicate platform_id '{platform_id}'")
        capabilities = tuple(
            capability_id(item) for item in provider.capabilities()
        )
        if len(set(capabilities)) != len(capabilities):
            raise ValueError(
                f"provider '{platform_id}' declares duplicate capabilities"
            )
        missing = [
            item.value for item in capabilities
            if not self.capability_catalog.contains(item)
        ]
        if missing:
            raise ValueError(
                f"provider '{platform_id}' declares unknown capabilities: "
                f"{', '.join(sorted(missing))}"
            )
        self._providers[platform_id] = provider
        self._declared[platform_id] = capabilities
        for capability in capabilities:
            self._owners[capability].append(platform_id)

    def bind(self, capability: CapabilityLike, platform_id: str) -> None:
        if self._frozen:
            raise ValueError("ProviderRegistry is frozen")
        capability = capability_id(capability)
        self.capability_catalog.get(capability)
        if platform_id not in self._providers:
            raise ValueError(f"unknown provider '{platform_id}'")
        if platform_id not in self._owners.get(capability, []):
            raise ValueError(
                f"provider '{platform_id}' does not support '{capability.value}'"
            )
        self._bindings[capability] = platform_id
        self._blocked.pop(capability, None)

    def block(self, capability: CapabilityLike, reason: str) -> None:
        if self._frozen:
            raise ValueError("ProviderRegistry is frozen")
        capability = capability_id(capability)
        self.capability_catalog.get(capability)
        self._bindings.pop(capability, None)
        self._blocked[capability] = reason

    def resolve(self, capability: CapabilityLike) -> Optional[Plugin]:
        capability = capability_id(capability)
        self.capability_catalog.get(capability)
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
        capabilities: Iterable[CapabilityLike],
    ) -> None:
        errors = []
        for capability in sorted(
            {capability_id(item) for item in capabilities},
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

    def invoke(
        self, capability: CapabilityLike, **kwargs
    ) -> CapabilityResult:
        capability = capability_id(capability)
        spec = self.capability_catalog.get(capability)
        missing = sorted(set(spec.inputs) - set(kwargs))
        if missing:
            return CapabilityResult(
                platform="provider_registry",
                capability=capability.value,
                status=CapabilityStatus.ERROR,
                summary=(
                    f"capability '{capability.value}' missing inputs: "
                    f"{', '.join(missing)}"
                ),
            )
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
        provider_capability = (
            Capability(capability.value)
            if capability.value in Capability._value2member_map_
            else capability
        )
        result = provider.invoke(provider_capability, **kwargs)
        if not isinstance(result, CapabilityResult):
            return CapabilityResult(
                platform=provider.platform_id,
                capability=capability.value,
                status=CapabilityStatus.ERROR,
                summary=(
                    f"provider '{provider.platform_id}' returned "
                    "a non-CapabilityResult value"
                ),
            )
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
        outputs = result.metadata.get("outputs", {})
        missing_outputs = sorted(
            set(spec.outputs)
            - set(outputs if isinstance(outputs, Mapping) else {})
        )
        if result.ok() and missing_outputs:
            return CapabilityResult(
                platform=provider.platform_id,
                capability=capability.value,
                status=CapabilityStatus.ERROR,
                summary=(
                    f"provider '{provider.platform_id}' omitted outputs for "
                    f"'{capability.value}': {', '.join(missing_outputs)}"
                ),
            )
        return result

    def providers(self) -> Tuple[Plugin, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def freeze(self) -> None:
        self.capability_catalog.freeze()
        self._frozen = True

    def get(self, platform_id: str) -> Plugin:
        try:
            return self._providers[platform_id]
        except KeyError as error:
            raise KeyError(f"unknown provider '{platform_id}'") from error

    def bindings(self) -> Dict[str, str]:
        result = {}
        for capability in sorted(self._owners):
            try:
                provider = self.resolve(capability)
            except ValueError:
                continue
            if provider is not None:
                result[capability.value] = provider.platform_id
        return result

    def declarations(self) -> Dict[str, Tuple[str, ...]]:
        return {
            platform_id: tuple(
                item.value for item in capabilities
            )
            for platform_id, capabilities
            in sorted(self._declared.items())
        }

    def runtime_fingerprints(self) -> Dict[str, str]:
        """Return provider-owned implementation/configuration digests.

        Providers remain usable without this optional contract, but a Suite
        cannot claim production readiness for an unversioned provider.
        """
        result = {}
        for platform_id, provider in sorted(self._providers.items()):
            resolver = getattr(provider, "runtime_fingerprint", None)
            value = ""
            if callable(resolver):
                try:
                    value = str(resolver()).strip().lower()
                except (Exception, SystemExit):
                    value = ""
            if (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                value = ""
            result[platform_id] = value
        return result
