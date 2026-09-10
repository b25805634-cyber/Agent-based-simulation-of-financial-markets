"""Train-only, exact-support sizing distributions; no I/O or market changes.

The managed caller authenticates original source bytes. Test payloads are
excluded before ingestion. The old action model and MLP representation remain
frozen; only the conditional distribution of buy/sell intensity is fitted.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from time import perf_counter
from typing import Any, Callable

from nmsim import information_learning_curve as curves
from nmsim import information_student as student
from nmsim import v2_distillation as distill


SCHEMA_VERSION = "information-intensity-distribution-study/1.0"
MODEL_SCHEMA_VERSION = "frozen-mlp-exact-support-softmax/1.0"
CANDIDATES = ("empirical", "conditional_softmax")
MAX_SUPPORT = 256
MAX_FIT_MULTIPLIES = 2_000_000_000
MAX_PYTHON_MULTIPLIES = 50_000_000
DistributionContractError = student.StudentContractError


def validate_distribution(distribution: Mapping[str, Any]) -> dict[str, list[float]]:
    """Validate a sorted finite categorical law on [0, 1], without binning."""
    if not isinstance(distribution, Mapping) or set(distribution) != {"support", "probabilities"}:
        raise DistributionContractError("distribution needs exactly support and probabilities")
    values: dict[str, list[float]] = {}
    for name in ("support", "probabilities"):
        raw = distribution[name]
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or not raw:
            raise DistributionContractError(f"{name} must be a nonempty sequence")
        values[name] = [student._number(value, name) for value in raw]
    support, probabilities = values["support"], values["probabilities"]
    if len(support) != len(probabilities) or len(support) > MAX_SUPPORT:
        raise DistributionContractError("distribution shape or exact-support capacity exceeded")
    if any(not 0 <= value <= 1 for value in support + probabilities):
        raise DistributionContractError("distribution entries must lie in [0, 1]")
    if any(left >= right for left, right in zip(support, support[1:])):
        raise DistributionContractError("support must be strictly increasing, without duplicate atoms")
    if not math.isclose(math.fsum(probabilities), 1.0, rel_tol=0, abs_tol=1e-10):
        raise DistributionContractError("distribution probabilities must sum to one")
    return values


def distribution_mean(distribution: Mapping[str, Any]) -> float:
    law = validate_distribution(distribution)
    return math.fsum(value * probability for value, probability in zip(law["support"], law["probabilities"]))


def quantile_sample(distribution: Mapping[str, Any], u: float) -> float:
    """Inverse CDF; u=1 uses the last positive atom for float RNG rounding."""
    law = validate_distribution(distribution)
    uniform = student._number(u, "u")
    if not 0 <= uniform <= 1:
        raise DistributionContractError("u must lie in [0, 1]")
    if uniform == 1:
        return next(value for value, probability in reversed(list(zip(law["support"], law["probabilities"]))) if probability > 0)
    cumulative, last_positive = 0.0, None
    for value, probability in zip(law["support"], law["probabilities"]):
        cumulative += probability
        if probability > 0:
            last_positive = value
            if uniform < cumulative:
                return value
    # A distribution with sum within 1e-10 of one may leave a rounding sliver.
    assert last_positive is not None
    return last_positive


def distribution_scores(distribution: Mapping[str, Any], observation: float) -> dict[str, float]:
    """CRPS is proper for arbitrary real y; W1-to-point is descriptive only.

    CRPS = E|X-y| - E|X-X'|/2. The second term uses sorted support and prefix
    sums, so an off-support observation is handled exactly, without a fake
    zero-density penalty or snapping its value to a neighboring atom.
    """
    law = validate_distribution(distribution)
    y = student._number(observation, "observation")
    if not 0 <= y <= 1:
        raise DistributionContractError("intensity observation must lie in [0, 1]")
    distance = math.fsum(probability * abs(value - y) for value, probability in zip(law["support"], law["probabilities"]))
    probability_before = weighted_before = pair_half = 0.0
    for value, probability in zip(law["support"], law["probabilities"]):
        pair_half += probability * (value * probability_before - weighted_before)
        probability_before += probability
        weighted_before += probability * value
    return {"crps": max(0.0, distance - pair_half), "wasserstein1_to_point": distance}


def _softmax(logits: Sequence[float]) -> list[float]:
    if any(not math.isfinite(value) for value in logits):
        raise DistributionContractError("conditional logits must remain finite")
    largest = max(logits)
    exponentials = [math.exp(value - largest) for value in logits]
    total = math.fsum(exponentials)
    return [value / total for value in exponentials]


def _hidden(model: Any, features: Sequence[float]) -> list[float]:
    standardized = model.standardizer.transform(features)
    return [math.tanh(bias + sum(weight * value for weight, value in zip(weights, standardized)))
            for weights, bias in zip(model.w1, model.b1)]


def _conditional_prediction(payload: Mapping[str, Any], hidden: Sequence[float]) -> dict[str, list[float]]:
    probabilities = _softmax([bias + sum(weight * value for weight, value in zip(weights, hidden))
                              for weights, bias in zip(payload["weights"], payload["biases"])])
    return {"support": list(payload["support"]), "probabilities": probabilities}


def _fit_softmax(features: list[list[float]], labels: list[int], empirical: Mapping[str, Any], *,
                 epochs: int, np: Any) -> tuple[dict[str, Any], list[float]]:
    """Fixed full-batch categorical CE + weight L2; no early stopping."""
    count, width, classes = len(features), len(features[0]), len(empirical["support"])
    biases = [math.log(value) for value in empirical["probabilities"]]
    weights = [[0.0] * width for _ in range(classes)]
    history = []
    if np is not None:
        x = np.asarray(features, dtype=np.float64)
        target = np.asarray(labels, dtype=np.int64)
        w, b = np.asarray(weights, dtype=np.float64), np.asarray(biases, dtype=np.float64)
        mw, vw, mb, vb = np.zeros_like(w), np.zeros_like(w), np.zeros_like(b), np.zeros_like(b)
        for step in range(1, epochs + 1):
            logits = np.einsum("ij,kj->ik", x, w, optimize=False) + b
            maximum = logits.max(axis=1, keepdims=True)
            exponentials = np.exp(logits - maximum)
            totals = exponentials.sum(axis=1, keepdims=True)
            probabilities = exponentials / totals
            loss = float((maximum[:, 0] + np.log(totals[:, 0]) - logits[np.arange(count), target]).mean())
            loss += 0.5e-4 * float((w * w).sum())
            delta = probabilities
            delta[np.arange(count), target] -= 1
            delta /= count
            gw = np.einsum("ik,ij->kj", delta, x, optimize=False) + 1e-4 * w
            gb = delta.sum(axis=0)
            mw, vw = 0.9 * mw + 0.1 * gw, 0.999 * vw + 0.001 * gw * gw
            mb, vb = 0.9 * mb + 0.1 * gb, 0.999 * vb + 0.001 * gb * gb
            w -= 0.02 * (mw / (1 - 0.9 ** step)) / (np.sqrt(vw / (1 - 0.999 ** step)) + 1e-8)
            b -= 0.02 * (mb / (1 - 0.9 ** step)) / (np.sqrt(vb / (1 - 0.999 ** step)) + 1e-8)
            if not math.isfinite(loss):
                raise DistributionContractError("non-finite distribution training loss")
            history.append(loss)
        weights, biases = w.tolist(), b.tolist()
    else:
        mw, vw = [[0.0] * width for _ in range(classes)], [[0.0] * width for _ in range(classes)]
        mb, vb = [0.0] * classes, [0.0] * classes
        for step in range(1, epochs + 1):
            gw, gb = [[0.0] * width for _ in range(classes)], [0.0] * classes
            loss = 0.0
            for features_row, label in zip(features, labels):
                logits = [bias + sum(weight * value for weight, value in zip(row, features_row)) for row, bias in zip(weights, biases)]
                probabilities = _softmax(logits)
                largest = max(logits)
                loss += (largest + math.log(math.fsum(math.exp(value - largest) for value in logits)) - logits[label]) / count
                for index, probability in enumerate(probabilities):
                    delta = (probability - float(index == label)) / count
                    gb[index] += delta
                    for column, value in enumerate(features_row):
                        gw[index][column] += delta * value
            for index in range(classes):
                for column in range(width):
                    weight = weights[index][column]
                    loss += 0.5e-4 * weight * weight
                    gradient = gw[index][column] + 1e-4 * weight
                    mw[index][column] = 0.9 * mw[index][column] + 0.1 * gradient
                    vw[index][column] = 0.999 * vw[index][column] + 0.001 * gradient * gradient
                    weights[index][column] -= 0.02 * (mw[index][column] / (1 - 0.9 ** step)) / (math.sqrt(vw[index][column] / (1 - 0.999 ** step)) + 1e-8)
                mb[index] = 0.9 * mb[index] + 0.1 * gb[index]
                vb[index] = 0.999 * vb[index] + 0.001 * gb[index] ** 2
                biases[index] -= 0.02 * (mb[index] / (1 - 0.9 ** step)) / (math.sqrt(vb[index] / (1 - 0.999 ** step)) + 1e-8)
            if not math.isfinite(loss):
                raise DistributionContractError("non-finite distribution training loss")
            history.append(loss)
    payload = {"schema_version": MODEL_SCHEMA_VERSION, "support": list(empirical["support"]),
               "input_dim": width, "weights": weights, "biases": biases,
               "training_count": count, "parameter_count": classes * (width + 1)}
    return payload, history


def _marginal_wasserstein(predictions: list[dict[str, Any]], observations: list[float]) -> float:
    """Exact 1-Wasserstein between the mean predictive CDF and empirical CDF."""
    masses: dict[float, float] = {}
    count = len(observations)
    for prediction in predictions:
        for value, probability in zip(prediction["support"], prediction["probabilities"]):
            masses[value] = masses.get(value, 0.0) + probability / count
    for value in observations:
        masses[value] = masses.get(value, 0.0) - 1 / count
    distance = cumulative = 0.0
    previous = min(masses)
    for value in sorted(masses):
        distance += abs(cumulative) * (value - previous)
        cumulative += masses[value]
        previous = value
    return distance


def _evaluation(predictions: list[dict[str, Any]], observations: list[float]) -> dict[str, Any]:
    count = len(observations)
    if not count:
        return {"n": 0, "crps": None, "wasserstein1_to_point": None,
                "marginal_wasserstein1": None, "mean_mae": None, "mean_bias": None,
                "observed_mean": None, "predicted_mean": None, "off_support_observations": 0,
                "endpoint": {}}
    predictions = [validate_distribution(value) for value in predictions]
    scores = [distribution_scores(prediction, value) for prediction, value in zip(predictions, observations)]
    means = [distribution_mean(prediction) for prediction in predictions]
    result: dict[str, Any] = {"n": count,
        "crps": math.fsum(score["crps"] for score in scores) / count,
        "wasserstein1_to_point": math.fsum(score["wasserstein1_to_point"] for score in scores) / count,
        "marginal_wasserstein1": _marginal_wasserstein(predictions, observations),
        "mean_mae": math.fsum(abs(mean - value) for mean, value in zip(means, observations)) / count,
        "mean_bias": math.fsum(mean - value for mean, value in zip(means, observations)) / count,
        "observed_mean": math.fsum(observations) / count,
        "predicted_mean": math.fsum(means) / count,
        "off_support_observations": sum(value not in prediction["support"] for prediction, value in zip(predictions, observations)),
        "endpoint": {}}
    for endpoint in (0.0, 1.0):
        probabilities = [math.fsum(probability for value, probability in zip(prediction["support"], prediction["probabilities"]) if value == endpoint) for prediction in predictions]
        targets = [float(value == endpoint) for value in observations]
        bins = []
        for index in range(10):
            pairs = [(probability, target) for probability, target in zip(probabilities, targets) if min(int(probability * 10), 9) == index]
            bins.append({"lower": index / 10, "upper": (index + 1) / 10, "n": len(pairs),
                         "predicted_probability": math.fsum(row[0] for row in pairs) / len(pairs) if pairs else None,
                         "observed_fraction": math.fsum(row[1] for row in pairs) / len(pairs) if pairs else None})
        result["endpoint"][str(int(endpoint))] = {
            "observed_count": int(sum(targets)), "observed_fraction": sum(targets) / count,
            "predicted_probability": math.fsum(probabilities) / count,
            "brier": math.fsum((probability - target) ** 2 for probability, target in zip(probabilities, targets)) / count,
            "reliability_bins": bins,
            "ece": math.fsum(row["n"] * abs(row["predicted_probability"] - row["observed_fraction"]) for row in bins if row["n"]) / count,
        }
    return result


def _source_models(original_study: Mapping[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    if not isinstance(original_study, Mapping) or original_study.get("schema_version") != student.SCHEMA_VERSION:
        raise DistributionContractError("original study schema is required")
    if original_study.get("encoder") != student.encoder_descriptor():
        raise DistributionContractError("original encoder identity mismatch")
    models, provenance = original_study.get("models"), original_study.get("provenance")
    selected = original_study.get("selected_model")
    if not isinstance(models, Mapping) or not isinstance(provenance, Mapping) or selected not in student.MODEL_NAMES:
        raise DistributionContractError("original fitted model identity is required")
    hashes = provenance.get("model_serialization_hashes")
    if not isinstance(hashes, Mapping):
        raise DistributionContractError("original model serialization hashes are required")
    for name in {selected, "mlp"}:
        if name not in models or student.stable_hash(models[name]) != hashes.get(name):
            raise DistributionContractError("original model serialization hash mismatch")
    action_model, hidden_model = student.load_model(models[selected]), student.load_model(models["mlp"])
    if not isinstance(hidden_model, distill.TanhMLPStudent):
        raise DistributionContractError("frozen original MLP representation is required")
    if provenance.get("normalized_records_hash_schema") != student.DATA_SCHEMA_VERSION:
        raise DistributionContractError("original normalized-record identity is required")
    identity = {"original_normalized_records_hash_schema": student.DATA_SCHEMA_VERSION,
                "original_normalized_records_semantic_hash": student._sha(provenance.get("normalized_records_semantic_hash"), "original_normalized_records_semantic_hash"),
                "action_model_name": selected, "action_model_serialization_semantic_hash": hashes[selected],
                "representation_model_name": "mlp", "representation_model_serialization_semantic_hash": hashes["mlp"],
                "encoder_semantic_hash": student.stable_hash(student.encoder_descriptor())}
    if not isinstance(original_study.get("selection"), Mapping) or original_study["selection"].get("selected_model") != selected:
        raise DistributionContractError("original selected action model disagrees with selection record")
    return action_model, hidden_model, identity


def fit_intensity_distributions(records: Sequence[Mapping[str, Any]], original_study: Mapping[str, Any], *,
                                epochs: int = 120, backend: str = "auto") -> dict[str, Any]:
    """Fit exact train support; select each action's law by validation CRPS.

    The pure library uses no file, Provider, network, or run context. The managed
    entrypoint must authenticate the original records and study artifacts.
    """
    student._integer(epochs, "epochs", 1)
    if backend not in ("auto", "numpy", "python"):
        raise DistributionContractError("backend must be auto, numpy, or python")
    np = None
    if backend in ("auto", "numpy"):
        try:
            import numpy as np
        except ImportError:
            if backend == "numpy":
                raise DistributionContractError("numpy backend requested but NumPy is unavailable") from None
    started = perf_counter()
    split, seed = curves._original_split(original_study)
    action_model, hidden_model, source_identity = _source_models(original_study)
    prepared = curves._training_and_validation(records, split, seed)
    examples = prepared["examples"]
    train_hash = distill.canonical_observations_hash(examples["train"], stratum_key=None)
    if hidden_model.training_config.get("train_observations_hash") != train_hash:
        raise DistributionContractError("training observations differ from the frozen original MLP")
    if hidden_model.standardizer.to_dict() != distill.Standardizer.fit(examples["train"]).to_dict():
        raise DistributionContractError("frozen MLP standardizer is not fitted on the original training rows")
    source_identity.update({
        "original_full_source_identity_is_reference_not_recomputed_from_test_payloads": True,
        "non_test_normalized_records_hash_schema": student.DATA_SCHEMA_VERSION,
        "non_test_normalized_records_semantic_hash": prepared["non_test_normalized_records_semantic_hash"],
        "identity_roster_hash_schema": curves.IDENTITY_ROSTER_SCHEMA_VERSION,
        "identity_roster_semantic_hash": prepared["identity_roster_semantic_hash"],
        "training_observations_hash_schema": distill.OBSERVATION_HASH_SCHEMA_VERSION,
        "training_observations_semantic_hash": train_hash,
        "source_file_byte_hash": "must_be_authenticated_by_managed_caller; not_inferred_from_records",
    })
    hidden = {partition: [_hidden(hidden_model, row["features"]) for row in rows] for partition, rows in examples.items()}
    models: dict[str, dict[str, Any]] = {name: {} for name in CANDIDATES}
    evaluations: dict[str, Any] = {name: {partition: {} for partition in examples} for name in (*CANDIDATES, "legacy_mean")}
    support_records, histories, timings = {}, {}, {}
    selected: dict[str, str] = {}
    validation_scores: dict[str, Any] = {}
    fit_work = 0
    for action_index, action in enumerate(distill.INTENSITY_ACTIONS):
        indices = {partition: [index for index, row in enumerate(rows) if row["intensity_weights"][action_index] == 1]
                   for partition, rows in examples.items()}
        labels = {partition: [examples[partition][index]["intensity_targets"][action_index] for index in positions]
                  for partition, positions in indices.items()}
        if not labels["train"]:
            raise DistributionContractError(f"no original training {action} labels; cannot invent an intensity distribution")
        support = sorted(set(labels["train"]))
        if len(support) > MAX_SUPPORT:
            raise DistributionContractError("more than 256 exact observed atoms; explicit new design required, no automatic binning")
        fit_work += epochs * len(labels["train"]) * len(support) * hidden_model.hidden_dim
        capacity = MAX_FIT_MULTIPLIES if np is not None else MAX_PYTHON_MULTIPLIES
        if fit_work > capacity:
            raise DistributionContractError("fitting work bound exceeded; explicitly use NumPy or a smaller frozen epoch budget")
        counts = [labels["train"].count(value) for value in support]
        empirical = validate_distribution({"support": support, "probabilities": [count / len(labels["train"]) for count in counts]})
        models["empirical"][action] = empirical
        start = perf_counter()
        target_index = {value: index for index, value in enumerate(support)}
        conditional, history = _fit_softmax([hidden["train"][index] for index in indices["train"]],
                                           [target_index[value] for value in labels["train"]], empirical,
                                           epochs=epochs, np=np)
        timings[action] = perf_counter() - start
        models["conditional_softmax"][action], histories[action] = conditional, history
        support_records[action] = {"support": support, "counts": counts, "training_rows": len(labels["train"]),
                                   "training_sample_ids": [examples["train"][index]["state_id"] for index in indices["train"]],
                                   "support_semantic_hash": student.stable_hash({"action": action, "support": support, "counts": counts})}
        for partition, positions in indices.items():
            predictions = {
                "empirical": [empirical for _ in positions],
                "conditional_softmax": [_conditional_prediction(conditional, hidden[partition][index]) for index in positions],
                "legacy_mean": [{"support": [action_model.predict(examples[partition][index]["features"])["intensities"][action_index]],
                                 "probabilities": [1.0]} for index in positions],
            }
            for candidate, laws in predictions.items():
                evaluations[candidate][partition][action] = _evaluation(laws, labels[partition])
        validation_scores[action] = {name: evaluations[name]["validation"][action]["crps"] for name in CANDIDATES}
        selected[action] = (min(CANDIDATES, key=lambda name: (validation_scores[action][name], CANDIDATES.index(name)))
                            if labels["validation"] else "empirical")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "encoder": student.encoder_descriptor(), "source_identity": source_identity,
        "split": split, "models": models, "exact_training_support": support_records,
        "parameter_counts": {
            "empirical": {action: len(models["empirical"][action]["support"]) for action in distill.INTENSITY_ACTIONS},
            "conditional_softmax": {action: models["conditional_softmax"][action]["parameter_count"] for action in distill.INTENSITY_ACTIONS},
            "frozen_representation_model": student.parameter_count(original_study["models"]["mlp"]),
            "frozen_action_model": student.parameter_count(original_study["models"][original_study["selected_model"]]),
            "head_counts_exclude_reused_frozen_models_and_standardizer": True,
        },
        "model_serialization_semantic_hashes": {name: student.stable_hash(payload) for name, payload in models.items()},
        "selection": {"criterion": "lowest_original_validation_conditional_crps_per_action",
                      "selected_by_action": selected, "candidate_order_for_ties": list(CANDIDATES),
                      "validation_scores": validation_scores, "empty_validation_action_policy": "empirical",
                      "legacy_mean_is_diagnostic_only": True, "test_used_for_selection": False},
        "training": {"epochs": epochs, "seed_of_frozen_representation": seed,
                     "representation": "original_frozen_mlp_hidden_tanh", "hidden_dim": hidden_model.hidden_dim,
                     "action_model_changed": False, "representation_updated": False,
                     "support_policy": "exact_action_conditional_train_observations_only",
                     "optimizer": "full_batch_adam", "objective": "categorical_cross_entropy_plus_weight_l2",
                     "learning_rate": 0.02, "beta1": 0.9, "beta2": 0.999, "epsilon": 1e-8, "l2": 1e-4,
                     "initialization": "zero_weights_and_log_empirical_probability_biases", "early_stopping": False,
                     "backend": "numpy" if np is not None else "python", "backend_version": np.__version__ if np is not None else "stdlib",
                     "max_support": MAX_SUPPORT, "estimated_fit_multiplies": fit_work,
                     "fit_multiply_limit": MAX_FIT_MULTIPLIES if np is not None else MAX_PYTHON_MULTIPLIES,
                     "loss_history": histories, "valid_rows": len(examples["train"]),
                     "sample_ids": [row["state_id"] for row in examples["train"]],
                     "family_ids": sorted({row["family_id"] for row in examples["train"]})},
        "validation": {"valid_rows": len(examples["validation"]),
                       "sample_ids": [row["state_id"] for row in examples["validation"]],
                       "family_ids": sorted({row["family_id"] for row in examples["validation"]}),
                       "observations_hash_schema": distill.OBSERVATION_HASH_SCHEMA_VERSION,
                       "observations_semantic_hash": distill.canonical_observations_hash(examples["validation"], stratum_key=None)},
        "test_exclusion": {"excluded_requested_rows": split["counts"]["test"]["requested"],
                           "excluded_sample_ids": sorted(sample for sample, partition in split["sample_assignments"].items() if partition == "test"),
                           "labels_accessed": False, "observations_accessed": False, "predictions_computed": 0,
                           "used_for_fitting": False, "used_for_selection": False, "metadata_only_roster_validation": True},
        "evaluation": evaluations,
        "interpretation": [
            "New optional sizing distribution; old action probabilities, old intensities, market integer rounding and old defaults are unchanged.",
            "CRPS is the proper continuous-observation selection score. W1-to-point and marginal W1 are descriptive, not proper conditional scores.",
            "Exact zero or one receives probability only if that exact value occurred in this action's original training partition.",
            "Validation observations outside train support are scored at their actual continuous value, not snapped or assigned artificial infinite CE.",
            "K=1 labels do not identify calibrated human or within-state Teacher randomness. Distribution fitting is exploratory generalization across states.",
            "Conditional-mean rollout is a paired null for stochastic-size rollout; empirical laws are the feature-blind distribution control.",
            "Original test was previously disclosed and is not used here. Fresh human and closed-loop evaluation are still required.",
        ],
    }
    result["study_semantic_hash"] = student.stable_hash(result)
    result["runtime_information"] = {"informational_only": True, "excluded_from_study_semantic_hash": True,
                                     "fit_seconds_by_action": timings, "total_seconds": perf_counter() - started}
    return result


def make_distribution_predictor(study: Mapping[str, Any], original_study: Mapping[str, Any], *,
                                candidate: str = "selected") -> Callable[..., dict[str, Any]]:
    """Load once; stdlib inference retains both old scores and new sizing laws."""
    if candidate not in (*CANDIDATES, "selected"):
        raise DistributionContractError("candidate must be selected, empirical, or conditional_softmax")
    if not isinstance(study, Mapping) or study.get("schema_version") != SCHEMA_VERSION:
        raise DistributionContractError("unsupported intensity distribution study schema")
    payload = deepcopy(dict(study))
    expected_hash = student._sha(payload.pop("study_semantic_hash", None), "study_semantic_hash")
    payload.pop("runtime_information", None)
    if student.stable_hash(payload) != expected_hash:
        raise DistributionContractError("intensity distribution study semantic hash mismatch")
    action_model, hidden_model, identity = _source_models(original_study)
    source_identity = payload.get("source_identity")
    if not isinstance(source_identity, Mapping) or any(source_identity.get(key) != value for key, value in identity.items()):
        raise DistributionContractError("distribution study is bound to another original Student")
    split, _seed = curves._original_split(original_study)
    if payload.get("split") != split:
        raise DistributionContractError("distribution split differs from the original frozen split")
    if payload.get("encoder") != student.encoder_descriptor():
        raise DistributionContractError("distribution encoder identity mismatch")
    if source_identity.get("training_observations_semantic_hash") != hidden_model.training_config.get("train_observations_hash"):
        raise DistributionContractError("distribution training identity differs from original MLP")
    laws = payload.get("models")
    selection = payload.get("selection")
    if not isinstance(laws, Mapping) or set(laws) != set(CANDIDATES) or not isinstance(selection, Mapping):
        raise DistributionContractError("distribution model candidates and selection are required")
    choices = selection.get("selected_by_action")
    if not isinstance(choices, Mapping) or set(choices) != set(distill.INTENSITY_ACTIONS) or any(value not in CANDIDATES for value in choices.values()):
        raise DistributionContractError("invalid action-conditional selection")
    for name in CANDIDATES:
        if not isinstance(laws[name], Mapping) or set(laws[name]) != set(distill.INTENSITY_ACTIONS):
            raise DistributionContractError("each candidate must retain both action distributions")
        if student.stable_hash(laws[name]) != payload.get("model_serialization_semantic_hashes", {}).get(name):
            raise DistributionContractError("distribution model serialization hash mismatch")
    for action in distill.INTENSITY_ACTIONS:
        empirical = validate_distribution(laws["empirical"][action])
        support_record = payload.get("exact_training_support", {}).get(action)
        if not isinstance(support_record, Mapping) or support_record.get("support") != empirical["support"]:
            raise DistributionContractError("distribution support differs from its training record")
        counts = support_record.get("counts")
        if not isinstance(counts, list) or len(counts) != len(empirical["support"]) or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in counts):
            raise DistributionContractError("exact support must have positive observed training counts")
        if support_record.get("training_rows") != sum(counts) or empirical["probabilities"] != [value / sum(counts) for value in counts]:
            raise DistributionContractError("empirical distribution differs from exact observed counts")
        if support_record.get("support_semantic_hash") != student.stable_hash({"action": action, "support": empirical["support"], "counts": counts}):
            raise DistributionContractError("exact support semantic hash mismatch")
        model = laws["conditional_softmax"][action]
        if not isinstance(model, Mapping) or model.get("schema_version") != MODEL_SCHEMA_VERSION or model.get("input_dim") != hidden_model.hidden_dim or model.get("support") != empirical["support"]:
            raise DistributionContractError("conditional model representation/support mismatch")
        classes = len(empirical["support"])
        if model.get("parameter_count") != classes * (hidden_model.hidden_dim + 1):
            raise DistributionContractError("conditional model parameter count mismatch")
        if not isinstance(model.get("weights"), list) or len(model["weights"]) != classes or not isinstance(model.get("biases"), list) or len(model["biases"]) != classes:
            raise DistributionContractError("conditional model has invalid parameter shape")
        for row in model["weights"]:
            if not isinstance(row, list) or len(row) != hidden_model.hidden_dim:
                raise DistributionContractError("conditional model has invalid parameter shape")
            for value in row:
                student._number(value, "weight")
        for value in model["biases"]:
            student._number(value, "bias")
        validate_distribution(_conditional_prediction(model, [0.0] * hidden_model.hidden_dim))

    def predict(visible_fields: Mapping[str, Any], account_state: Mapping[str, Any]) -> dict[str, Any]:
        features = student.encode_observation(visible_fields, account_state)
        original_prediction = action_model.predict(features)
        hidden_features = _hidden(hidden_model, features)
        distributions = {}
        for action in distill.INTENSITY_ACTIONS:
            chosen = choices[action] if candidate == "selected" else candidate
            distributions[action] = (deepcopy(laws[chosen][action]) if chosen == "empirical"
                                     else _conditional_prediction(laws[chosen][action], hidden_features))
        return {**original_prediction, "intensity_distributions": distributions,
                "intensity_means": [distribution_mean(distributions[action]) for action in distill.INTENSITY_ACTIONS]}

    return predict


__all__ = ["SCHEMA_VERSION", "MODEL_SCHEMA_VERSION", "CANDIDATES", "MAX_SUPPORT",
           "DistributionContractError", "validate_distribution", "distribution_mean",
           "quantile_sample", "distribution_scores", "fit_intensity_distributions", "make_distribution_predictor"]
