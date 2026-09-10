"""Training-family learning curves on an already frozen information-study split.

Pure analysis library: the managed caller verifies source bytes and provenance.
Test rows are identified from metadata and excluded before label normalization.
Existing V2 fitting and information-Student metrics are reused without changes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from time import perf_counter
from typing import Any

from nmsim import information_student as student
from nmsim import information_weight as domain
from nmsim import v2_distillation as distill


SCHEMA_VERSION = "information-student-learning-curves/1.0"
SUBSET_SCHEMA_VERSION = "information-student-nested-training-families/1.0"
IDENTITY_ROSTER_SCHEMA_VERSION = "information-student-label-free-roster/1.0"
DEFAULT_FRACTIONS = (0.125, 0.25, 0.5, 1.0)
LearningCurveContractError = student.StudentContractError


def _original_split(original_study: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    if not isinstance(original_study, Mapping) or original_study.get("schema_version") != student.SCHEMA_VERSION:
        raise LearningCurveContractError("original_study must be an information Student study")
    source_split = original_study.get("split")
    if not isinstance(source_split, Mapping) or source_split.get("schema_version") != student.SPLIT_SCHEMA_VERSION:
        raise LearningCurveContractError("original study must contain its frozen group split")
    split = deepcopy(dict(source_split))
    supplied_hash = student._sha(split.pop("split_semantic_hash", None), "split_semantic_hash")
    if student.stable_hash(split) != supplied_hash:
        raise LearningCurveContractError("original split semantic hash mismatch")
    split["split_semantic_hash"] = supplied_hash
    if split.get("assignment_algorithm_schema") != distill.SPLIT_SCHEMA_VERSION or split.get("fractions") != list(student.FRACTIONS):
        raise LearningCurveContractError("original split algorithm or fractions changed")
    training = original_study.get("training")
    if not isinstance(training, Mapping):
        raise LearningCurveContractError("original study training settings are required")
    seed = training.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or split.get("seed") != seed:
        raise LearningCurveContractError("original training and split seeds must agree")
    for key in ("sample_assignments", "latent_assignments", "family_assignments"):
        assignments = split.get(key)
        if not isinstance(assignments, Mapping) or not assignments or any(value not in student.PARTITIONS for value in assignments.values()):
            raise LearningCurveContractError(f"invalid original {key}")
    if not isinstance(split.get("counts"), Mapping):
        raise LearningCurveContractError("original split counts are required")
    return split, seed


def _training_and_validation(records: Sequence[Mapping[str, Any]], split: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Check the exact identity roster, then pass only non-test rows to ingestion.

    ``prepare_records`` also computes a temporary split over those non-test
    rows. Its assignments are discarded; all valid examples are reassigned
    exclusively by the original study's immutable sample assignments. Reusing
    this helper preserves its hash, paired-view and failure validation rules.
    """
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence) or not records:
        raise LearningCurveContractError("records must be a nonempty sequence")
    assignments = split["sample_assignments"]
    roster, non_test = [], []
    seen: set[str] = set()
    groups: dict[str, list[dict[str, str]]] = {}
    for record in records:
        if not isinstance(record, Mapping) or record.get("schema_version") != student.SOURCE_SCHEMA_VERSION:
            raise LearningCurveContractError("unsupported public sample schema")
        sample = student._sha(record.get("sample_id"), "sample_id")
        latent = student._identity(record.get("latent_state_id"), "latent_state_id")
        family = student._identity(record.get("family_id", latent), "family_id")
        profile = record.get("profile_id")
        if sample in seen or sample not in assignments or profile not in domain.PROFILE_IDS:
            raise LearningCurveContractError("duplicate/unknown sample or unknown profile")
        seen.add(sample)
        partition = assignments[sample]
        if split["latent_assignments"].get(latent) != partition or split["family_assignments"].get(family) != partition:
            raise LearningCurveContractError("source identity disagrees with original assignments")
        identity = {"sample_id": sample, "latent_state_id": latent, "family_id": family,
                    "profile_id": profile, "partition": partition}
        roster.append(identity)
        groups.setdefault(latent, []).append(identity)
        if partition != "test":
            non_test.append(record)
        # No decision, status, response hash, observation, or attempt field of
        # a test row is accessed. Test payloads can be absent or malformed.
    if seen != set(assignments) or set(groups) != set(split["latent_assignments"]) or {row["family_id"] for row in roster} != set(split["family_assignments"]):
        raise LearningCurveContractError("source roster differs from the original frozen split")
    for group in groups.values():
        if len(group) != len(domain.PROFILE_IDS) or {row["profile_id"] for row in group} != set(domain.PROFILE_IDS) or len({row["family_id"] for row in group}) != 1:
            raise LearningCurveContractError("each latent group must retain all four views in one family")
    prepared = student.prepare_records(non_test, seed=seed)
    examples = {"train": [], "validation": []}
    for temporary_partition in student.PARTITIONS:
        for row in prepared["examples_by_partition"][temporary_partition]:
            examples[assignments[row["state_id"]]].append(row)
    for partition, rows in examples.items():
        rows.sort(key=lambda row: (row["family_id"], row["latent_state_id"], row["profile_id"], row["state_id"]))
        if not rows:
            raise LearningCurveContractError("original train and validation partitions need valid rows")
        counts = split["counts"].get(partition)
        if not isinstance(counts, Mapping) or counts.get("valid") != len(rows):
            raise LearningCurveContractError("non-test valid counts differ from the original study")
    for partition in student.PARTITIONS:
        assigned = [row for row in roster if row["partition"] == partition]
        counts = split["counts"].get(partition)
        expected = {"requested": len(assigned), "latent_groups": len({row["latent_state_id"] for row in assigned}),
                    "families": len({row["family_id"] for row in assigned})}
        if not isinstance(counts, Mapping) or any(counts.get(key) != value for key, value in expected.items()):
            raise LearningCurveContractError("identity roster counts differ from the original study")
    roster.sort(key=lambda row: (row["family_id"], row["latent_state_id"], row["profile_id"], row["sample_id"]))
    return {"examples": examples, "roster": roster,
            "non_test_normalized_records_semantic_hash": prepared["normalized_records_hash"],
            "identity_roster_semantic_hash": student.stable_hash({"schema_version": IDENTITY_ROSTER_SCHEMA_VERSION, "rows": roster})}


def learning_curves(records: Sequence[Mapping[str, Any]], original_study: Mapping[str, Any], *,
                    fractions: Sequence[float] = DEFAULT_FRACTIONS, epochs: int = 120,
                    hidden_dim: int = 16, backend: str = "auto") -> dict[str, Any]:
    """Fit nested training-family subsets and evaluate train/validation only.

    Every point starts fresh with the original seed and the existing fitting
    kernels. No model is selected or deployed, and no test metric is computed.
    Test labels and observations are deliberately not read, even for validation.
    Runtime is informational and excluded from ``curve_semantic_hash``.
    """
    student._integer(epochs, "epochs", 1)
    student._integer(hidden_dim, "hidden_dim", 1)
    if isinstance(fractions, (str, bytes)) or not isinstance(fractions, Sequence) or not fractions:
        raise LearningCurveContractError("fractions must be a nonempty increasing sequence")
    fraction_values = [student._number(value, "fraction") for value in fractions]
    if any(not 0 < value <= 1 for value in fraction_values) or any(a >= b for a, b in zip(fraction_values, fraction_values[1:])):
        raise LearningCurveContractError("fractions must be strictly increasing in (0, 1]")
    if backend not in ("auto", "numpy", "python"):
        raise LearningCurveContractError("backend must be auto, numpy, or python")
    np = None
    if backend in ("auto", "numpy"):
        try:
            import numpy as np
        except ImportError:
            if backend == "numpy":
                raise LearningCurveContractError("numpy backend requested but NumPy is unavailable") from None
    split, seed = _original_split(original_study)
    prepared = _training_and_validation(records, split, seed)
    provenance = original_study.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("normalized_records_hash_schema") != student.DATA_SCHEMA_VERSION:
        raise LearningCurveContractError("original source normalized-record identity is required")
    original_hash = student._sha(provenance.get("normalized_records_semantic_hash"), "original_normalized_records_semantic_hash")
    training = prepared["examples"]["train"]
    validation = prepared["examples"]["validation"]
    families = sorted((family for family, partition in split["family_assignments"].items() if partition == "train"),
                      key=lambda family: (student.stable_hash({"schema_version": SUBSET_SCHEMA_VERSION, "seed": seed, "family_id": family}), family))
    points, timings = [], []
    for fraction in fraction_values:
        family_count = max(1, math.ceil(fraction * len(families)))
        subset_families = families[:family_count]
        family_set = set(subset_families)
        subset = [row for row in training if row["family_id"] in family_set]
        requested = [row for row in prepared["roster"] if row["family_id"] in family_set]
        if not subset:
            raise LearningCurveContractError("a requested training-family subset has no valid Teacher rows")
        start = perf_counter()
        standardizer = distill.Standardizer.fit(subset)
        timing: dict[str, Any] = {"fraction": fraction, "standardizer_seconds": perf_counter() - start,
                                  "fit_seconds_by_model": {}, "evaluation_seconds_by_model": {}}
        models, histories, evaluations = {}, {}, {}
        for name in student.MODEL_NAMES:
            start = perf_counter()
            if name == "prior":
                model = distill.ActionPrior.fit(subset)
            else:
                model = (distill.LinearSoftmaxStudent(student.FEATURE_DIM, seed=seed) if name == "linear"
                         else distill.TanhMLPStudent(student.FEATURE_DIM, hidden_dim=hidden_dim, seed=seed))
                if np is None:
                    histories[name] = model.fit(subset, standardizer, epochs=epochs)
                    model.training_config.update({"backend": "python", "backend_version": "stdlib"})
                else:
                    histories[name] = student._numpy_fit(model, subset, standardizer, epochs=epochs, np=np)
            timing["fit_seconds_by_model"][name] = perf_counter() - start
            start = perf_counter()
            evaluations[name] = {"train": student._metrics(model, subset), "validation": student._metrics(model, validation)}
            timing["evaluation_seconds_by_model"][name] = perf_counter() - start
            models[name] = model.to_dict()
        points.append({
            "fraction": fraction, "family_ids": subset_families, "family_count": family_count,
            "total_training_family_count": len(families), "effective_family_fraction": family_count / len(families),
            "latent_group_count": len({row["latent_state_id"] for row in requested}),
            "requested_rows": len(requested), "valid_rows": len(subset), "failed_rows": len(requested) - len(subset),
            "training_sample_ids": [row["state_id"] for row in subset],
            "training_observations_hash_schema": distill.OBSERVATION_HASH_SCHEMA_VERSION,
            "training_observations_semantic_hash": distill.canonical_observations_hash(subset, stratum_key=None),
            "standardizer": standardizer.to_dict(), "models": models,
            "model_serialization_hashes": {name: student.stable_hash(payload) for name, payload in models.items()},
            "parameter_counts": {name: student.parameter_count(payload) for name, payload in models.items()},
            "loss_history": histories, "evaluation": evaluations,
        })
        timings.append(timing)
    result = {
        "schema_version": SCHEMA_VERSION, "encoder": student.encoder_descriptor(),
        "settings": {"fractions": fraction_values, "epochs": epochs, "hidden_dim": hidden_dim, "seed": seed,
                     "backend": "numpy" if np is not None else "python", "backend_version": np.__version__ if np is not None else "stdlib",
                     "subset_schema_version": SUBSET_SCHEMA_VERSION, "family_count_rounding": "ceil_with_minimum_one",
                     "subset_order": "SHA-256 rank of schema/seed/family_id; shared prefix at every fraction",
                     "parameter_count_excludes_standardizer_statistics": True,
                     "fitting_kernel": "unchanged information_student/v2_distillation full-batch Adam",
                     "model_selection_performed": False},
        "source_identity": {
            "original_normalized_records_hash_schema": student.DATA_SCHEMA_VERSION,
            "original_normalized_records_semantic_hash": original_hash,
            "original_identity_is_reference_not_recomputed_from_test_payloads": True,
            "non_test_normalized_records_hash_schema": student.DATA_SCHEMA_VERSION,
            "non_test_normalized_records_semantic_hash": prepared["non_test_normalized_records_semantic_hash"],
            "identity_roster_hash_schema": IDENTITY_ROSTER_SCHEMA_VERSION,
            "identity_roster_semantic_hash": prepared["identity_roster_semantic_hash"],
            "source_file_byte_hash": "must_be_verified_and_recorded_by_managed_caller",
        },
        "split": split, "nested_training_family_order": families,
        "validation": {"family_count": split["counts"]["validation"]["families"],
                       "valid_rows": len(validation), "sample_ids": [row["state_id"] for row in validation],
                       "observations_hash_schema": distill.OBSERVATION_HASH_SCHEMA_VERSION,
                       "observations_semantic_hash": distill.canonical_observations_hash(validation, stratum_key=None)},
        "test_exclusion": {"excluded_requested_rows": split["counts"]["test"]["requested"],
                           "excluded_family_count": split["counts"]["test"]["families"],
                           "excluded_sample_ids": sorted(sample for sample, partition in split["sample_assignments"].items() if partition == "test"),
                           "labels_accessed": False, "observations_accessed": False, "predictions_computed": 0,
                           "used_for_fitting": False, "used_for_selection": False,
                           "metadata_only_roster_validation": True},
        "points": points,
        "interpretation": [
            "Original study test results were previously disclosed; that test is development evidence, not a fresh final test.",
            "Only the original training-family count changes; validation rows, seed, epochs, architecture and fitting kernels stay fixed across points.",
            "One nested subset order and one initialization seed give exploratory learning curves, not uncertainty estimates or guaranteed sample-complexity scaling.",
            "Validation can guide development; future confirmatory claims require a newly frozen independent evaluation source.",
            "K=1 per state/view supplies Teacher labels, not independent human participants or calibrated human action distributions.",
        ],
    }
    result["curve_semantic_hash"] = student.stable_hash(result)
    result["runtime_information"] = {"clock": "time.perf_counter", "informational_only": True,
                                     "excluded_from_curve_semantic_hash": True, "points": timings}
    return result


__all__ = ["SCHEMA_VERSION", "SUBSET_SCHEMA_VERSION", "IDENTITY_ROSTER_SCHEMA_VERSION",
           "DEFAULT_FRACTIONS", "LearningCurveContractError", "learning_curves"]
