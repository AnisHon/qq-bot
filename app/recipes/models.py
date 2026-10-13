from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping


_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/@#-]{0,255}\Z")
_EXACT_ID_RE = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_RAW_RECORD_BYTES = 4 * 1024 * 1024


def _non_empty_key(value: str, label: str) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value) or value != value.strip():
        raise ValueError(f"{label} must be a non-empty stable identifier")
    return value


def validate_exact_identifier(kind: IngredientKind, value: str | None) -> None:
    if not isinstance(value, str) or not _EXACT_ID_RE.fullmatch(value):
        raise ValueError(f"{kind.value} requires an exact namespaced identifier")
    namespace, path = value.split(":", 1)
    if not namespace or not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"{kind.value} requires a canonical namespaced identifier")


def _decimal(value: Decimal | None, label: str, *, positive: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")


def _json_mapping(value: Mapping[str, Any] | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        # A JSON round-trip both validates the supported shape and prevents a
        # caller from mutating a nested dict/list after model construction.
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-compatible values") from exc
    return copied


class DatasetState(str, Enum):
    STAGING = "staging"
    ACTIVE = "active"
    RETIRED = "retired"
    QUARANTINED = "quarantined"


class DatasetAvailability(str, Enum):
    DATA_UNAVAILABLE = "data_unavailable"
    VERSION_MISMATCH = "version_mismatch"
    AVAILABLE = "available"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class DatasetStatus:
    key: PackVersionKey
    availability: DatasetAvailability
    dataset_id: str | None = None
    queryable_recipe_count: int = 0
    quarantined_recipe_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.key, PackVersionKey) or not isinstance(self.availability, DatasetAvailability):
            raise ValueError("dataset status requires a key and availability state")
        if self.queryable_recipe_count < 0 or self.quarantined_recipe_count < 0:
            raise ValueError("dataset recipe counts cannot be negative")


class IngredientKind(str, Enum):
    ITEM_EXACT = "item_exact"
    FLUID_EXACT = "fluid_exact"
    TAG = "tag"
    ORE_DICTIONARY = "ore_dictionary"
    PREDICATE = "predicate"
    SOURCE_EXTENSION = "source_extension"


class IngredientRole(str, Enum):
    MATERIAL = "material"
    TOOL = "tool"
    CATALYST = "catalyst"
    CONDITION = "condition"


class IngredientInterpretation(str, Enum):
    EXACT = "exact"
    CONDITIONAL = "conditional"
    UNSUPPORTED = "unsupported"


class RecipeStatus(str, Enum):
    ACCEPTED = "accepted"
    CONDITIONAL = "conditional"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class PackVersionKey:
    pack_id: str
    pack_version: str
    game_version: str

    def __post_init__(self) -> None:
        for name in ("pack_id", "pack_version", "game_version"):
            object.__setattr__(self, name, _non_empty_key(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    source_id: str
    source_revision: str
    exporter_id: str
    exporter_version: str | None
    license_id: str | None
    content_hash: str
    schema_version: int = 1
    importer_version: str = "contract-1"
    imported_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        for name in ("source_id", "source_revision", "exporter_id"):
            object.__setattr__(self, name, _non_empty_key(getattr(self, name), name))
        if self.exporter_version is not None:
            object.__setattr__(self, "exporter_version", _non_empty_key(self.exporter_version, "exporter_version"))
        if self.license_id is not None:
            object.__setattr__(self, "license_id", _non_empty_key(self.license_id, "license_id"))
        if self.importer_version:
            object.__setattr__(self, "importer_version", _non_empty_key(self.importer_version, "importer_version"))
        if not _SHA256_RE.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool) or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(self.imported_at, str) or not self.imported_at.strip():
            raise ValueError("imported_at must be a non-empty timestamp")


@dataclass(frozen=True, slots=True)
class RecipeDataset:
    dataset_id: str
    key: PackVersionKey
    source: SourceMetadata
    state: DatasetState = DatasetState.STAGING

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _non_empty_key(self.dataset_id, "dataset_id"))
        if not isinstance(self.key, PackVersionKey):
            raise ValueError("dataset key must be a PackVersionKey")
        if not isinstance(self.source, SourceMetadata):
            raise ValueError("dataset source metadata is required")
        if not isinstance(self.state, DatasetState):
            raise ValueError("dataset state is invalid")


@dataclass(frozen=True, slots=True)
class SourceLocator:
    uri: str
    line: int | None = None
    json_pointer: str | None = None
    record_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip() or "\x00" in self.uri:
            raise ValueError("source locator uri must be non-empty")
        if self.line is not None and (
            not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 1
        ):
            raise ValueError("source locator line must be a positive integer")
        for name in ("json_pointer", "record_key"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or "\x00" in value):
                raise ValueError(f"source locator {name} must be text")
        if self.line is None and self.json_pointer is None and self.record_key is None:
            raise ValueError("source locator needs a line, JSON pointer, or record key")


@dataclass(frozen=True, slots=True)
class RawRecipeRecord:
    dataset_id: str
    source_record_id: str
    source_locator: SourceLocator
    raw_payload: bytes
    parsed_fields: Mapping[str, Any] = field(default_factory=dict)
    unknown_fields: Mapping[str, Any] = field(default_factory=dict)
    critical_unknown_fields: tuple[str, ...] = ()
    raw_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _non_empty_key(self.dataset_id, "dataset_id"))
        object.__setattr__(self, "source_record_id", _non_empty_key(self.source_record_id, "source_record_id"))
        if not isinstance(self.source_locator, SourceLocator):
            raise ValueError("raw source locator is required")
        if not isinstance(self.raw_payload, bytes) or not self.raw_payload:
            raise ValueError("raw_payload must contain the exact source record bytes")
        if len(self.raw_payload) > MAX_RAW_RECORD_BYTES:
            raise ValueError("raw_payload exceeds the 4 MiB record limit")
        parsed = _json_mapping(self.parsed_fields, "parsed_fields")
        unknown = _json_mapping(self.unknown_fields, "unknown_fields")
        object.__setattr__(self, "parsed_fields", parsed or {})
        object.__setattr__(self, "unknown_fields", unknown or {})
        critical = tuple(self.critical_unknown_fields)
        if any(not isinstance(item, str) or not item for item in critical):
            raise ValueError("critical_unknown_fields must be non-empty field names")
        object.__setattr__(self, "critical_unknown_fields", tuple(dict.fromkeys(critical)))
        computed_hash = hashlib.sha256(self.raw_payload).hexdigest()
        if self.raw_hash is not None and self.raw_hash != computed_hash:
            raise ValueError("raw_hash does not match exact raw_payload bytes")
        object.__setattr__(self, "raw_hash", computed_hash)


@dataclass(frozen=True, slots=True)
class Ingredient:
    kind: IngredientKind
    value: str | None
    amount: Decimal | None
    unit: str | None
    role: IngredientRole = IngredientRole.MATERIAL
    consumed: bool | None = None
    constraints: Mapping[str, Any] | None = None
    interpretation: IngredientInterpretation = IngredientInterpretation.EXACT
    source_locator: SourceLocator | None = None
    source_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IngredientKind) or not isinstance(self.role, IngredientRole):
            raise ValueError("ingredient kind and role must use the declared enums")
        if not isinstance(self.interpretation, IngredientInterpretation):
            raise ValueError("ingredient interpretation is invalid")
        _decimal(self.amount, "ingredient amount", positive=True)
        if self.unit is not None and (not isinstance(self.unit, str) or not self.unit.strip()):
            raise ValueError("ingredient unit must be non-empty when supplied")
        if self.consumed is not None and not isinstance(self.consumed, bool):
            raise ValueError("ingredient consumed must be true, false, or unknown")
        if self.role == IngredientRole.TOOL and self.consumed is not False:
            raise ValueError("tool ingredients must explicitly be non-consumable")
        if self.kind in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT}:
            validate_exact_identifier(self.kind, self.value)
        elif self.value is not None and (not isinstance(self.value, str) or not self.value.strip()):
            raise ValueError("ingredient value must be non-empty when supplied")
        if self.kind == IngredientKind.SOURCE_EXTENSION and self.interpretation != IngredientInterpretation.UNSUPPORTED:
            raise ValueError("unrecognized source extensions must remain unsupported")
        if self.kind not in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT} and self.interpretation == IngredientInterpretation.EXACT:
            raise ValueError("conditional ingredient kinds cannot be marked exact")
        constraints = _json_mapping(self.constraints, "ingredient constraints")
        source_fields = _json_mapping(self.source_fields, "ingredient source fields") or {}
        if constraints and self.interpretation == IngredientInterpretation.EXACT:
            raise ValueError("constrained ingredients cannot be marked exact without a resolver")
        if self.consumed is None and self.interpretation == IngredientInterpretation.EXACT:
            raise ValueError("unknown consumption semantics cannot be marked exact")
        if self.amount is not None and self.unit is None and self.interpretation != IngredientInterpretation.UNSUPPORTED:
            raise ValueError("an amount with an unknown unit must remain unsupported")
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "source_fields", source_fields)


@dataclass(frozen=True, slots=True)
class RecipeOutput:
    kind: IngredientKind
    value: str | None
    amount: Decimal | None
    unit: str | None
    probability: Decimal = Decimal("1")
    constraints: Mapping[str, Any] | None = None
    interpretation: IngredientInterpretation = IngredientInterpretation.EXACT
    source_locator: SourceLocator | None = None
    source_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IngredientKind) or not isinstance(self.interpretation, IngredientInterpretation):
            raise ValueError("output kind and interpretation must use the declared enums")
        _decimal(self.amount, "output amount", positive=True)
        _decimal(self.probability, "output probability")
        if self.probability < 0 or self.probability > 1:
            raise ValueError("output probability must be between zero and one")
        if self.unit is not None and (not isinstance(self.unit, str) or not self.unit.strip()):
            raise ValueError("output unit must be non-empty when supplied")
        if self.kind in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT}:
            validate_exact_identifier(self.kind, self.value)
        elif self.value is not None and (not isinstance(self.value, str) or not self.value.strip()):
            raise ValueError("output value must be non-empty when supplied")
        if self.kind == IngredientKind.SOURCE_EXTENSION and self.interpretation != IngredientInterpretation.UNSUPPORTED:
            raise ValueError("unrecognized source extensions must remain unsupported")
        if self.kind not in {IngredientKind.ITEM_EXACT, IngredientKind.FLUID_EXACT} and self.interpretation == IngredientInterpretation.EXACT:
            raise ValueError("conditional output kinds cannot be marked exact")
        constraints = _json_mapping(self.constraints, "output constraints")
        source_fields = _json_mapping(self.source_fields, "output source fields") or {}
        if constraints and self.interpretation == IngredientInterpretation.EXACT:
            raise ValueError("constrained outputs cannot be marked exact without a resolver")
        if self.amount is not None and self.unit is None and self.interpretation != IngredientInterpretation.UNSUPPORTED:
            raise ValueError("an output amount with an unknown unit must remain unsupported")
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "source_fields", source_fields)


@dataclass(frozen=True, slots=True)
class Recipe:
    dataset_id: str
    recipe_id: str
    source_record_id: str
    raw_source_hash: str
    machine_ids: tuple[str, ...]
    inputs: tuple[Ingredient, ...]
    outputs: tuple[RecipeOutput, ...]
    energy_eut: int | None = None
    duration_ticks: int | None = None
    conditions: Mapping[str, Any] = field(default_factory=dict)
    critical_unknown_fields: tuple[str, ...] = ()
    status: RecipeStatus | None = None
    quarantine_reasons: tuple[str, ...] = ()
    source_locator: SourceLocator | None = None

    def __post_init__(self) -> None:
        for name in ("dataset_id", "recipe_id", "source_record_id"):
            object.__setattr__(self, name, _non_empty_key(getattr(self, name), name))
        if not _SHA256_RE.fullmatch(self.raw_source_hash):
            raise ValueError("raw_source_hash must be a lowercase SHA-256 hex digest")
        machine_ids = tuple(_non_empty_key(value, "machine_id") for value in self.machine_ids)
        if len(machine_ids) != len(set(machine_ids)):
            raise ValueError("machine_ids must not contain duplicates")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        if any(not isinstance(item, Ingredient) for item in inputs):
            raise ValueError("recipe inputs must be Ingredient values")
        if any(not isinstance(item, RecipeOutput) for item in outputs):
            raise ValueError("recipe outputs must be RecipeOutput values")
        if not outputs:
            raise ValueError("recipe must define at least one output")
        for name, value in (("energy_eut", self.energy_eut), ("duration_ticks", self.duration_ticks)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or unknown")
        conditions = _json_mapping(self.conditions, "recipe conditions") or {}
        critical_fields = tuple(dict.fromkeys(self.critical_unknown_fields))
        if any(not isinstance(item, str) or not item for item in critical_fields):
            raise ValueError("critical_unknown_fields must be non-empty field names")

        reasons = list(self.quarantine_reasons)
        reasons.extend(f"unknown critical field: {field_name}" for field_name in critical_fields)
        for index, ingredient in enumerate(inputs):
            if ingredient.interpretation == IngredientInterpretation.UNSUPPORTED:
                reasons.append(f"unsupported input condition at index {index}")
            if ingredient.amount is not None and ingredient.unit is None:
                reasons.append(f"input amount unit unknown at index {index}")
        for index, output in enumerate(outputs):
            if output.interpretation == IngredientInterpretation.UNSUPPORTED:
                reasons.append(f"unsupported output condition at index {index}")
            if output.amount is not None and output.unit is None:
                reasons.append(f"output amount unit unknown at index {index}")
        reasons = list(dict.fromkeys(reasons))
        inferred = (
            RecipeStatus.QUARANTINED
            if reasons
            else RecipeStatus.CONDITIONAL
            if any(
                item.interpretation == IngredientInterpretation.CONDITIONAL or item.consumed is None
                for item in inputs
            )
            or any(item.interpretation == IngredientInterpretation.CONDITIONAL for item in outputs)
            or conditions
            else RecipeStatus.ACCEPTED
        )
        if self.status is not None and not isinstance(self.status, RecipeStatus):
            raise ValueError("recipe status is invalid")
        if self.status is not None and self.status != inferred:
            raise ValueError("recipe status does not match its understood conditions")
        object.__setattr__(self, "machine_ids", machine_ids)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(self, "critical_unknown_fields", critical_fields)
        object.__setattr__(self, "quarantine_reasons", tuple(reasons))
        object.__setattr__(self, "status", inferred)
