"""Public-observation distillation for the information-allocation Teacher data.

Pure library: no file access, Provider, run creation, or network. The historical
Teacher contracts and V2 serialized model formats remain unchanged. NumPy is
optional during fitting only; inference always uses the stdlib V2 implementation.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Any, Callable

from nmsim import information_weight as domain
from nmsim import information_weight_scale as scale
from nmsim import v2_attention as attention
from nmsim import v2_distillation as distill


SCHEMA_VERSION = "information-student-experiment/1.0"
ENCODER_SCHEMA_VERSION = "information-visible-mask-account-encoder/1.0"
DATA_SCHEMA_VERSION = "information-student-normalized-public-records/1.0"
SOURCE_SCHEMA_VERSION = "information_weight_teacher_scale_sample/1.0"
SPLIT_SCHEMA_VERSION = "information-student-group-split/1.0"
INFORMATION_FIELDS = domain.P8 + domain.F8 + domain.N8
FEATURE_NAMES = INFORMATION_FIELDS + tuple(name + "__visible" for name in INFORMATION_FIELDS) + domain.ACCOUNT8
FEATURE_DIM = len(FEATURE_NAMES)
DEFAULT_SEED = 20260909
DEFAULT_EPOCHS = 120
PARTITIONS = ("train", "validation", "test")
FRACTIONS = (0.70, 0.15, 0.15)
MODEL_NAMES = ("prior", "linear", "mlp")
StudentContractError = distill.DistillationContractError
_PRIVATE_KEYS = frozenset({"raw_response", "raw_completion", "private_rationale", "private_reasoning", "reasoning", "api_key", "base_url", "raw_endpoint", "private_error"})


def stable_hash(value: Any) -> str:
    """Semantic SHA-256 of explicitly schema-tagged canonical JSON payloads."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def encoder_descriptor() -> dict[str, Any]:
    return {
        "schema_version": ENCODER_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "feature_dim": FEATURE_DIM,
        "unobserved_information_value": 0.0,
        "information_presence_masks": "one iff the numeric field was actually visible to Teacher",
        "identity_features": [],
        "account_fields": list(domain.ACCOUNT8),
        "inference_domain_policy": "finite_values_no_clipping; train_support_diagnostic_separate",
    }


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StudentContractError(f"{field} must be a finite number")
    return 0.0 if value == 0 else float(value)


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StudentContractError(f"{field} must be an integer >= {minimum}")
    return value


def _identity(value: Any, field: str) -> str:
    # Research identifiers must not double as arbitrary text/reasoning channels.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value):
        raise StudentContractError(f"invalid {field}")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise StudentContractError(f"{field} must be a SHA-256 hex digest")
    return value


def encode_observation(visible_fields: Mapping[str, Any], account_state: Mapping[str, Any]) -> list[float]:
    """Encode only visible values, their masks, and the eight account variables.

    No profile label, latent information, state identifier, or prompt enters the
    feature vector. Missing information and an observed zero remain distinct.
    Finite OOD values are accepted without clipping; support diagnostics must be
    reported separately. Ingestion checks the historical training design ranges.
    """
    if not isinstance(visible_fields, Mapping) or not isinstance(account_state, Mapping):
        raise StudentContractError("visible_fields and account_state must be mappings")
    if set(visible_fields) - set(INFORMATION_FIELDS):
        raise StudentContractError("unknown information feature")
    if set(account_state) != set(domain.ACCOUNT8):
        raise StudentContractError("account_state must contain exactly ACCOUNT8")
    values = {name: _number(value, name) for name, value in visible_fields.items()}
    account = {name: _number(account_state[name], name) for name in domain.ACCOUNT8}
    for name in domain.BINARY_FIELDS:
        if name in values and values[name] not in (0.0, 1.0):
            raise StudentContractError(f"{name} must be binary")
    for value_name, mask_name in attention.MASKED_FEATURES:
        if account[mask_name] not in (0.0, 1.0):
            raise StudentContractError(f"{mask_name} must be binary")
        if account[mask_name] == 0 and account[value_name] != 0:
            raise StudentContractError(f"unavailable {value_name} must be zero")
    if account["post_sale_return_mask"] and not account["days_since_trade_scaled_mask"]:
        raise StudentContractError("sale history requires trade history")
    return ([values.get(name, 0.0) for name in INFORMATION_FIELDS]
            + [float(name in values) for name in INFORMATION_FIELDS]
            + [account[name] for name in domain.ACCOUNT8])


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one historical public sample and whitelist the learning fields.

    Prompt strings and irrelevant extra metadata are never copied. Known private
    fields are rejected. A caller may retain raw public rows separately but must
    supply their file-byte digest/provenance at the managed entrypoint boundary.
    """
    if not isinstance(record, Mapping) or record.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise StudentContractError("unsupported public sample schema")
    if _PRIVATE_KEYS.intersection(record):
        raise StudentContractError("private fields are forbidden in public student inputs")
    sample_id = _sha(record.get("sample_id"), "sample_id")
    latent = _identity(record.get("latent_state_id"), "latent_state_id")
    family = _identity(record.get("family_id", latent), "family_id")
    profile = record.get("profile_id")
    if profile not in domain.PROFILE_IDS:
        raise StudentContractError("unknown profile_id")
    view = record.get("visible_observation")
    if not isinstance(view, Mapping) or view.get("schema_version") != scale.VIEW_SCHEMA_VERSION:
        raise StudentContractError("unsupported public view schema")
    if view.get("latent_id") != latent or view.get("profile_id") != profile:
        raise StudentContractError("public view identity disagrees with sample")
    latent_hash = _sha(view.get("latent_hash"), "latent_hash")
    view_hash = _sha(view.get("view_hash"), "view_hash")
    visible, account = view.get("visible_fields"), view.get("account_state")
    features = encode_observation(visible, account)
    if set(visible) != set(domain.PROFILE_FIELDS[profile]):
        raise StudentContractError("visible fields disagree with frozen profile")
    expected_hash = domain.stable_hash({
        "schema_version": scale.VIEW_SCHEMA_VERSION, "latent_hash": latent_hash,
        "profile_id": profile, "visible_fields": visible, "account_state": account,
    })
    if view_hash != expected_hash:
        raise StudentContractError("public view hash mismatch")
    ranges = {**attention.FEATURE_RANGES, **domain.EXTRA_FIELD_RANGES}
    for name, value in {**visible, **account}.items():
        lower, upper = ranges[name]
        if not lower <= value <= upper:
            raise StudentContractError(f"training feature {name} is outside frozen domain")
    status = record.get("status")
    if status not in ("valid", "failed"):
        raise StudentContractError("public status must be valid or failed")
    application = _integer(record.get("application_attempt_count"), "application_attempt_count", 1)
    retries = _integer(record.get("technical_retry_count"), "technical_retry_count")
    if retries != application - 1:
        raise StudentContractError("attempt/retry accounting mismatch")
    response_hash = record.get("response_hash")
    if response_hash is not None:
        _sha(response_hash, "response_hash")
    decision = record.get("decision")
    action = intensity = None
    failure = record.get("failure_code")
    if status == "valid":
        if not isinstance(decision, Mapping) or set(decision) != {"action", "intensity", "response_schema_version"}:
            raise StudentContractError("valid public decision must have exactly three public fields")
        if decision["response_schema_version"] != attention.RESPONSE_SCHEMA_VERSION:
            raise StudentContractError("unsupported Teacher response schema")
        action = decision["action"]
        intensity = _number(decision["intensity"], "intensity")
        if action not in distill.ACTIONS or not 0 <= intensity <= 1 or (action == "hold" and intensity != 0):
            raise StudentContractError("invalid public action/intensity")
        if failure is not None or response_hash is None:
            raise StudentContractError("valid row has failure or missing response hash")
    else:
        if decision is not None:
            raise StudentContractError("failed requests cannot enter training as decisions")
        failure = _identity(failure, "failure_code")
    return {
        "sample_id": sample_id, "latent_state_id": latent, "family_id": family,
        "profile_id": profile, "latent_hash": latent_hash, "view_hash": view_hash,
        "features": features, "status": status, "action": action,
        "intensity": intensity, "failure_code": failure, "response_hash": response_hash,
        "application_attempt_count": application, "technical_retry_count": retries,
    }


def prepare_records(records: Sequence[Mapping[str, Any]], *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Freeze family assignments on all requested groups before failure filtering."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise StudentContractError("seed must be an integer")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence) or not records:
        raise StudentContractError("records must be a nonempty sequence")
    rows = [normalize_record(row) for row in records]
    rows.sort(key=lambda row: (row["family_id"], row["latent_state_id"], row["profile_id"], row["sample_id"]))
    if len({row["sample_id"] for row in rows}) != len(rows):
        raise StudentContractError("duplicate sample_id")
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["latent_state_id"], []).append(row)
    for group in groups.values():
        if len(group) != len(domain.PROFILE_IDS) or {row["profile_id"] for row in group} != set(domain.PROFILE_IDS):
            raise StudentContractError("each latent group must include all four requested views, including failures")
        if len({row["family_id"] for row in group}) != 1 or len({row["latent_hash"] for row in group}) != 1:
            raise StudentContractError("paired views disagree on family/latent identity")
        if any(row["features"][-8:] != group[0]["features"][-8:] for row in group):
            raise StudentContractError("paired views disagree on account state")
        # Overlapping visible information must also describe the same world.
        for index in range(len(INFORMATION_FIELDS)):
            observed = [row["features"][index] for row in group if row["features"][24 + index]]
            if observed and any(value != observed[0] for value in observed):
                raise StudentContractError("paired views disagree on shared information")
    # Placeholders carry no Teacher targets; they freeze the group roster only.
    roster = [{"family_id": row["family_id"], "state_id": row["sample_id"],
               "features": [0.0], "target_probs": [0.0, 1.0, 0.0],
               "intensity_targets": [0.0, 0.0], "intensity_weights": [0.0, 0.0]} for row in rows]
    split = distill.deterministic_group_split(roster, seed=seed, fractions=FRACTIONS)
    assignments = split.family_assignments
    examples: dict[str, list[dict[str, Any]]] = {name: [] for name in PARTITIONS}
    accounting_by_partition: dict[str, Any] = {}
    for row in rows:
        if row["status"] != "valid":
            continue
        action, intensity = row["action"], row["intensity"]
        examples[assignments[row["family_id"]]].append({
            "family_id": row["family_id"], "state_id": row["sample_id"],
            "features": row["features"],
            "target_probs": [float(action == name) for name in distill.ACTIONS],
            "intensity_targets": [intensity if action == name else 0.0 for name in distill.INTENSITY_ACTIONS],
            "intensity_weights": [float(action == name) for name in distill.INTENSITY_ACTIONS],
            "profile_id": row["profile_id"], "latent_state_id": row["latent_state_id"],
        })
    for name in PARTITIONS:
        assigned = [row for row in rows if assignments[row["family_id"]] == name]
        accounting_by_partition[name] = {
            "requested": len(assigned), "valid": len(examples[name]),
            "failed": sum(row["status"] == "failed" for row in assigned),
            "latent_groups": len({row["latent_state_id"] for row in assigned}),
            "families": sum(partition == name for partition in assignments.values()),
        }
    split_payload = {
        "schema_version": SPLIT_SCHEMA_VERSION, "assignment_algorithm_schema": distill.SPLIT_SCHEMA_VERSION,
        "seed": seed, "fractions": list(FRACTIONS),
        "grouping": "explicit family_id when supplied; otherwise latent_state_id",
        "roster_includes_failed_requests": True,
        "family_assignments": dict(sorted(assignments.items())),
        "latent_assignments": {latent: assignments[group[0]["family_id"]] for latent, group in sorted(groups.items())},
        "sample_assignments": {row["sample_id"]: assignments[row["family_id"]] for row in rows},
        "counts": accounting_by_partition,
    }
    split_payload["split_semantic_hash"] = stable_hash(split_payload)
    valid = [row for row in rows if row["status"] == "valid"]
    return {
        "examples_by_partition": examples, "split": split_payload,
        "normalized_records_hash": stable_hash({"schema_version": DATA_SCHEMA_VERSION, "rows": rows}),
        "accounting": {
            "logical_requests": len(rows), "valid_teacher_answers": len(valid),
            "failed_logical_requests": len(rows) - len(valid),
            "physical_attempts": sum(row["application_attempt_count"] for row in rows),
            "technical_retries": sum(row["technical_retry_count"] for row in rows),
            "latent_groups": len(groups),
            "complete_valid_paired_groups": sum(all(row["status"] == "valid" for row in group) for group in groups.values()),
            "human_participants": 0,
            "failure_counts": dict(sorted(Counter(row["failure_code"] for row in rows if row["status"] == "failed").items())),
            "action_counts": {name: sum(row["action"] == name for row in valid) for name in distill.ACTIONS},
            "by_partition": accounting_by_partition,
        },
    }


def load_model(payload: Mapping[str, Any]) -> Any:
    schema = payload.get("schema_version") if isinstance(payload, Mapping) else None
    constructors = {distill.ACTION_PRIOR_SCHEMA_VERSION: distill.ActionPrior,
                    distill.LINEAR_SCHEMA_VERSION: distill.LinearSoftmaxStudent,
                    distill.MLP_SCHEMA_VERSION: distill.TanhMLPStudent}
    if schema not in constructors:
        raise StudentContractError("unsupported serialized model schema")
    model = constructors[schema].from_dict(payload)
    if not isinstance(model, distill.ActionPrior) and model.input_dim != FEATURE_DIM:
        raise StudentContractError("information model input width must be 56")
    return model


def make_predictor(serialized_model: Mapping[str, Any]) -> Callable[..., dict[str, list[float]]]:
    """Load once for a repeated, stdlib-only market inference loop."""
    model = load_model(serialized_model)
    return lambda visible_fields, account_state: model.predict(encode_observation(visible_fields, account_state))


def predict_model(serialized_model: Mapping[str, Any], visible_fields: Mapping[str, Any], account_state: Mapping[str, Any]) -> dict[str, list[float]]:
    return make_predictor(serialized_model)(visible_fields, account_state)


def parameter_count(serialized_model: Mapping[str, Any]) -> int:
    """Stored fitted coefficients, excluding train standardization statistics."""
    model = load_model(serialized_model)
    if isinstance(model, distill.ActionPrior):
        return 5  # 3 class probabilities (sum constrained) + 2 intensities.
    if isinstance(model, distill.LinearSoftmaxStudent):
        return FEATURE_DIM * 5 + 5
    return FEATURE_DIM * model.hidden_dim + model.hidden_dim + model.hidden_dim * 5 + 5


def make_support_checker(reference: Mapping[str, Any]) -> Callable[..., dict[str, Any]]:
    """One-row rectangular support diagnostic; does not certify joint support."""
    fitted = distill.OODReference.from_dict(reference)
    if fitted.feature_dim != FEATURE_DIM:
        raise StudentContractError("information support width must be 56")

    def check(visible_fields: Mapping[str, Any], account_state: Mapping[str, Any]) -> dict[str, Any]:
        features = encode_observation(visible_fields, account_state)
        outside = [FEATURE_NAMES[i] for i, (value, lower, upper) in enumerate(zip(features, fitted.feature_min, fitted.feature_max)) if not lower <= value <= upper]
        return {"outside_train_range": bool(outside), "outside_features": outside,
                "max_abs_z": max(abs(value) for value in fitted.standardizer.transform(features)),
                "joint_support_assessed": False}

    return check


def _numpy_fit(model: Any, rows: list[dict[str, Any]], standardizer: Any, *, epochs: int, np: Any) -> dict[str, list[float]]:
    """Vectorization of existing full-batch Adam; same loss/initialization."""
    model.standardizer = standardizer
    x = np.asarray(standardizer.transform_many(rows), dtype=np.float64)
    targets = np.asarray([row["target_probs"] for row in rows], dtype=np.float64)
    intensity_targets = np.asarray([row["intensity_targets"] for row in rows], dtype=np.float64)
    intensity_weights = np.asarray([row["intensity_weights"] for row in rows], dtype=np.float64)
    total_weight = float(intensity_weights.sum())
    names = ["w_action", "b_action", "w_intensity", "b_intensity"]
    is_mlp = isinstance(model, distill.TanhMLPStudent)
    if is_mlp:
        names += ["w1", "b1"]
    parameters = {name: np.asarray(getattr(model, name), dtype=np.float64) for name in names}
    moments = {name: np.zeros_like(value) for name, value in parameters.items()}
    variances = {name: np.zeros_like(value) for name, value in parameters.items()}
    history = []
    # Explicit contraction avoids BLAS thread/reduction variability and spurious
    # floating-status warnings observed with macOS NumPy 2.0 matmul kernels.
    def multiply(left: Any, right: Any) -> Any:
        return np.einsum("ij,jk->ik", left, right, optimize=False)

    for step in range(1, epochs + 1):
        hidden = np.tanh(multiply(x, parameters["w1"].T) + parameters["b1"]) if is_mlp else x
        logits = multiply(hidden, parameters["w_action"].T) + parameters["b_action"]
        probabilities = np.exp(logits - logits.max(axis=1, keepdims=True))
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        intensity_logits = multiply(hidden, parameters["w_intensity"].T) + parameters["b_intensity"]
        exponential = np.exp(-np.abs(intensity_logits))
        intensities = np.where(intensity_logits >= 0, 1 / (1 + exponential), exponential / (1 + exponential))
        action_delta = (probabilities - targets) / len(rows)
        intensity_delta = np.zeros_like(intensities)
        intensity_loss = 0.0
        if total_weight:
            intensity_delta = 2 * intensity_weights * (intensities - intensity_targets) / total_weight * intensities * (1 - intensities)
            intensity_loss = float((intensity_weights * (intensities - intensity_targets) ** 2).sum() / total_weight)
        gradients = {
            "w_action": multiply(action_delta.T, hidden), "b_action": action_delta.sum(axis=0),
            "w_intensity": multiply(intensity_delta.T, hidden), "b_intensity": intensity_delta.sum(axis=0),
        }
        if is_mlp:
            hidden_delta = (multiply(action_delta, parameters["w_action"]) + multiply(intensity_delta, parameters["w_intensity"])) * (1 - hidden ** 2)
            gradients["w1"], gradients["b1"] = multiply(hidden_delta.T, x), hidden_delta.sum(axis=0)
        regularization = 0.0
        for name in names:
            if name.startswith("w"):
                gradients[name] += 1e-4 * parameters[name]
                regularization += 0.5e-4 * float((parameters[name] ** 2).sum())
        loss = float(-(targets * np.log(np.maximum(probabilities, 1e-15))).sum() / len(rows)) + intensity_loss + regularization
        if not math.isfinite(loss):
            raise FloatingPointError("non-finite information student training loss")
        history.append(loss)
        for name in names:
            moments[name] = 0.9 * moments[name] + 0.1 * gradients[name]
            variances[name] = 0.999 * variances[name] + 0.001 * gradients[name] ** 2
            parameters[name] -= 0.02 * (moments[name] / (1 - 0.9 ** step)) / (np.sqrt(variances[name] / (1 - 0.999 ** step)) + 1e-8)
    for name in names:
        setattr(model, name, parameters[name].tolist())
    model.training_config = {
        "optimizer": "full_batch_adam", "epochs": epochs, "learning_rate": 0.02,
        "intensity_loss_weight": 1.0, "l2": 1e-4,
        "train_observations_hash": distill.canonical_observations_hash(rows, stratum_key=None),
        "backend": "numpy", "backend_version": np.__version__, "matrix_kernel": "einsum_no_optimization",
    }
    return {"loss": history}


def _metrics(model: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    predictions = model.predict_many(rows)
    result = distill.evaluate_predictions(rows, predictions)
    confusion = [[0] * 3 for _ in range(3)]
    for row, prediction in zip(rows, predictions):
        truth = max(range(3), key=lambda i: row["target_probs"][i])
        predicted = max(range(3), key=lambda i: prediction["action_probs"][i])
        confusion[truth][predicted] += 1
    f1 = []
    for i in range(3):
        denominator = sum(confusion[i]) + sum(confusion[j][i] for j in range(3))
        f1.append(2 * confusion[i][i] / denominator if denominator else 0.0)
    result.update({"confusion_matrix_true_rows_predicted_columns": confusion,
                   "action_order": list(distill.ACTIONS), "macro_f1": sum(f1) / 3,
                   "per_action_f1": dict(zip(distill.ACTIONS, f1))})
    by_profile = {}
    for profile in domain.PROFILE_IDS:
        indexes = [i for i, row in enumerate(rows) if row["profile_id"] == profile]
        if indexes:
            by_profile[profile] = distill.evaluate_predictions([rows[i] for i in indexes], [predictions[i] for i in indexes])
    result["by_profile"] = by_profile
    return result


def train_models(records: Sequence[Mapping[str, Any]], *, seed: int = DEFAULT_SEED, epochs: int = DEFAULT_EPOCHS, hidden_dim: int = 16, backend: str = "auto") -> dict[str, Any]:
    """Fit three candidates; select by validation CE; then evaluate frozen test.

    Selection is (validation cross-entropy, candidate order prior/linear/MLP).
    No test metric changes model selection or training. All rows have K=1 hard
    labels; predicted probabilities are learned scores, not measured human or
    within-state Teacher distributions. Repeated model development after seeing
    this test requires a newly frozen evaluation source.
    """
    _integer(epochs, "epochs", 1)
    _integer(hidden_dim, "hidden_dim", 1)
    if backend not in ("auto", "numpy", "python"):
        raise StudentContractError("backend must be auto, numpy, or python")
    np = None
    if backend in ("auto", "numpy"):
        try:
            import numpy as np
        except ImportError:
            if backend == "numpy":
                raise StudentContractError("numpy backend requested but NumPy is unavailable") from None
    actual_backend = "numpy" if np is not None else "python"
    prepared = prepare_records(records, seed=seed)
    examples = prepared["examples_by_partition"]
    if any(not examples[name] for name in PARTITIONS):
        raise StudentContractError("train, validation, and test must each contain valid observations")
    training_rows = examples["train"]
    standardizer = distill.Standardizer.fit(training_rows)
    models = {
        "prior": distill.ActionPrior.fit(training_rows),
        "linear": distill.LinearSoftmaxStudent(FEATURE_DIM, seed=seed),
        "mlp": distill.TanhMLPStudent(FEATURE_DIM, hidden_dim=hidden_dim, seed=seed),
    }
    histories = {}
    for name in ("linear", "mlp"):
        if np is None:
            histories[name] = models[name].fit(training_rows, standardizer, epochs=epochs)
            models[name].training_config.update({"backend": "python", "backend_version": "stdlib"})
        else:
            histories[name] = _numpy_fit(models[name], training_rows, standardizer, epochs=epochs, np=np)
    evaluation = {name: {partition: _metrics(model, examples[partition]) for partition in ("train", "validation")} for name, model in models.items()}
    selected = min(MODEL_NAMES, key=lambda name: (evaluation[name]["validation"]["action_cross_entropy"], MODEL_NAMES.index(name)))
    # Freeze this record before any test prediction/evaluation is made.
    selection = {"criterion": "lowest_validation_action_cross_entropy", "tie_break": list(MODEL_NAMES),
                 "selected_model": selected,
                 "validation_scores": {name: evaluation[name]["validation"]["action_cross_entropy"] for name in MODEL_NAMES},
                 "test_used_for_selection": False}
    for name, model in models.items():
        evaluation[name]["test"] = _metrics(model, examples["test"])
    serialized = {name: model.to_dict() for name, model in models.items()}
    reference = distill.build_ood_reference(training_rows, standardizer=standardizer)
    return {
        "schema_version": SCHEMA_VERSION, "encoder": encoder_descriptor(),
        "models": serialized, "selected_model": selected, "selection": selection,
        "split": prepared["split"], "accounting": prepared["accounting"], "evaluation": evaluation,
        "ood_reference": reference.to_dict(),
        "ood_diagnostics": {name: distill.ood_diagnostics(examples[name], reference) for name in PARTITIONS},
        "training": {"seed": seed, "epochs": epochs, "hidden_dim": hidden_dim,
                     "backend": actual_backend, "backend_version": np.__version__ if np is not None else "stdlib",
                     "parameter_counts": {name: parameter_count(payload) for name, payload in serialized.items()},
                     "parameter_count_excludes_standardizer_statistics": True, "loss_history": histories},
        "provenance": {
            "normalized_records_hash_schema": DATA_SCHEMA_VERSION,
            "normalized_records_semantic_hash": prepared["normalized_records_hash"],
            "encoder_semantic_hash": stable_hash(encoder_descriptor()),
            "model_serialization_hashes": {name: stable_hash(payload) for name, payload in serialized.items()},
            "source_file_byte_hash": "supplied_by_managed_caller; not inferred from records",
            "selection_completed_before_test_evaluation": True,
            "same_backend_repeatability_only": True, "private_fields_copied": False,
            "science_status": "new exploratory distillation; no human validation; K=1 per state-view",
        },
    }


__all__ = ["SCHEMA_VERSION", "ENCODER_SCHEMA_VERSION", "DATA_SCHEMA_VERSION", "SOURCE_SCHEMA_VERSION",
           "SPLIT_SCHEMA_VERSION", "INFORMATION_FIELDS", "FEATURE_NAMES", "FEATURE_DIM", "DEFAULT_SEED",
           "DEFAULT_EPOCHS", "PARTITIONS", "MODEL_NAMES", "StudentContractError", "stable_hash",
           "encoder_descriptor", "encode_observation", "normalize_record", "prepare_records",
           "load_model", "make_predictor", "predict_model", "parameter_count", "make_support_checker", "train_models"]
