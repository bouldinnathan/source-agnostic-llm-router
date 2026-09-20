"""Client-visible model replicas and machine-preference aliases.

Replica membership is based on the upstream name with punctuation and case
folded away, unless an operator sets ``replica_group``. Ollama's ``qwen3:14b``
and LM Studio's ``qwen3-14b`` therefore share one HA name. A machine-name
collision never silently changes which machine a name prefers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Literal

from .schema import ModelConfig, QueryRequest, RouterConfig


@dataclass(frozen=True, slots=True)
class ModelAlias:
    name: str
    strategy: str
    deployment_ids: tuple[str, ...]
    preferred_endpoints: tuple[str, ...]
    models: tuple[ModelConfig, ...]
    kind: Literal["ha", "preferred", "pinned"] = "ha"

    def apply(self, query: QueryRequest) -> QueryRequest:
        """Restrict a request to this alias without weakening caller filters."""

        deployment_ids = self.deployment_ids
        if query.allowed_deployments is not None:
            allowed = set(query.allowed_deployments)
            deployment_ids = tuple(item for item in deployment_ids if item in allowed)
        return replace(
            query,
            strategy=query.strategy or self.strategy,
            allowed_deployments=deployment_ids,
            preferred_endpoints=self.preferred_endpoints,
        )


def build_aliases(config: RouterConfig) -> dict[str, ModelAlias]:
    """Expose HA, preferred-machine, and strict-machine aliases for each group.

    Ambiguous names are omitted, leaving the other aliases usable. Names depend
    only on their group and machine identities, so adding an unrelated machine
    does not rename existing aliases. See :func:`alias_conflicts` for omitted
    names that need an explicit, distinct ``replica_group`` or ``machine_id``.
    """

    return _catalog(config)[0]


def alias_conflicts(config: RouterConfig) -> tuple[str, ...]:
    """Return generated alias names omitted because they are ambiguous."""

    return _catalog(config)[1]


def _catalog(config: RouterConfig) -> tuple[dict[str, ModelAlias], tuple[str, ...]]:
    groups: dict[str, list[ModelConfig]] = {}
    for model in config.models:
        if model.enabled:
            groups.setdefault(replica_group_key(model), []).append(model)

    aliases: dict[str, ModelAlias] = {}
    conflicts: set[str] = set()

    def add(
        name: str,
        models: tuple[ModelConfig, ...],
        preferred_endpoints: tuple[str, ...] = (),
        *,
        kind: Literal["ha", "preferred", "pinned"] = "ha",
    ) -> None:
        # Keep gateway presets reserved without importing the HTTP gateway.
        if name == "auto" or name.startswith("auto:") or name in aliases:
            aliases.pop(name, None)
            conflicts.add(name)
        elif name not in conflicts:
            aliases[name] = ModelAlias(
                name=name,
                strategy="latency",
                deployment_ids=tuple(model.id for model in models),
                preferred_endpoints=preferred_endpoints,
                models=models,
                kind=kind,
            )

    for group_name, members in sorted(groups.items()):
        models = tuple(sorted(members, key=lambda model: model.id))
        add(f"{group_name}-ha", models)

        machines: dict[str, list[ModelConfig]] = {}
        for model in models:
            endpoint = config.endpoints[model.endpoint]
            machine = endpoint.machine_id or endpoint.name
            machines.setdefault(machine, []).append(model)
        for machine, machine_members in sorted(machines.items()):
            name = f"{group_name}-{_slug(machine, fallback='machine')}"
            machine_models = tuple(machine_members)
            preferred_endpoints = tuple(sorted({model.endpoint for model in machine_models}))
            add(name, models, preferred_endpoints, kind="preferred")
            add(f"{name}-nofailover", machine_models, preferred_endpoints, kind="pinned")

    return dict(sorted(aliases.items())), tuple(sorted(conflicts))


def replica_group_key(model: ModelConfig) -> str:
    """The replica group a model belongs to: its declared group or its own name.

    Names that differ only in punctuation or case, such as Ollama's ``qwen3:14b``
    and LM Studio's ``qwen3-14b``, are the same model published two ways, so
    they form one group. Quantization and variant tags are part of the name and
    keep their own groups.
    """
    return _slug(model.replica_group or model.upstream_model, fallback="model")


def _slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if slug:
        return slug
    # Even unusual model identifiers receive deterministic, usable names.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{fallback}-{digest}"
