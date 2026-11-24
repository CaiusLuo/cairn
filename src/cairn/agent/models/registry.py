"""Approved model catalog and the runtime selection layered on top of it.

Deployment files answer *what may be served*: the provider catalog, credentials
and the host allowlist.  This module answers *what is serving now*: a durable
provider/group/model reference plus a small cache of fully built runtimes.

Keeping those concerns apart is what lets a selection change without rewriting
``models.toml`` or regenerating an approval.  The registry is also the only
place that turns a model reference into a runtime, so chat, development,
reviews and knowledge ingestion all obey the same validation and snapshot rules.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from cairn.agent.models.config.admission import ApprovedCatalog, ApprovedProvider
from cairn.agent.models.runtime import (
    ProviderActivation,
    ProviderRuntime,
    build_provider_runtime,
    runtime_for_model,
)
from cairn.agent.models.selection import ModelRef


class ModelSelectionError(ValueError):
    """A model reference cannot be served by the current approved catalog."""


@dataclass(frozen=True)
class ModelChoice:
    """One selectable model, with the labels a client needs to show it."""

    ref: ModelRef
    provider_label: str
    group_label: str

    def as_dict(self) -> dict[str, str]:
        return {
            **self.ref.as_dict(),
            "provider_label": self.provider_label,
            "group_label": self.group_label,
        }


def _choices_for(providers: tuple[ApprovedProvider, ...]) -> tuple[ModelChoice, ...]:
    choices: list[ModelChoice] = []
    for approved in providers:
        provider = approved.provider
        for group in provider.config.model_config:
            for model_id in group.model_ids:
                ref = ModelRef(provider.name, group.name, model_id)
                choices.append(
                    ModelChoice(
                        ref=ref,
                        provider_label=provider.name,
                        group_label=group.name,
                    )
                )
    return tuple(choices)


def _first_approved(
    providers: tuple[ApprovedProvider, ...],
) -> ApprovedProvider | None:
    """Prefer a provider whose credential is ready for the initial default."""
    if not providers:
        return None
    return next(
        (provider for provider in providers if provider.missing_credential is None),
        providers[0],
    )


def _validate_with_catalog(catalog: ApprovedCatalog, ref: ModelRef) -> ApprovedProvider:
    approved = catalog.by_name(ref.provider)
    if approved is None:
        raise ModelSelectionError("Provider is not in the approved catalog")
    group = next(
        (
            group
            for group in approved.provider.config.model_config
            if group.name == ref.group
        ),
        None,
    )
    if group is None or ref.model not in group.model_ids:
        raise ModelSelectionError("Model is outside the approved groups")
    return approved


def _activation(
    approved: ApprovedProvider,
    ref: ModelRef,
    runtime: ProviderRuntime,
    *,
    secret_env_keys: frozenset[str],
    sensitive_values: tuple[str, ...],
) -> ProviderActivation:
    return ProviderActivation(
        runtime=runtime,
        secret_env_keys=secret_env_keys,
        sensitive_values=sensitive_values,
        ref=ref,
    )


class ProviderRegistry:
    """Current approved catalog plus the selected runtime.

    ``prepare`` methods build and validate without changing observable state.
    ``commit``/``install_prepared`` only publish already-built runtimes, so a
    failed build or a failed selection write leaves the previous model serving.
    """

    def __init__(
        self,
        catalog: ApprovedCatalog | None = None,
        selection: ModelRef | None = None,
        *,
        fallback: ProviderActivation | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._catalog = catalog
        self._fallback = fallback
        self._cache: dict[ModelRef, ProviderActivation] = {}
        self._selection: ModelRef | None = selection
        if selection is not None:
            try:
                self.prepare(selection)
            except ModelSelectionError:
                # A stale preference must not prevent startup.  The caller can
                # persist the fallback once the UI reads the effective state.
                self._selection = None
        if self._selection is None:
            self._selection = self._default_ref()

    # -- read -------------------------------------------------------------

    @property
    def catalog(self) -> ApprovedCatalog | None:
        return self._catalog

    @property
    def providers(self) -> tuple[ApprovedProvider, ...]:
        return () if self._catalog is None else self._catalog.providers

    def choices(self) -> tuple[ModelChoice, ...]:
        with self._lock:
            if self._catalog is not None:
                return _choices_for(self.providers)
            runtime = self._fallback.runtime if self._fallback is not None else None
            if runtime is None:
                return ()
            provider = runtime.provider
            return tuple(
                ModelChoice(
                    ref=ModelRef(provider.name, group.name, model_id),
                    provider_label=provider.name,
                    group_label=group.name,
                )
                for group in runtime.model_manager.list_models()
                for model_id in group.model_ids
            )

    def current_ref(self) -> ModelRef | None:
        with self._lock:
            return self._selection or self._default_ref()

    def current_activation(self) -> ProviderActivation | None:
        with self._lock:
            if self._catalog is None:
                return self._fallback
            ref = self.current_ref()
            if ref is None:
                return None
            return self.resolve(ref)

    def find(self, model_id: str, *, provider: str | None = None) -> ModelRef:
        """Resolve a bare model id, preferring the current provider.

        Model ids are not globally unique: two providers may expose the same
        id.  The current provider wins; otherwise the first approved match is
        used.  A provider name and a group name are also accepted for CLI
        convenience, but they must be unambiguous.
        """
        with self._lock:
            choices = self.choices()
            if provider is not None:
                choices = tuple(
                    choice for choice in choices if choice.ref.provider == provider
                )
            current = self.current_ref()
            ordered = sorted(
                choices,
                key=lambda choice: (
                    choice.ref.provider != (current.provider if current else ""),
                    choice.ref.group != (current.group if current else ""),
                ),
            )
            exact = [choice.ref for choice in ordered if choice.ref.model == model_id]
            if exact:
                return exact[0]
            groups = [choice.ref for choice in ordered if choice.ref.group == model_id]
            if groups:
                return groups[0]
            providers = [
                choice.ref for choice in ordered if choice.ref.provider == model_id
            ]
            if providers:
                return providers[0]
        raise ModelSelectionError("Model is outside the approved groups")

    def runtime_for_model(
        self, model_id: str, *, provider: str | None = None
    ) -> ProviderRuntime | None:
        """Resolve a model for a background role without changing selection."""
        try:
            ref = self.find(model_id, provider=provider)
        except ModelSelectionError:
            return None
        try:
            return self.prepare(ref).runtime
        except ModelSelectionError:
            return None

    # -- build ------------------------------------------------------------

    def _default_ref(self) -> ModelRef | None:
        if self._catalog is not None:
            approved = _first_approved(self._catalog.providers)
            if approved is None:
                return None
            group = approved.provider.config.model_config[0]
            return ModelRef(approved.provider.name, group.name, group.model_ids[0])
        if (
            self._fallback is not None
            and self._fallback.runtime.provider.config.model_config
        ):
            config = self._fallback.runtime.provider.config
            group = config.model_config[0]
            return ModelRef(
                self._fallback.runtime.provider.name, group.name, group.model_ids[0]
            )
        return None

    def prepare(self, ref: ModelRef) -> ProviderActivation:
        """Build one selection without publishing it."""
        with self._lock:
            cached = self._cache.get(ref)
            if cached is not None:
                return cached
            if self._catalog is not None:
                approved = _validate_with_catalog(self._catalog, ref)
                base = build_provider_runtime(
                    approved.provider,
                    approved.api_key,
                    approved.context_budget,
                    group=ref.group,
                    missing_credential=approved.missing_credential,
                )
                runtime = (
                    base
                    if base.model_manager.current_model_id() == ref.model
                    else runtime_for_model(base, ref.model)
                )
                activation = _activation(
                    approved,
                    ref,
                    runtime,
                    secret_env_keys=self._catalog.secret_env_keys,
                    sensitive_values=self._catalog.sensitive_values,
                )
            else:
                activation = self._prepare_fallback(ref)
            self._cache[ref] = activation
            return activation

    def _prepare_fallback(self, ref: ModelRef) -> ProviderActivation:
        fallback = self._fallback
        if fallback is None:
            raise ModelSelectionError("No approved provider is available")
        runtime = fallback.runtime
        if ref.provider != runtime.provider.name:
            raise ModelSelectionError("Provider is not in the approved catalog")
        selected = (
            runtime
            if runtime.model_manager.current_model_id() == ref.model
            else runtime_for_model(runtime, ref.model)
        )
        return replace(fallback, runtime=selected, ref=ref)

    def prepare_catalog(
        self, catalog: ApprovedCatalog, selection: ModelRef | None
    ) -> ProviderActivation:
        """Build a selection against a new catalog without installing it."""
        with self._lock:
            target = selection
            if target is not None:
                try:
                    _validate_with_catalog(catalog, target)
                except ModelSelectionError:
                    target = None
            if target is None:
                approved = _first_approved(catalog.providers)
                if approved is None:
                    raise ModelSelectionError("No approved provider is available")
                group = approved.provider.config.model_config[0]
                target = ModelRef(
                    approved.provider.name, group.name, group.model_ids[0]
                )
            approved = _validate_with_catalog(catalog, target)
            runtime = build_provider_runtime(
                approved.provider,
                approved.api_key,
                approved.context_budget,
                group=target.group,
                missing_credential=approved.missing_credential,
            )
            if runtime.model_manager.current_model_id() != target.model:
                runtime = runtime_for_model(runtime, target.model)
            return _activation(
                approved,
                target,
                runtime,
                secret_env_keys=catalog.secret_env_keys,
                sensitive_values=catalog.sensitive_values,
            )

    # -- publish ----------------------------------------------------------

    def commit(self, activation: ProviderActivation) -> None:
        """Publish an activation built by :meth:`prepare`."""
        ref = activation.ref
        if ref is None:
            raise ValueError("Activation has no model reference")
        with self._lock:
            if self._catalog is not None:
                self._fallback = None
            # In static fallback mode keep the unpinned runtime as the catalog
            # boundary; the committed activation is only the selected view.
            self._cache[ref] = activation
            self._selection = ref

    def install_prepared(
        self, catalog: ApprovedCatalog, activation: ProviderActivation
    ) -> None:
        """Publish a catalog and activation built by :meth:`prepare_catalog`."""
        with self._lock:
            self._catalog = catalog
            self._fallback = None
            self._cache = {activation.ref: activation} if activation.ref else {}
            self._selection = activation.ref

    def install_activation(self, activation: ProviderActivation) -> None:
        """Replace a single-runtime deployment, preserving old callers."""
        with self._lock:
            self._catalog = None
            self._fallback = activation
            self._selection = activation.ref or self._default_ref()
            self._cache = (
                {activation.ref: activation} if activation.ref is not None else {}
            )

    def resolve(self, ref: ModelRef | None = None) -> ProviderActivation | None:
        """Activation for an explicit reference or the current selection."""
        with self._lock:
            target = ref or self.current_ref()
            if target is None:
                return self._fallback
            return self.prepare(target)
