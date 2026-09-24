"""Operator configuration for Kubernetes executor pods: placement, resources and volumes."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from kubernetes.utils import parse_quantity  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.services.executor_base import SESSION_EXPIRES_AT_KEY

CONTROLLER_LABEL_KEYS: Final[frozenset[str]] = frozenset({"app", "component"})
CONTROLLER_ANNOTATION_KEYS: Final[frozenset[str]] = frozenset({SESSION_EXPIRES_AT_KEY})

DEFAULT_RESOURCE_REQUESTS: Final[dict[str, str]] = {"cpu": "100m", "memory": "64Mi"}
DEFAULT_RESOURCE_LIMITS: Final[dict[str, str]] = {"cpu": "1"}
DEFAULT_WORKSPACE_SIZE_LIMIT: Final[str] = "100Mi"
DEFAULT_TMP_SIZE_LIMIT: Final[str] = "64Mi"

_DNS_LABEL: Final[str] = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
_DNS_SUBDOMAIN_RE: Final[re.Pattern[str]] = re.compile(rf"^{_DNS_LABEL}(\.{_DNS_LABEL})*$")
_NAME: Final[str] = r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?"
_NAME_RE: Final[re.Pattern[str]] = re.compile(rf"^{_NAME}$")
_LABEL_VALUE_RE: Final[re.Pattern[str]] = re.compile(rf"^({_NAME})?$")


def _is_dns_subdomain(value: str) -> bool:
    return len(value) <= 253 and _DNS_SUBDOMAIN_RE.match(value) is not None


def _check_qualified_name(key: str) -> None:
    prefix, _, name = key.rpartition("/")
    if "/" in key and not _is_dns_subdomain(prefix):
        raise ValueError(f"key {key!r} has an invalid prefix")
    if not name or len(name) > 63 or _NAME_RE.match(name) is None:
        raise ValueError(f"key {key!r} is not a valid Kubernetes label or annotation key")


def _quantity(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"resource quantity must be a string or number, got {value!r}")
    text = str(value).strip()
    try:
        amount = parse_quantity(text)
    except ValueError as e:
        raise ValueError(f"{value!r} is not a valid Kubernetes quantity") from e
    if amount <= 0:
        raise ValueError(f"resource quantity must be positive, got {value!r}")
    return text


def _empty_to_none(value: object) -> object:
    return None if value == "" else value


Quantity = Annotated[str, BeforeValidator(_quantity)]
ObjectName = Annotated[str | None, BeforeValidator(_empty_to_none)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Toleration(_Strict):
    key: str | None = None
    operator: Literal["Exists", "Equal"] | None = None
    value: str | None = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = None
    toleration_seconds: int | None = Field(default=None, alias="tolerationSeconds")


class Affinity(_Strict):
    node_affinity: dict[str, Any] | None = Field(default=None, alias="nodeAffinity")
    pod_affinity: dict[str, Any] | None = Field(default=None, alias="podAffinity")
    pod_anti_affinity: dict[str, Any] | None = Field(default=None, alias="podAntiAffinity")


class TopologySpreadConstraint(_Strict):
    max_skew: int = Field(alias="maxSkew", ge=1)
    topology_key: str = Field(alias="topologyKey", min_length=1)
    when_unsatisfiable: Literal["DoNotSchedule", "ScheduleAnyway"] = Field(
        alias="whenUnsatisfiable"
    )
    label_selector: dict[str, Any] | None = Field(default=None, alias="labelSelector")
    min_domains: int | None = Field(default=None, alias="minDomains", ge=1)
    match_label_keys: list[str] | None = Field(default=None, alias="matchLabelKeys")
    node_affinity_policy: Literal["Honor", "Ignore"] | None = Field(
        default=None, alias="nodeAffinityPolicy"
    )
    node_taints_policy: Literal["Honor", "Ignore"] | None = Field(
        default=None, alias="nodeTaintsPolicy"
    )


class ExecutorPodOverrides(_Strict):
    """Scheduling fields and extra metadata applied to every executor pod."""

    node_selector: dict[str, str] = Field(default_factory=dict, alias="nodeSelector")
    tolerations: list[Toleration] = Field(default_factory=list)
    affinity: Affinity | None = None
    topology_spread_constraints: list[TopologySpreadConstraint] = Field(
        default_factory=list, alias="topologySpreadConstraints"
    )
    priority_class_name: ObjectName = Field(default=None, alias="priorityClassName")
    runtime_class_name: ObjectName = Field(default=None, alias="runtimeClassName")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

    @field_validator("node_selector", "labels")
    @classmethod
    def _check_labels(cls, value: dict[str, str]) -> dict[str, str]:
        for key, label_value in value.items():
            _check_qualified_name(key)
            if len(label_value) > 63 or _LABEL_VALUE_RE.match(label_value) is None:
                raise ValueError(f"label value {label_value!r} for {key!r} is not valid")
        return value

    @field_validator("labels")
    @classmethod
    def _check_label_ownership(cls, value: dict[str, str]) -> dict[str, str]:
        owned = sorted(CONTROLLER_LABEL_KEYS & value.keys())
        if owned:
            raise ValueError(f"labels {owned} are set by the controller and cannot be overridden")
        return value

    @field_validator("annotations")
    @classmethod
    def _check_annotations(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            _check_qualified_name(key)
        owned = sorted(CONTROLLER_ANNOTATION_KEYS & value.keys())
        if owned:
            raise ValueError(
                f"annotations {owned} are set by the controller and cannot be overridden"
            )
        return value

    @field_validator("priority_class_name", "runtime_class_name")
    @classmethod
    def _check_object_name(cls, value: str | None) -> str | None:
        if value is not None and not _is_dns_subdomain(value):
            raise ValueError(f"{value!r} is not a valid Kubernetes object name")
        return value


class ExecutorPodResources(_Strict):
    """CPU, memory and ephemeral-storage for the executor container.

    The memory limit is not set here: it comes from MEMORY_LIMIT_MB, which the
    Docker backend also uses. The memory request is capped at that limit.
    """

    requests: dict[Literal["cpu", "memory", "ephemeral-storage"], Quantity | None] = Field(
        default_factory=dict
    )
    limits: dict[Literal["cpu", "ephemeral-storage"], Quantity | None] = Field(default_factory=dict)

    @field_validator("limits", mode="before")
    @classmethod
    def _reject_memory_limit(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("memory") is not None:
            raise ValueError(
                "limits.memory is not supported; the memory limit comes from MEMORY_LIMIT_MB "
                "(chart: codeInterpreter.memoryLimitMb)"
            )
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k != "memory"}
        return value

    @model_validator(mode="after")
    def _check_requests_within_limits(self) -> ExecutorPodResources:
        for name, limit in self.limits.items():
            request = self.requests.get(name)
            if limit is None or request is None:
                continue
            if _parse(request) > _parse(limit):
                raise ValueError(f"requests.{name} ({request}) exceeds limits.{name} ({limit})")
        return self


def _parse(quantity: str) -> Decimal:
    value: Decimal = parse_quantity(quantity)
    return value


def quantity_to_mebibytes(quantity: str) -> Decimal:
    return _parse(quantity) / (1024 * 1024)


def _error_text(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in error.errors()
    )


def _load_json(name: str, raw: str) -> Any:  # noqa: ANN401
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{name} is not valid JSON: {e}") from e


def parse_pod_overrides(name: str, raw: str | None) -> ExecutorPodOverrides:
    """Parse the JSON pod overrides in env var ``name``. Unset or empty means none."""
    if raw is None or not raw.strip():
        return ExecutorPodOverrides()
    try:
        return ExecutorPodOverrides.model_validate(_load_json(name, raw))
    except ValidationError as e:
        raise ValueError(f"{name} is invalid: {_error_text(e)}") from e


def parse_pod_resources(name: str, raw: str | None) -> ExecutorPodResources:
    """Parse the JSON resources in env var ``name``. Unset or empty means the defaults."""
    if raw is None or not raw.strip():
        return ExecutorPodResources.model_validate(
            {"requests": DEFAULT_RESOURCE_REQUESTS, "limits": DEFAULT_RESOURCE_LIMITS}
        )
    try:
        return ExecutorPodResources.model_validate(_load_json(name, raw))
    except ValidationError as e:
        raise ValueError(f"{name} is invalid: {_error_text(e)}") from e


def parse_size_limit(name: str, raw: str | None, default: str) -> str:
    """Parse an emptyDir size limit in env var ``name``."""
    if raw is None or not raw.strip():
        return default
    try:
        return _quantity(raw)
    except ValueError as e:
        raise ValueError(f"{name} is invalid: {e}") from e
