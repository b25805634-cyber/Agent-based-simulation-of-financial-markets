"""Pure-Python distillation utilities for the isolated V2 market prototype.

The module deliberately has no provider, simulation, or file-system side
effects.  It consumes *aggregated* teacher observations with this contract::

    {
        "family_id": str,
        "state_id": str,
        "features": [float, ...],
        "target_probs": [p_buy, p_hold, p_sell],
        "intensity_targets": [i_buy, i_sell],
        "intensity_weights": [w_buy, w_sell],
        # optional grouping aid:
        "stratum": str,
    }

``intensity_weights`` make the intensity loss and metric conditional on the
teacher actually assigning mass to the corresponding buy/sell action.  The
fixed action orders below are part of the serialized model contract.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import random
from typing import Any, Optional


ACTIONS = ("buy", "hold", "sell")
INTENSITY_ACTIONS = ("buy", "sell")
OBSERVATION_HASH_SCHEMA_VERSION = "v2-aggregated-observation-v1"
SPLIT_SCHEMA_VERSION = "v2-family-group-split-v1"
STANDARDIZER_SCHEMA_VERSION = "v2-standardizer-v1"
ACTION_PRIOR_SCHEMA_VERSION = "v2-action-prior-v1"
LINEAR_SCHEMA_VERSION = "v2-linear-softmax-v1"
MLP_SCHEMA_VERSION = "v2-tanh-mlp-v1"
OOD_REFERENCE_SCHEMA_VERSION = "v2-ood-reference-v1"
OOD_DIAGNOSTIC_SCHEMA_VERSION = "v2-ood-diagnostic-v1"
OOD_Z_THRESHOLD = 3.0


class DistillationContractError(ValueError):
    """An observation, split, or serialized model violates the V2 contract."""


def ood_diagnostic_descriptor() -> dict[str, Any]:
    """Return the frozen, machine-readable V2 OOD diagnostic semantics."""

    return {
        "schema_version": OOD_DIAGNOSTIC_SCHEMA_VERSION,
        "reference_partition": "student_train_rows_only",
        "support_geometry": "per_feature_train_min_max_rectangle",
        "standardization": "student_train_only_mean_and_population_scale",
        "standardized_tail_rule": "absolute_z_strictly_greater_than_threshold",
        "z_threshold": OOD_Z_THRESHOLD,
        "evaluation_scopes": [
            "validation_rows",
            "frozen_test_rows",
            "effective_clamped_pre_decision_market_states",
        ],
        "joint_support_assessed": False,
        "density_or_distribution_shift_assessed": False,
        "diagnostic_only": True,
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _finite_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DistillationContractError(f"{field} must contain real numbers")
    number = float(value)
    if not math.isfinite(number):
        raise DistillationContractError(f"{field} must contain only finite numbers")
    # Canonicalize negative zero so semantically identical rows hash equally.
    return 0.0 if number == 0.0 else number


def _numeric_vector(
    value: Any,
    *,
    field: str,
    length: int | None = None,
) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DistillationContractError(f"{field} must be a numeric sequence")
    result = [_finite_float(item, field=field) for item in value]
    if length is not None and len(result) != length:
        raise DistillationContractError(f"{field} must have length {length}")
    if length is None and not result:
        raise DistillationContractError(f"{field} must not be empty")
    return result


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DistillationContractError(f"{field} must be a non-empty string")
    return value


def normalize_observation(
    observation: Mapping[str, Any],
    *,
    expected_feature_dim: int | None = None,
    stratum_key: str | None = "stratum",
) -> dict[str, Any]:
    """Validate and copy one aggregated observation into canonical form."""

    if not isinstance(observation, Mapping):
        raise DistillationContractError("each observation must be a mapping")
    family_id = _nonempty_string(observation.get("family_id"), field="family_id")
    state_id = _nonempty_string(observation.get("state_id"), field="state_id")
    features = _numeric_vector(observation.get("features"), field="features")
    if expected_feature_dim is not None and len(features) != expected_feature_dim:
        raise DistillationContractError(
            f"features has length {len(features)}; expected {expected_feature_dim}"
        )

    target_probs = _numeric_vector(
        observation.get("target_probs"), field="target_probs", length=len(ACTIONS)
    )
    if any(item < 0.0 or item > 1.0 for item in target_probs):
        raise DistillationContractError("target_probs entries must lie in [0, 1]")
    probability_sum = sum(target_probs)
    if not math.isclose(probability_sum, 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise DistillationContractError("target_probs must sum to one")
    # Remove harmless floating summation drift while preserving soft labels.
    target_probs = [item / probability_sum for item in target_probs]

    intensity_targets = _numeric_vector(
        observation.get("intensity_targets"),
        field="intensity_targets",
        length=len(INTENSITY_ACTIONS),
    )
    if any(item < 0.0 or item > 1.0 for item in intensity_targets):
        raise DistillationContractError("intensity_targets entries must lie in [0, 1]")
    intensity_weights = _numeric_vector(
        observation.get("intensity_weights"),
        field="intensity_weights",
        length=len(INTENSITY_ACTIONS),
    )
    if any(item < 0.0 for item in intensity_weights):
        raise DistillationContractError("intensity_weights entries must be non-negative")

    normalized: dict[str, Any] = {
        "family_id": family_id,
        "state_id": state_id,
        "features": features,
        "target_probs": target_probs,
        "intensity_targets": intensity_targets,
        "intensity_weights": intensity_weights,
    }
    if stratum_key is not None and stratum_key in observation:
        normalized[stratum_key] = _nonempty_string(
            observation[stratum_key], field=stratum_key
        )
    return normalized


def normalize_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    stratum_key: str | None = "stratum",
) -> list[dict[str, Any]]:
    """Validate a non-empty observation collection and enforce one feature width."""

    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise DistillationContractError("observations must be a sequence")
    if not observations:
        raise DistillationContractError("observations must not be empty")
    normalized: list[dict[str, Any]] = []
    feature_dim: int | None = None
    identities: set[tuple[str, str]] = set()
    for observation in observations:
        row = normalize_observation(
            observation,
            expected_feature_dim=feature_dim,
            stratum_key=stratum_key,
        )
        if feature_dim is None:
            feature_dim = len(row["features"])
        identity = (row["family_id"], row["state_id"])
        if identity in identities:
            raise DistillationContractError(
                f"duplicate aggregated observation identity: {identity!r}"
            )
        identities.add(identity)
        normalized.append(row)
    return normalized


def _observation_sort_key(observation: Mapping[str, Any]) -> tuple[str, str, bytes]:
    return (
        str(observation["family_id"]),
        str(observation["state_id"]),
        _canonical_json(observation),
    )


def canonical_observations_hash(
    observations: Sequence[Mapping[str, Any]],
    *,
    stratum_key: str | None = "stratum",
) -> str:
    """Hash scientific observation content independently of input row order."""

    rows = sorted(
        normalize_observations(observations, stratum_key=stratum_key),
        key=_observation_sort_key,
    )
    payload = {
        "schema_version": OBSERVATION_HASH_SCHEMA_VERSION,
        "action_order": list(ACTIONS),
        "intensity_action_order": list(INTENSITY_ACTIONS),
        "observations": rows,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class DatasetSplit:
    """A family-grouped split whose row order is canonical."""

    train: tuple[dict[str, Any], ...]
    validation: tuple[dict[str, Any], ...]
    test: tuple[dict[str, Any], ...]
    family_assignments: dict[str, str]
    family_strata: dict[str, str]
    seed: int
    fractions: tuple[float, float, float]
    stratum_key: str | None

    def all_rows(self) -> tuple[dict[str, Any], ...]:
        return self.train + self.validation + self.test

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SPLIT_SCHEMA_VERSION,
            "seed": self.seed,
            "fractions": list(self.fractions),
            "stratum_key": self.stratum_key,
            "family_assignments": dict(sorted(self.family_assignments.items())),
            "family_strata": dict(sorted(self.family_strata.items())),
            "counts": {
                "train_rows": len(self.train),
                "validation_rows": len(self.validation),
                "test_rows": len(self.test),
                "train_families": sum(
                    value == "train" for value in self.family_assignments.values()
                ),
                "validation_families": sum(
                    value == "validation" for value in self.family_assignments.values()
                ),
                "test_families": sum(
                    value == "test" for value in self.family_assignments.values()
                ),
            },
        }


def _split_counts(n_groups: int, fractions: tuple[float, float, float]) -> list[int]:
    targets = [n_groups * fraction for fraction in fractions]
    counts = [math.floor(target) for target in targets]
    remaining = n_groups - sum(counts)
    priority = sorted(
        range(3),
        key=lambda index: (-(targets[index] - counts[index]), index),
    )
    for index in priority[:remaining]:
        counts[index] += 1
    return counts


def deterministic_group_split(
    observations: Sequence[Mapping[str, Any]],
    *,
    seed: int = 0,
    fractions: tuple[float, float, float] = (0.70, 0.15, 0.15),
    stratum_key: str | None = None,
) -> DatasetSplit:
    """Split by ``family_id`` with optional deterministic stratification.

    Families are first ordered by a SHA-256 rank derived from the seed,
    stratum, and family id.  Within each stratum, largest-remainder integer
    allocation approximates the requested 70/15/15 fractions.  Consequently
    row order cannot change an assignment and a family can never leak across
    splits.  Very small strata can naturally have an empty validation or test
    subset; callers should design confirmatory strata with adequate support.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DistillationContractError("seed must be an integer")
    if len(fractions) != 3:
        raise DistillationContractError("fractions must contain train/validation/test")
    fraction_values = tuple(
        _finite_float(item, field="fractions") for item in fractions
    )
    if any(item < 0.0 for item in fraction_values) or not math.isclose(
        sum(fraction_values), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise DistillationContractError("fractions must be non-negative and sum to one")

    rows = normalize_observations(observations, stratum_key=stratum_key)
    family_rows: dict[str, list[dict[str, Any]]] = {}
    family_strata: dict[str, str] = {}
    default_stratum = "__all__"
    for row in rows:
        family_id = row["family_id"]
        family_rows.setdefault(family_id, []).append(row)
        stratum = default_stratum if stratum_key is None else row.get(stratum_key, "__missing__")
        prior = family_strata.setdefault(family_id, stratum)
        if prior != stratum:
            raise DistillationContractError(
                f"family_id {family_id!r} appears in multiple strata"
            )

    strata: dict[str, list[str]] = {}
    for family_id, stratum in family_strata.items():
        strata.setdefault(stratum, []).append(family_id)

    assignments: dict[str, str] = {}
    split_names = ("train", "validation", "test")
    for stratum in sorted(strata):
        ranked = sorted(
            strata[stratum],
            key=lambda family_id: (
                hashlib.sha256(
                    _canonical_json(
                        {
                            "schema_version": SPLIT_SCHEMA_VERSION,
                            "seed": seed,
                            "stratum": stratum,
                            "family_id": family_id,
                        }
                    )
                ).digest(),
                family_id,
            ),
        )
        counts = _split_counts(len(ranked), fraction_values)
        offset = 0
        for split_name, count in zip(split_names, counts):
            for family_id in ranked[offset : offset + count]:
                assignments[family_id] = split_name
            offset += count

    split_rows: dict[str, list[dict[str, Any]]] = {
        "train": [],
        "validation": [],
        "test": [],
    }
    for family_id, family_observations in family_rows.items():
        split_rows[assignments[family_id]].extend(family_observations)
    for value in split_rows.values():
        value.sort(key=_observation_sort_key)

    return DatasetSplit(
        train=tuple(split_rows["train"]),
        validation=tuple(split_rows["validation"]),
        test=tuple(split_rows["test"]),
        family_assignments=dict(sorted(assignments.items())),
        family_strata=dict(sorted(family_strata.items())),
        seed=seed,
        fractions=fraction_values,
        stratum_key=stratum_key,
    )


def apply_frozen_family_assignments(
    observations: Sequence[Mapping[str, Any]],
    *,
    family_assignments: Mapping[str, str],
    family_strata: Optional[Mapping[str, str]] = None,
    seed: int,
    fractions: tuple[float, float, float] = (0.70, 0.15, 0.15),
    stratum_key: str | None = None,
) -> DatasetSplit:
    """Project eligible rows through a pre-outcome family partition.

    ``family_assignments`` may contain families with no eligible row.  This is
    intentional: an all-failed Teacher state keeps its pre-Teacher partition
    identity and dataset accounting, while contributing no Student row.  A row
    from an unassigned family, an unknown partition name, or a family that
    changes stratum fails closed.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DistillationContractError("seed must be an integer")
    if not isinstance(family_assignments, Mapping) or not family_assignments:
        raise DistillationContractError("frozen family assignments must be non-empty")
    assignments: dict[str, str] = {}
    for raw_family, raw_partition in family_assignments.items():
        if not isinstance(raw_family, str) or not raw_family:
            raise DistillationContractError("frozen family ids must be non-empty strings")
        if raw_partition not in {"train", "validation", "test"}:
            raise DistillationContractError(
                "frozen family assignment must be train, validation, or test"
            )
        assignments[raw_family] = str(raw_partition)

    if len(fractions) != 3:
        raise DistillationContractError("fractions must contain train/validation/test")
    fraction_values = tuple(
        _finite_float(item, field="fractions") for item in fractions
    )
    if any(item < 0.0 for item in fraction_values) or not math.isclose(
        sum(fraction_values), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise DistillationContractError("fractions must be non-negative and sum to one")

    rows = normalize_observations(observations, stratum_key=stratum_key)
    if stratum_key is not None:
        if not isinstance(family_strata, Mapping):
            raise DistillationContractError(
                "frozen family strata are required for a stratified split"
            )
        frozen_strata = {str(key): str(value) for key, value in family_strata.items()}
        if set(frozen_strata) != set(assignments):
            raise DistillationContractError(
                "frozen family strata must cover exactly the assigned families"
            )
    else:
        frozen_strata = {}
    split_rows: dict[str, list[dict[str, Any]]] = {
        "train": [],
        "validation": [],
        "test": [],
    }
    for row in rows:
        family_id = row["family_id"]
        try:
            partition = assignments[family_id]
        except KeyError as exc:
            raise DistillationContractError(
                "eligible row belongs to a family absent from the frozen split"
            ) from exc
        if stratum_key is not None:
            stratum = row.get(stratum_key, "__missing__")
            if frozen_strata[family_id] != stratum:
                raise DistillationContractError(
                    f"family_id {family_id!r} changed its frozen stratum"
                )
        split_rows[partition].append(row)
    for value in split_rows.values():
        value.sort(key=_observation_sort_key)

    return DatasetSplit(
        train=tuple(split_rows["train"]),
        validation=tuple(split_rows["validation"]),
        test=tuple(split_rows["test"]),
        family_assignments=dict(sorted(assignments.items())),
        family_strata=dict(sorted(frozen_strata.items())),
        seed=seed,
        fractions=fraction_values,
        stratum_key=stratum_key,
    )


def _feature_rows(
    examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]],
) -> list[list[float]]:
    if isinstance(examples_or_features, (str, bytes)) or not isinstance(
        examples_or_features, Sequence
    ):
        raise DistillationContractError("feature rows must be a sequence")
    if not examples_or_features:
        raise DistillationContractError("feature rows must not be empty")
    result: list[list[float]] = []
    expected_dim: int | None = None
    for index, value in enumerate(examples_or_features):
        features = value.get("features") if isinstance(value, Mapping) else value
        row = _numeric_vector(features, field=f"features[{index}]")
        if expected_dim is None:
            expected_dim = len(row)
        elif len(row) != expected_dim:
            raise DistillationContractError("all feature rows must have equal length")
        result.append(row)
    return result


@dataclass(frozen=True)
class Standardizer:
    """Per-feature train-set mean and population standard deviation."""

    means: tuple[float, ...]
    scales: tuple[float, ...]

    @classmethod
    def fit(
        cls,
        train_examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]],
    ) -> "Standardizer":
        rows = _feature_rows(train_examples_or_features)
        n_rows = len(rows)
        n_features = len(rows[0])
        means = [sum(row[index] for row in rows) / n_rows for index in range(n_features)]
        variances = [
            sum((row[index] - means[index]) ** 2 for row in rows) / n_rows
            for index in range(n_features)
        ]
        scales = [math.sqrt(max(variance, 0.0)) for variance in variances]
        scales = [scale if scale > 1e-12 else 1.0 for scale in scales]
        return cls(tuple(means), tuple(scales))

    @property
    def feature_dim(self) -> int:
        return len(self.means)

    def transform(self, features: Sequence[float]) -> list[float]:
        row = _numeric_vector(features, field="features", length=self.feature_dim)
        return [
            (value - mean) / scale
            for value, mean, scale in zip(row, self.means, self.scales)
        ]

    def transform_many(
        self, examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]]
    ) -> list[list[float]]:
        return [self.transform(row) for row in _feature_rows(examples_or_features)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STANDARDIZER_SCHEMA_VERSION,
            "means": list(self.means),
            "scales": list(self.scales),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Standardizer":
        if not isinstance(payload, Mapping) or payload.get("schema_version") != STANDARDIZER_SCHEMA_VERSION:
            raise DistillationContractError("unsupported standardizer schema")
        means = _numeric_vector(payload.get("means"), field="means")
        scales = _numeric_vector(
            payload.get("scales"), field="scales", length=len(means)
        )
        if any(scale <= 0.0 for scale in scales):
            raise DistillationContractError("standardizer scales must be positive")
        return cls(tuple(means), tuple(scales))


def _prediction_payload(action_probs: Sequence[float], intensities: Sequence[float]) -> dict[str, list[float]]:
    probabilities = _numeric_vector(
        action_probs, field="action_probs", length=len(ACTIONS)
    )
    if any(item < 0.0 or item > 1.0 for item in probabilities) or not math.isclose(
        sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-8
    ):
        raise DistillationContractError("predicted action_probs must sum to one")
    intensity_values = _numeric_vector(
        intensities, field="intensities", length=len(INTENSITY_ACTIONS)
    )
    if any(item < 0.0 or item > 1.0 for item in intensity_values):
        raise DistillationContractError("predicted intensities must lie in [0, 1]")
    return {"action_probs": probabilities, "intensities": intensity_values}


@dataclass(frozen=True)
class ActionPrior:
    """Feature-blind null baseline learned from train observations only."""

    action_probs: tuple[float, float, float]
    intensities: tuple[float, float]

    @classmethod
    def fit(cls, train_examples: Sequence[Mapping[str, Any]]) -> "ActionPrior":
        rows = normalize_observations(train_examples, stratum_key=None)
        rows.sort(key=_observation_sort_key)
        action_probs = [
            sum(row["target_probs"][index] for row in rows) / len(rows)
            for index in range(len(ACTIONS))
        ]
        intensities: list[float] = []
        for index in range(len(INTENSITY_ACTIONS)):
            weight = sum(row["intensity_weights"][index] for row in rows)
            if weight > 0.0:
                value = sum(
                    row["intensity_weights"][index]
                    * row["intensity_targets"][index]
                    for row in rows
                ) / weight
            else:
                value = 0.5
            intensities.append(value)
        prediction = _prediction_payload(action_probs, intensities)
        return cls(tuple(prediction["action_probs"]), tuple(prediction["intensities"]))

    def predict(self, features: Sequence[float] | None = None) -> dict[str, list[float]]:
        # Validate supplied features when present, while intentionally ignoring them.
        if features is not None:
            _numeric_vector(features, field="features")
        return _prediction_payload(self.action_probs, self.intensities)

    def predict_many(
        self, examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]]
    ) -> list[dict[str, list[float]]]:
        return [self.predict(features) for features in _feature_rows(examples_or_features)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_PRIOR_SCHEMA_VERSION,
            "action_order": list(ACTIONS),
            "intensity_action_order": list(INTENSITY_ACTIONS),
            "action_probs": list(self.action_probs),
            "intensities": list(self.intensities),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionPrior":
        if not isinstance(payload, Mapping) or payload.get("schema_version") != ACTION_PRIOR_SCHEMA_VERSION:
            raise DistillationContractError("unsupported action-prior schema")
        if payload.get("action_order") != list(ACTIONS) or payload.get(
            "intensity_action_order"
        ) != list(INTENSITY_ACTIONS):
            raise DistillationContractError("serialized action order does not match contract")
        prediction = _prediction_payload(
            payload.get("action_probs"), payload.get("intensities")
        )
        return cls(tuple(prediction["action_probs"]), tuple(prediction["intensities"]))


def _softmax(logits: Sequence[float]) -> list[float]:
    maximum = max(logits)
    exponentials = [math.exp(value - maximum) for value in logits]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def _matrix(rows: int, columns: int, value: float = 0.0) -> list[list[float]]:
    return [[value for _ in range(columns)] for _ in range(rows)]


def _adam_vector_update(
    parameters: list[float],
    gradients: Sequence[float],
    first_moment: list[float],
    second_moment: list[float],
    *,
    step: int,
    learning_rate: float,
    beta1: float = 0.9,
    beta2: float = 0.999,
    epsilon: float = 1e-8,
) -> None:
    correction1 = 1.0 - beta1**step
    correction2 = 1.0 - beta2**step
    for index, gradient in enumerate(gradients):
        first_moment[index] = beta1 * first_moment[index] + (1.0 - beta1) * gradient
        second_moment[index] = beta2 * second_moment[index] + (1.0 - beta2) * gradient * gradient
        adjusted_first = first_moment[index] / correction1
        adjusted_second = second_moment[index] / correction2
        parameters[index] -= learning_rate * adjusted_first / (
            math.sqrt(adjusted_second) + epsilon
        )


def _adam_matrix_update(
    parameters: list[list[float]],
    gradients: Sequence[Sequence[float]],
    first_moment: list[list[float]],
    second_moment: list[list[float]],
    *,
    step: int,
    learning_rate: float,
) -> None:
    for row, gradient, first, second in zip(
        parameters, gradients, first_moment, second_moment
    ):
        _adam_vector_update(
            row,
            gradient,
            first,
            second,
            step=step,
            learning_rate=learning_rate,
        )


class LinearSoftmaxStudent:
    """Interpretable linear softmax/sigmoid student baseline.

    Coefficients operate on standardized features.  The model uses the exact
    same action and conditional-intensity objectives as :class:`TanhMLPStudent`
    so their held-out metrics are directly comparable.
    """

    def __init__(self, input_dim: int, *, seed: int = 0) -> None:
        if isinstance(input_dim, bool) or not isinstance(input_dim, int) or input_dim <= 0:
            raise DistillationContractError("input_dim must be a positive integer")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise DistillationContractError("seed must be an integer")
        self.input_dim = input_dim
        self.seed = seed
        rng = random.Random(seed)
        limit = math.sqrt(6.0 / (input_dim + len(ACTIONS) + len(INTENSITY_ACTIONS)))
        self.w_action = [
            [rng.uniform(-limit, limit) for _ in range(input_dim)]
            for _ in ACTIONS
        ]
        self.b_action = [0.0] * len(ACTIONS)
        self.w_intensity = [
            [rng.uniform(-limit, limit) for _ in range(input_dim)]
            for _ in INTENSITY_ACTIONS
        ]
        self.b_intensity = [0.0] * len(INTENSITY_ACTIONS)
        self.standardizer: Standardizer | None = None
        self.training_config: dict[str, Any] | None = None

    def _forward_standardized(
        self, features: Sequence[float]
    ) -> tuple[list[float], list[float]]:
        action_logits = [
            bias + sum(weight * value for weight, value in zip(weights, features))
            for weights, bias in zip(self.w_action, self.b_action)
        ]
        intensity_logits = [
            bias + sum(weight * value for weight, value in zip(weights, features))
            for weights, bias in zip(self.w_intensity, self.b_intensity)
        ]
        return _softmax(action_logits), [_sigmoid(value) for value in intensity_logits]

    def fit(
        self,
        train_examples: Sequence[Mapping[str, Any]],
        standardizer: Standardizer | None = None,
        *,
        epochs: int = 300,
        learning_rate: float = 0.02,
        intensity_loss_weight: float = 1.0,
        l2: float = 1e-4,
    ) -> dict[str, list[float]]:
        rows = normalize_observations(train_examples, stratum_key=None)
        rows.sort(key=_observation_sort_key)
        if any(len(row["features"]) != self.input_dim for row in rows):
            raise DistillationContractError("student input_dim does not match examples")
        if isinstance(epochs, bool) or not isinstance(epochs, int) or epochs <= 0:
            raise DistillationContractError("epochs must be a positive integer")
        learning_rate = _finite_float(learning_rate, field="learning_rate")
        intensity_loss_weight = _finite_float(
            intensity_loss_weight, field="intensity_loss_weight"
        )
        l2 = _finite_float(l2, field="l2")
        if learning_rate <= 0.0 or intensity_loss_weight < 0.0 or l2 < 0.0:
            raise DistillationContractError("training rates and weights are out of range")
        fitted_standardizer = standardizer or Standardizer.fit(rows)
        if fitted_standardizer.feature_dim != self.input_dim:
            raise DistillationContractError("standardizer feature width does not match student")
        self.standardizer = fitted_standardizer
        transformed = [self.standardizer.transform(row["features"]) for row in rows]
        n_rows = len(rows)
        total_intensity_weight = sum(sum(row["intensity_weights"]) for row in rows)

        mwa = _matrix(len(ACTIONS), self.input_dim)
        vwa = _matrix(len(ACTIONS), self.input_dim)
        mba = [0.0] * len(ACTIONS)
        vba = [0.0] * len(ACTIONS)
        mwi = _matrix(len(INTENSITY_ACTIONS), self.input_dim)
        vwi = _matrix(len(INTENSITY_ACTIONS), self.input_dim)
        mbi = [0.0] * len(INTENSITY_ACTIONS)
        vbi = [0.0] * len(INTENSITY_ACTIONS)
        history: list[float] = []

        for step in range(1, epochs + 1):
            gwa = _matrix(len(ACTIONS), self.input_dim)
            gba = [0.0] * len(ACTIONS)
            gwi = _matrix(len(INTENSITY_ACTIONS), self.input_dim)
            gbi = [0.0] * len(INTENSITY_ACTIONS)
            action_loss = 0.0
            intensity_loss = 0.0
            for row, features in zip(rows, transformed):
                action_probs, intensities = self._forward_standardized(features)
                action_delta = [
                    (predicted - target) / n_rows
                    for predicted, target in zip(action_probs, row["target_probs"])
                ]
                action_loss -= sum(
                    target * math.log(max(predicted, 1e-15))
                    for target, predicted in zip(row["target_probs"], action_probs)
                ) / n_rows
                intensity_delta = [0.0] * len(INTENSITY_ACTIONS)
                if total_intensity_weight > 0.0 and intensity_loss_weight > 0.0:
                    for index, (predicted, target, weight) in enumerate(
                        zip(
                            intensities,
                            row["intensity_targets"],
                            row["intensity_weights"],
                        )
                    ):
                        intensity_loss += (
                            intensity_loss_weight
                            * weight
                            * (predicted - target) ** 2
                            / total_intensity_weight
                        )
                        derivative = (
                            2.0
                            * intensity_loss_weight
                            * weight
                            * (predicted - target)
                            / total_intensity_weight
                        )
                        intensity_delta[index] = derivative * predicted * (1.0 - predicted)
                for output_index, delta in enumerate(action_delta):
                    gba[output_index] += delta
                    for feature_index, feature_value in enumerate(features):
                        gwa[output_index][feature_index] += delta * feature_value
                for output_index, delta in enumerate(intensity_delta):
                    gbi[output_index] += delta
                    for feature_index, feature_value in enumerate(features):
                        gwi[output_index][feature_index] += delta * feature_value

            l2_sum = 0.0
            for parameters, gradients in (
                (self.w_action, gwa),
                (self.w_intensity, gwi),
            ):
                for parameter_row, gradient_row in zip(parameters, gradients):
                    for index, parameter in enumerate(parameter_row):
                        l2_sum += parameter * parameter
                        gradient_row[index] += l2 * parameter
            total_loss = action_loss + intensity_loss + 0.5 * l2 * l2_sum
            if not math.isfinite(total_loss):
                raise FloatingPointError("linear student training produced a non-finite loss")
            history.append(total_loss)
            _adam_matrix_update(
                self.w_action, gwa, mwa, vwa, step=step, learning_rate=learning_rate
            )
            _adam_vector_update(
                self.b_action, gba, mba, vba, step=step, learning_rate=learning_rate
            )
            _adam_matrix_update(
                self.w_intensity, gwi, mwi, vwi, step=step, learning_rate=learning_rate
            )
            _adam_vector_update(
                self.b_intensity, gbi, mbi, vbi, step=step, learning_rate=learning_rate
            )

        self.training_config = {
            "optimizer": "full_batch_adam",
            "epochs": epochs,
            "learning_rate": learning_rate,
            "intensity_loss_weight": intensity_loss_weight,
            "l2": l2,
            "train_observations_hash": canonical_observations_hash(
                rows, stratum_key=None
            ),
        }
        return {"loss": history}

    def predict(self, features: Sequence[float]) -> dict[str, list[float]]:
        if self.standardizer is None:
            raise RuntimeError("student must be fitted or loaded before prediction")
        action_probs, intensities = self._forward_standardized(
            self.standardizer.transform(features)
        )
        return _prediction_payload(action_probs, intensities)

    def predict_many(
        self, examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]]
    ) -> list[dict[str, list[float]]]:
        return [self.predict(features) for features in _feature_rows(examples_or_features)]

    def to_dict(self) -> dict[str, Any]:
        if self.standardizer is None:
            raise RuntimeError("student must be fitted or loaded before serialization")
        return {
            "schema_version": LINEAR_SCHEMA_VERSION,
            "model_type": "linear_softmax_student",
            "action_order": list(ACTIONS),
            "intensity_action_order": list(INTENSITY_ACTIONS),
            "input_dim": self.input_dim,
            "seed": self.seed,
            "standardizer": self.standardizer.to_dict(),
            "parameters": {
                "w_action": [list(row) for row in self.w_action],
                "b_action": list(self.b_action),
                "w_intensity": [list(row) for row in self.w_intensity],
                "b_intensity": list(self.b_intensity),
            },
            "training_config": dict(self.training_config or {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LinearSoftmaxStudent":
        if not isinstance(payload, Mapping) or payload.get("schema_version") != LINEAR_SCHEMA_VERSION:
            raise DistillationContractError("unsupported linear-student schema")
        if payload.get("model_type") != "linear_softmax_student":
            raise DistillationContractError("serialized model type does not match")
        if payload.get("action_order") != list(ACTIONS) or payload.get(
            "intensity_action_order"
        ) != list(INTENSITY_ACTIONS):
            raise DistillationContractError("serialized action order does not match contract")
        input_dim = payload.get("input_dim")
        seed = payload.get("seed")
        model = cls(input_dim, seed=seed)
        standardizer = Standardizer.from_dict(payload.get("standardizer"))
        if standardizer.feature_dim != input_dim:
            raise DistillationContractError("serialized standardizer width does not match")
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise DistillationContractError("serialized parameters must be a mapping")

        def matrix_value(name: str, rows: int) -> list[list[float]]:
            value = parameters.get(name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != rows:
                raise DistillationContractError(f"{name} has the wrong shape")
            return [
                _numeric_vector(row, field=name, length=input_dim)
                for row in value
            ]

        model.w_action = matrix_value("w_action", len(ACTIONS))
        model.b_action = _numeric_vector(
            parameters.get("b_action"), field="b_action", length=len(ACTIONS)
        )
        model.w_intensity = matrix_value("w_intensity", len(INTENSITY_ACTIONS))
        model.b_intensity = _numeric_vector(
            parameters.get("b_intensity"),
            field="b_intensity",
            length=len(INTENSITY_ACTIONS),
        )
        training_config = payload.get("training_config", {})
        if not isinstance(training_config, Mapping):
            raise DistillationContractError("training_config must be a mapping")
        model.training_config = dict(training_config)
        model.standardizer = standardizer
        model.predict(model.standardizer.means)
        return model

    def model_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()


class TanhMLPStudent:
    """One-hidden-layer numeric student with deterministic full-batch Adam."""

    def __init__(
        self,
        input_dim: int,
        *,
        hidden_dim: int = 16,
        seed: int = 0,
    ) -> None:
        if isinstance(input_dim, bool) or not isinstance(input_dim, int) or input_dim <= 0:
            raise DistillationContractError("input_dim must be a positive integer")
        if isinstance(hidden_dim, bool) or not isinstance(hidden_dim, int) or hidden_dim <= 0:
            raise DistillationContractError("hidden_dim must be a positive integer")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise DistillationContractError("seed must be an integer")
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.seed = seed
        rng = random.Random(seed)
        first_limit = math.sqrt(6.0 / (input_dim + hidden_dim))
        output_limit = math.sqrt(6.0 / (hidden_dim + len(ACTIONS) + len(INTENSITY_ACTIONS)))
        self.w1 = [
            [rng.uniform(-first_limit, first_limit) for _ in range(input_dim)]
            for _ in range(hidden_dim)
        ]
        self.b1 = [0.0] * hidden_dim
        self.w_action = [
            [rng.uniform(-output_limit, output_limit) for _ in range(hidden_dim)]
            for _ in ACTIONS
        ]
        self.b_action = [0.0] * len(ACTIONS)
        self.w_intensity = [
            [rng.uniform(-output_limit, output_limit) for _ in range(hidden_dim)]
            for _ in INTENSITY_ACTIONS
        ]
        self.b_intensity = [0.0] * len(INTENSITY_ACTIONS)
        self.standardizer: Standardizer | None = None
        self.training_config: dict[str, Any] | None = None

    def _forward_standardized(
        self, features: Sequence[float]
    ) -> tuple[list[float], list[float], list[float]]:
        hidden = [
            math.tanh(
                bias + sum(weight * value for weight, value in zip(weights, features))
            )
            for weights, bias in zip(self.w1, self.b1)
        ]
        action_logits = [
            bias + sum(weight * value for weight, value in zip(weights, hidden))
            for weights, bias in zip(self.w_action, self.b_action)
        ]
        intensity_logits = [
            bias + sum(weight * value for weight, value in zip(weights, hidden))
            for weights, bias in zip(self.w_intensity, self.b_intensity)
        ]
        return hidden, _softmax(action_logits), [_sigmoid(value) for value in intensity_logits]

    def fit(
        self,
        train_examples: Sequence[Mapping[str, Any]],
        standardizer: Standardizer | None = None,
        *,
        epochs: int = 300,
        learning_rate: float = 0.02,
        intensity_loss_weight: float = 1.0,
        l2: float = 1e-4,
    ) -> dict[str, list[float]]:
        """Fit only on the supplied train examples and return loss history.

        When ``standardizer`` is omitted it is fitted from ``train_examples``;
        validation/test rows are never accepted by or visible to this method.
        """

        rows = normalize_observations(train_examples, stratum_key=None)
        rows.sort(key=_observation_sort_key)
        if len(rows[0]["features"]) != self.input_dim:
            raise DistillationContractError("student input_dim does not match examples")
        if any(len(row["features"]) != self.input_dim for row in rows):
            raise DistillationContractError("student input_dim does not match examples")
        if isinstance(epochs, bool) or not isinstance(epochs, int) or epochs <= 0:
            raise DistillationContractError("epochs must be a positive integer")
        learning_rate = _finite_float(learning_rate, field="learning_rate")
        intensity_loss_weight = _finite_float(
            intensity_loss_weight, field="intensity_loss_weight"
        )
        l2 = _finite_float(l2, field="l2")
        if learning_rate <= 0.0 or intensity_loss_weight < 0.0 or l2 < 0.0:
            raise DistillationContractError("training rates and weights are out of range")
        fitted_standardizer = standardizer or Standardizer.fit(rows)
        if fitted_standardizer.feature_dim != self.input_dim:
            raise DistillationContractError("standardizer feature width does not match student")
        self.standardizer = fitted_standardizer
        transformed = [self.standardizer.transform(row["features"]) for row in rows]
        n_rows = len(rows)
        total_intensity_weight = sum(
            sum(row["intensity_weights"]) for row in rows
        )

        mw1 = _matrix(self.hidden_dim, self.input_dim)
        vw1 = _matrix(self.hidden_dim, self.input_dim)
        mb1 = [0.0] * self.hidden_dim
        vb1 = [0.0] * self.hidden_dim
        mwa = _matrix(len(ACTIONS), self.hidden_dim)
        vwa = _matrix(len(ACTIONS), self.hidden_dim)
        mba = [0.0] * len(ACTIONS)
        vba = [0.0] * len(ACTIONS)
        mwi = _matrix(len(INTENSITY_ACTIONS), self.hidden_dim)
        vwi = _matrix(len(INTENSITY_ACTIONS), self.hidden_dim)
        mbi = [0.0] * len(INTENSITY_ACTIONS)
        vbi = [0.0] * len(INTENSITY_ACTIONS)
        history: list[float] = []

        for step in range(1, epochs + 1):
            gw1 = _matrix(self.hidden_dim, self.input_dim)
            gb1 = [0.0] * self.hidden_dim
            gwa = _matrix(len(ACTIONS), self.hidden_dim)
            gba = [0.0] * len(ACTIONS)
            gwi = _matrix(len(INTENSITY_ACTIONS), self.hidden_dim)
            gbi = [0.0] * len(INTENSITY_ACTIONS)
            action_loss = 0.0
            intensity_loss = 0.0

            for row, features in zip(rows, transformed):
                hidden, action_probs, intensities = self._forward_standardized(features)
                action_delta = [
                    (predicted - target) / n_rows
                    for predicted, target in zip(action_probs, row["target_probs"])
                ]
                action_loss -= sum(
                    target * math.log(max(predicted, 1e-15))
                    for target, predicted in zip(row["target_probs"], action_probs)
                ) / n_rows

                intensity_delta = [0.0] * len(INTENSITY_ACTIONS)
                if total_intensity_weight > 0.0 and intensity_loss_weight > 0.0:
                    for index, (predicted, target, weight) in enumerate(
                        zip(
                            intensities,
                            row["intensity_targets"],
                            row["intensity_weights"],
                        )
                    ):
                        intensity_loss += (
                            intensity_loss_weight
                            * weight
                            * (predicted - target) ** 2
                            / total_intensity_weight
                        )
                        derivative = (
                            2.0
                            * intensity_loss_weight
                            * weight
                            * (predicted - target)
                            / total_intensity_weight
                        )
                        intensity_delta[index] = derivative * predicted * (1.0 - predicted)

                hidden_delta = [0.0] * self.hidden_dim
                for output_index, delta in enumerate(action_delta):
                    gba[output_index] += delta
                    for hidden_index, hidden_value in enumerate(hidden):
                        gwa[output_index][hidden_index] += delta * hidden_value
                        hidden_delta[hidden_index] += (
                            self.w_action[output_index][hidden_index] * delta
                        )
                for output_index, delta in enumerate(intensity_delta):
                    gbi[output_index] += delta
                    for hidden_index, hidden_value in enumerate(hidden):
                        gwi[output_index][hidden_index] += delta * hidden_value
                        hidden_delta[hidden_index] += (
                            self.w_intensity[output_index][hidden_index] * delta
                        )
                for hidden_index, hidden_value in enumerate(hidden):
                    delta = hidden_delta[hidden_index] * (1.0 - hidden_value * hidden_value)
                    gb1[hidden_index] += delta
                    for feature_index, feature_value in enumerate(features):
                        gw1[hidden_index][feature_index] += delta * feature_value

            l2_sum = 0.0
            for parameters, gradients in (
                (self.w1, gw1),
                (self.w_action, gwa),
                (self.w_intensity, gwi),
            ):
                for parameter_row, gradient_row in zip(parameters, gradients):
                    for index, parameter in enumerate(parameter_row):
                        l2_sum += parameter * parameter
                        gradient_row[index] += l2 * parameter
            total_loss = action_loss + intensity_loss + 0.5 * l2 * l2_sum
            if not math.isfinite(total_loss):
                raise FloatingPointError("student training produced a non-finite loss")
            history.append(total_loss)

            _adam_matrix_update(
                self.w1, gw1, mw1, vw1, step=step, learning_rate=learning_rate
            )
            _adam_vector_update(
                self.b1, gb1, mb1, vb1, step=step, learning_rate=learning_rate
            )
            _adam_matrix_update(
                self.w_action, gwa, mwa, vwa, step=step, learning_rate=learning_rate
            )
            _adam_vector_update(
                self.b_action, gba, mba, vba, step=step, learning_rate=learning_rate
            )
            _adam_matrix_update(
                self.w_intensity, gwi, mwi, vwi, step=step, learning_rate=learning_rate
            )
            _adam_vector_update(
                self.b_intensity, gbi, mbi, vbi, step=step, learning_rate=learning_rate
            )

        self.training_config = {
            "optimizer": "full_batch_adam",
            "epochs": epochs,
            "learning_rate": learning_rate,
            "intensity_loss_weight": intensity_loss_weight,
            "l2": l2,
            "train_observations_hash": canonical_observations_hash(
                rows, stratum_key=None
            ),
        }
        return {"loss": history}

    def predict(self, features: Sequence[float]) -> dict[str, list[float]]:
        if self.standardizer is None:
            raise RuntimeError("student must be fitted or loaded before prediction")
        standardized = self.standardizer.transform(features)
        _hidden, action_probs, intensities = self._forward_standardized(standardized)
        return _prediction_payload(action_probs, intensities)

    def predict_many(
        self, examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]]
    ) -> list[dict[str, list[float]]]:
        return [self.predict(features) for features in _feature_rows(examples_or_features)]

    def to_dict(self) -> dict[str, Any]:
        if self.standardizer is None:
            raise RuntimeError("student must be fitted or loaded before serialization")
        return {
            "schema_version": MLP_SCHEMA_VERSION,
            "model_type": "tanh_mlp_student",
            "action_order": list(ACTIONS),
            "intensity_action_order": list(INTENSITY_ACTIONS),
            "input_dim": self.input_dim,
            "hidden_dim": self.hidden_dim,
            "seed": self.seed,
            "standardizer": self.standardizer.to_dict(),
            "parameters": {
                "w1": [list(row) for row in self.w1],
                "b1": list(self.b1),
                "w_action": [list(row) for row in self.w_action],
                "b_action": list(self.b_action),
                "w_intensity": [list(row) for row in self.w_intensity],
                "b_intensity": list(self.b_intensity),
            },
            "training_config": dict(self.training_config or {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TanhMLPStudent":
        if not isinstance(payload, Mapping) or payload.get("schema_version") != MLP_SCHEMA_VERSION:
            raise DistillationContractError("unsupported student schema")
        if payload.get("model_type") != "tanh_mlp_student":
            raise DistillationContractError("serialized model type does not match")
        if payload.get("action_order") != list(ACTIONS) or payload.get(
            "intensity_action_order"
        ) != list(INTENSITY_ACTIONS):
            raise DistillationContractError("serialized action order does not match contract")
        input_dim = payload.get("input_dim")
        hidden_dim = payload.get("hidden_dim")
        seed = payload.get("seed")
        model = cls(input_dim, hidden_dim=hidden_dim, seed=seed)
        standardizer = Standardizer.from_dict(payload.get("standardizer"))
        if standardizer.feature_dim != input_dim:
            raise DistillationContractError("serialized standardizer width does not match")
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise DistillationContractError("serialized parameters must be a mapping")

        def matrix_value(name: str, rows: int, columns: int) -> list[list[float]]:
            value = parameters.get(name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != rows:
                raise DistillationContractError(f"{name} has the wrong shape")
            return [
                _numeric_vector(row, field=name, length=columns)
                for row in value
            ]

        model.w1 = matrix_value("w1", hidden_dim, input_dim)
        model.b1 = _numeric_vector(parameters.get("b1"), field="b1", length=hidden_dim)
        model.w_action = matrix_value("w_action", len(ACTIONS), hidden_dim)
        model.b_action = _numeric_vector(
            parameters.get("b_action"), field="b_action", length=len(ACTIONS)
        )
        model.w_intensity = matrix_value(
            "w_intensity", len(INTENSITY_ACTIONS), hidden_dim
        )
        model.b_intensity = _numeric_vector(
            parameters.get("b_intensity"),
            field="b_intensity",
            length=len(INTENSITY_ACTIONS),
        )
        training_config = payload.get("training_config", {})
        if not isinstance(training_config, Mapping):
            raise DistillationContractError("training_config must be a mapping")
        model.training_config = dict(training_config)
        model.standardizer = standardizer
        # Exercise the loaded graph and fail closed on pathological parameters.
        model.predict(model.standardizer.means)
        return model

    def model_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()


def evaluate_predictions(
    examples: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> dict[str, float | int | None]:
    """Compute soft-label action and conditional-intensity metrics."""

    rows = normalize_observations(examples, stratum_key=None)
    if isinstance(predictions, (str, bytes)) or not isinstance(predictions, Sequence):
        raise DistillationContractError("predictions must be a sequence")
    if len(rows) != len(predictions):
        raise DistillationContractError("prediction count does not match examples")
    normalized_predictions = [
        _prediction_payload(
            prediction.get("action_probs"), prediction.get("intensities")
        )
        if isinstance(prediction, Mapping)
        else (_ for _ in ()).throw(
            DistillationContractError("each prediction must be a mapping")
        )
        for prediction in predictions
    ]

    cross_entropy = 0.0
    brier = 0.0
    correct = 0
    intensity_error = 0.0
    intensity_weight = 0.0
    for row, prediction in zip(rows, normalized_predictions):
        cross_entropy -= sum(
            target * math.log(max(predicted, 1e-15))
            for target, predicted in zip(
                row["target_probs"], prediction["action_probs"]
            )
        )
        brier += sum(
            (predicted - target) ** 2
            for predicted, target in zip(
                prediction["action_probs"], row["target_probs"]
            )
        )
        predicted_class = max(
            range(len(ACTIONS)), key=lambda index: prediction["action_probs"][index]
        )
        target_class = max(
            range(len(ACTIONS)), key=lambda index: row["target_probs"][index]
        )
        correct += int(predicted_class == target_class)
        for predicted, target, weight in zip(
            prediction["intensities"],
            row["intensity_targets"],
            row["intensity_weights"],
        ):
            intensity_error += weight * abs(predicted - target)
            intensity_weight += weight

    count = len(rows)
    return {
        "n": count,
        "action_cross_entropy": cross_entropy / count,
        "action_brier": brier / count,
        "action_accuracy": correct / count,
        "conditional_intensity_mae": (
            intensity_error / intensity_weight if intensity_weight > 0.0 else None
        ),
        "conditional_intensity_weight": intensity_weight,
    }


def evaluate_model(
    model: ActionPrior | LinearSoftmaxStudent | TanhMLPStudent,
    examples: Sequence[Mapping[str, Any]],
) -> dict[str, float | int | None]:
    rows = normalize_observations(examples, stratum_key=None)
    return evaluate_predictions(rows, model.predict_many(rows))


@dataclass(frozen=True)
class OODReference:
    """Train-only rectangular support plus standardization for OOD diagnostics."""

    feature_min: tuple[float, ...]
    feature_max: tuple[float, ...]
    standardizer: Standardizer

    @property
    def feature_dim(self) -> int:
        return len(self.feature_min)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OOD_REFERENCE_SCHEMA_VERSION,
            "feature_min": list(self.feature_min),
            "feature_max": list(self.feature_max),
            "standardizer": self.standardizer.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OODReference":
        if not isinstance(payload, Mapping) or payload.get("schema_version") != OOD_REFERENCE_SCHEMA_VERSION:
            raise DistillationContractError("unsupported OOD reference schema")
        standardizer = Standardizer.from_dict(payload.get("standardizer"))
        lower = _numeric_vector(
            payload.get("feature_min"),
            field="feature_min",
            length=standardizer.feature_dim,
        )
        upper = _numeric_vector(
            payload.get("feature_max"),
            field="feature_max",
            length=standardizer.feature_dim,
        )
        if any(low > high for low, high in zip(lower, upper)):
            raise DistillationContractError("OOD feature minima exceed maxima")
        return cls(tuple(lower), tuple(upper), standardizer)


def build_ood_reference(
    train_examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]],
    *,
    standardizer: Standardizer | None = None,
) -> OODReference:
    """Build OOD support exclusively from the caller-supplied train rows."""

    rows = _feature_rows(train_examples_or_features)
    fitted_standardizer = standardizer or Standardizer.fit(rows)
    if fitted_standardizer.feature_dim != len(rows[0]):
        raise DistillationContractError("OOD standardizer width does not match rows")
    lower = [min(row[index] for row in rows) for index in range(len(rows[0]))]
    upper = [max(row[index] for row in rows) for index in range(len(rows[0]))]
    return OODReference(tuple(lower), tuple(upper), fitted_standardizer)


def ood_diagnostics(
    examples_or_features: Sequence[Mapping[str, Any] | Sequence[float]],
    reference: OODReference,
    *,
    z_threshold: float = OOD_Z_THRESHOLD,
) -> dict[str, Any]:
    """Report train-range and standardized-tail exceedances without inference."""

    rows = _feature_rows(examples_or_features)
    if len(rows[0]) != reference.feature_dim:
        raise DistillationContractError("OOD reference width does not match rows")
    z_threshold = _finite_float(z_threshold, field="z_threshold")
    if z_threshold <= 0.0:
        raise DistillationContractError("z_threshold must be positive")
    outside_by_feature = [0] * reference.feature_dim
    z_exceedances_by_feature = [0] * reference.feature_dim
    outside_rows = 0
    maximum_abs_z = 0.0
    for row in rows:
        standardized = reference.standardizer.transform(row)
        row_outside = False
        for index, (value, z_value) in enumerate(zip(row, standardized)):
            if value < reference.feature_min[index] or value > reference.feature_max[index]:
                outside_by_feature[index] += 1
                row_outside = True
            if abs(z_value) > z_threshold:
                z_exceedances_by_feature[index] += 1
            maximum_abs_z = max(maximum_abs_z, abs(z_value))
        outside_rows += int(row_outside)
    n_rows = len(rows)
    return {
        "n": n_rows,
        "z_threshold": z_threshold,
        "outside_train_range_rows": outside_rows,
        "outside_train_range_row_fraction": outside_rows / n_rows,
        "outside_train_range_by_feature": outside_by_feature,
        "outside_train_range_fraction_by_feature": [
            count / n_rows for count in outside_by_feature
        ],
        "z_exceedances_by_feature": z_exceedances_by_feature,
        "z_exceedance_fraction_by_feature": [
            count / n_rows for count in z_exceedances_by_feature
        ],
        "max_abs_z": maximum_abs_z,
    }


__all__ = [
    "ACTIONS",
    "INTENSITY_ACTIONS",
    "ActionPrior",
    "DatasetSplit",
    "DistillationContractError",
    "LinearSoftmaxStudent",
    "OODReference",
    "OOD_DIAGNOSTIC_SCHEMA_VERSION",
    "OOD_Z_THRESHOLD",
    "Standardizer",
    "TanhMLPStudent",
    "apply_frozen_family_assignments",
    "build_ood_reference",
    "canonical_observations_hash",
    "deterministic_group_split",
    "evaluate_model",
    "evaluate_predictions",
    "normalize_observation",
    "normalize_observations",
    "ood_diagnostics",
    "ood_diagnostic_descriptor",
]
