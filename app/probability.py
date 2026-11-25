"""Deterministic, stdlib-only probability model for PAPER entry candidates.

The binary outcome is 1 only when a hypothetical buy at the candidate close and
sell at the close of the fifth subsequent daily candle has positive net return
after configured entry/exit slippage and fees.  Features are captured at the
candidate candle; future bars are used only to mature labels.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, settings_fingerprint

HORIZON_CANDLES = 5
FEATURE_NAMES = (
    "close_over_sma30",
    "close_over_sma60",
    "sma30_over_sma60",
    "sma60_momentum_5",
    "ema_spread_over_close",
    "rsi14_scaled",
    "atr14_over_close",
    "volume_ratio20",
)
MIN_TOTAL = 300
MIN_TRAIN = 180
MIN_CALIBRATION = 60
MIN_TEST = 60
MIN_CLASS = 20


class ProbabilityModelError(ValueError):
    """A model or feature failed strict validation."""


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def artifact_hash(payload: dict[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("artifact_hash", None)
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ProbabilityModelError("probability data must be finite")
    return value


def extract_features(metrics: dict[str, Decimal]) -> dict[str, float]:
    required = ("close", "sma30", "sma60", "sma60_five_ago", "ema7", "ema15", "rsi14", "atr14", "volume_ratio20")
    if any(name not in metrics for name in required):
        raise ProbabilityModelError("candidate is missing probability features")
    values = {name: _finite(float(metrics[name])) for name in required}
    if values["close"] <= 0 or values["sma30"] <= 0 or values["sma60"] <= 0 or values["sma60_five_ago"] <= 0:
        raise ProbabilityModelError("probability feature denominator must be positive")
    result = {
        "close_over_sma30": values["close"] / values["sma30"] - 1.0,
        "close_over_sma60": values["close"] / values["sma60"] - 1.0,
        "sma30_over_sma60": values["sma30"] / values["sma60"] - 1.0,
        "sma60_momentum_5": values["sma60"] / values["sma60_five_ago"] - 1.0,
        "ema_spread_over_close": (values["ema7"] - values["ema15"]) / values["close"],
        "rsi14_scaled": values["rsi14"] / 100.0,
        "atr14_over_close": values["atr14"] / values["close"],
        "volume_ratio20": values["volume_ratio20"],
    }
    return {name: _finite(result[name]) for name in FEATURE_NAMES}


def net_outcome(entry_close: Decimal, exit_close: Decimal, settings: Settings) -> tuple[int, str]:
    hundred = Decimal("100")
    buy = entry_close * (Decimal("1") + settings.slippage_per_side_pct / hundred)
    sell = exit_close * (Decimal("1") - settings.slippage_per_side_pct / hundred)
    entry_cost = buy * (Decimal("1") + settings.fee_per_side_pct / hundred)
    exit_proceeds = sell * (Decimal("1") - settings.fee_per_side_pct / hundred)
    net_return = exit_proceeds / entry_cost - Decimal("1")
    return (1 if net_return > 0 else 0), format(net_return, "f")


def pava(probabilities: Iterable[float], labels: Iterable[int]) -> list[dict[str, Any]]:
    """Fit non-decreasing isotonic blocks after coalescing equal scores."""
    grouped: dict[float, list[int]] = {}
    for raw_score, label in zip(probabilities, labels, strict=True):
        score = _finite(float(raw_score))
        if label not in (0, 1):
            raise ProbabilityModelError("labels must be binary")
        bucket = grouped.setdefault(score, [0, 0])
        bucket[0] += int(label)
        bucket[1] += 1
    blocks: list[dict[str, Any]] = [
        {"min_score": score, "max_score": score, "positive": values[0], "count": values[1]}
        for score, values in sorted(grouped.items())
    ]
    index = 1
    while index < len(blocks):
        left, right = blocks[index - 1], blocks[index]
        if left["positive"] / left["count"] <= right["positive"] / right["count"]:
            index += 1
            continue
        blocks[index - 1:index + 1] = [{
            "min_score": left["min_score"],
            "max_score": right["max_score"],
            "positive": left["positive"] + right["positive"],
            "count": left["count"] + right["count"],
        }]
        index = max(1, index - 1)
    for block in blocks:
        block["probability"] = block["positive"] / block["count"]
    return blocks


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 700.0)))
    exp_value = math.exp(max(value, -700.0))
    return exp_value / (1.0 + exp_value)


def _raw_probability(features: list[float], model: dict[str, Any]) -> float:
    scaled = [(value - mean) / scale for value, mean, scale in zip(features, model["means"], model["scales"], strict=True)]
    return _finite(_sigmoid(model["intercept"] + sum(weight * value for weight, value in zip(model["weights"], scaled, strict=True))))


def _calibrated(raw: float, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the last observed block at or below a score.

    PAVA blocks can have gaps between observed raw scores. Selecting the next
    block inside such a gap would grant its higher support/probability before
    that score was observed, so runtime scoring stays conservatively on the
    previous block until the next block's minimum is reached.
    """
    selected = blocks[0]
    for block in blocks:
        if raw < block["min_score"]:
            break
        selected = block
        if raw <= block["max_score"]:
            break
    return selected


def wilson_lower(positive: int, count: int, z: float = 1.959963984540054) -> float:
    if count <= 0 or not 0 <= positive <= count:
        raise ProbabilityModelError("invalid Wilson support")
    proportion = positive / count
    denominator = 1.0 + z * z / count
    centre = proportion + z * z / (2.0 * count)
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z * z / (4.0 * count)) / count)
    return max(0.0, (centre - margin) / denominator)


def _fit_logistic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = [[float(row["features"][name]) for name in FEATURE_NAMES] for row in rows]
    labels = [int(row["label"]) for row in rows]
    means = [sum(column) / len(rows) for column in zip(*matrix, strict=True)]
    scales = []
    for index, mean in enumerate(means):
        variance = sum((row[index] - mean) ** 2 for row in matrix) / len(rows)
        scales.append(math.sqrt(variance) if variance > 1e-24 else 1.0)
    scaled = [[(value - means[index]) / scales[index] for index, value in enumerate(row)] for row in matrix]
    weights = [0.0] * len(FEATURE_NAMES)
    prevalence = min(max(sum(labels) / len(labels), 1e-6), 1.0 - 1e-6)
    intercept = math.log(prevalence / (1.0 - prevalence))
    learning_rate = 0.1
    regularization = 1e-3
    for _ in range(2000):
        errors = [_sigmoid(intercept + sum(w * x for w, x in zip(weights, row, strict=True))) - label for row, label in zip(scaled, labels, strict=True)]
        intercept -= learning_rate * sum(errors) / len(rows)
        for index in range(len(weights)):
            gradient = sum(error * row[index] for error, row in zip(errors, scaled, strict=True)) / len(rows)
            weights[index] -= learning_rate * (gradient + regularization * weights[index])
    numbers = means + scales + weights + [intercept]
    if not all(math.isfinite(value) for value in numbers):
        raise ProbabilityModelError("non-finite logistic fit")
    return {"means": means, "scales": scales, "weights": weights, "intercept": intercept}


def temporal_split(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Global chronological split; timestamps, never symbols/rows, define boundaries.

    Five complete daily timestamps are embargoed before calibration and test.
    """
    ordered = sorted(rows, key=lambda row: (int(row["candle_close_time"]), row["symbol"]))
    timestamps = sorted({int(row["candle_close_time"]) for row in ordered})
    if len(timestamps) < 3:
        return {"train": [], "calibration": [], "test": []}
    calibration_index = int(len(timestamps) * 0.60)
    test_index = int(len(timestamps) * 0.80)
    calibration_start = timestamps[min(calibration_index, len(timestamps) - 1)]
    test_start = timestamps[min(test_index, len(timestamps) - 1)]
    day_ms = 86_400_000
    return {
        "train": [row for row in ordered if int(row["candle_close_time"]) < calibration_start - HORIZON_CANDLES * day_ms],
        "calibration": [row for row in ordered if calibration_start <= int(row["candle_close_time"]) < test_start - HORIZON_CANDLES * day_ms],
        "test": [row for row in ordered if int(row["candle_close_time"]) >= test_start],
    }


def _sample_status(rows: list[dict[str, Any]], split: dict[str, list[dict[str, Any]]]) -> tuple[bool, str]:
    if len(rows) < MIN_TOTAL:
        return False, f"need at least {MIN_TOTAL} matured candidates"
    requirements = {"train": MIN_TRAIN, "calibration": MIN_CALIBRATION, "test": MIN_TEST}
    for name, minimum in requirements.items():
        partition = split[name]
        if len(partition) < minimum:
            return False, f"{name} needs at least {minimum} samples after embargo"
        counts = {label: sum(int(row["label"]) == label for row in partition) for label in (0, 1)}
        if min(counts.values()) < MIN_CLASS:
            return False, f"{name} needs at least {MIN_CLASS} samples of each class"
    return True, "sample requirements met"


def fit_artifact(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    split = temporal_split(rows)
    enough, reason = _sample_status(rows, split)
    base: dict[str, Any] = {
        "schema_version": 1,
        "status": "INSUFFICIENT_SAMPLE",
        "reason": reason,
        "strategy_version": settings.strategy_version,
        "config_hash": settings_fingerprint(settings),
        "outcome": {
            "definition": "net_return_after_entry_and_exit_fee_and_slippage_gt_zero",
            "horizon_daily_candles": HORIZON_CANDLES,
            "fee_per_side_pct": format(settings.fee_per_side_pct, "f"),
            "slippage_per_side_pct": format(settings.slippage_per_side_pct, "f"),
        },
        "feature_names": list(FEATURE_NAMES),
        "sample_counts": {"total_matured": len(rows), **{name: len(items) for name, items in split.items()}},
    }
    if not enough:
        base["artifact_hash"] = artifact_hash(base)
        return base
    model = _fit_logistic(split["train"])
    calibration_raw = [_raw_probability([float(row["features"][name]) for name in FEATURE_NAMES], model) for row in split["calibration"]]
    blocks = pava(calibration_raw, [int(row["label"]) for row in split["calibration"]])
    test_predictions = []
    for row in split["test"]:
        raw = _raw_probability([float(row["features"][name]) for name in FEATURE_NAMES], model)
        test_predictions.append(float(_calibrated(raw, blocks)["probability"]))
    labels = [int(row["label"]) for row in split["test"]]
    brier = sum((prediction - label) ** 2 for prediction, label in zip(test_predictions, labels, strict=True)) / len(labels)
    train_prevalence = sum(int(row["label"]) for row in split["train"]) / len(split["train"])
    baseline = sum((train_prevalence - label) ** 2 for label in labels) / len(labels)
    bins: dict[int, list[tuple[float, int]]] = {}
    for prediction, label in zip(test_predictions, labels, strict=True):
        bins.setdefault(min(9, int(prediction * 10)), []).append((prediction, label))
    ece = sum(len(items) / len(labels) * abs(sum(p for p, _ in items) / len(items) - sum(y for _, y in items) / len(items)) for items in bins.values())
    base.update({
        "status": "VALID" if brier < baseline and ece <= 0.10 else "INVALID_METRICS",
        "reason": "validation criteria met" if brier < baseline and ece <= 0.10 else "Brier must beat baseline and ECE must be <= 0.10",
        "model": model,
        "isotonic_blocks": blocks,
        "metrics": {"brier": brier, "baseline_brier": baseline, "ece": ece},
    })
    base["artifact_hash"] = artifact_hash(base)
    return base


def write_artifact(path: str | Path, artifact: dict[str, Any]) -> None:
    """Atomically replace a local canonical artifact in its destination directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (canonical_json(artifact) + "\n").encode("utf-8")
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
            temporary_path = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def read_artifact(path: str | Path, settings: Settings) -> dict[str, Any]:
    """Read and strictly validate an artifact, including non-runnable statuses."""
    artifact_path = Path(path)
    try:
        metadata = artifact_path.lstat()
        if artifact_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ProbabilityModelError("probability artifact must be a regular file")
        if metadata.st_size > 1_000_000:
            raise ProbabilityModelError("probability artifact exceeds size limit")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ProbabilityModelError("probability artifact ownership or permissions are unsafe")
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except ProbabilityModelError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbabilityModelError("probability artifact unavailable or invalid") from exc
    if not isinstance(payload, dict) or payload.get("artifact_hash") != artifact_hash(payload):
        raise ProbabilityModelError("probability artifact hash mismatch")
    status = payload.get("status")
    base_keys = {
        "schema_version", "status", "reason", "strategy_version", "config_hash",
        "outcome", "feature_names", "sample_counts", "artifact_hash",
    }
    expected_keys = base_keys if status == "INSUFFICIENT_SAMPLE" else base_keys | {"model", "isotonic_blocks", "metrics"}
    if status not in {"VALID", "INVALID_METRICS", "INSUFFICIENT_SAMPLE"} or set(payload) != expected_keys:
        raise ProbabilityModelError("probability artifact schema mismatch")
    if payload.get("schema_version") != 1:
        raise ProbabilityModelError("probability artifact schema mismatch")
    if payload.get("strategy_version") != settings.strategy_version or payload.get("config_hash") != settings_fingerprint(settings):
        raise ProbabilityModelError("probability artifact binding mismatch")
    expected_outcome = {
        "definition": "net_return_after_entry_and_exit_fee_and_slippage_gt_zero",
        "horizon_daily_candles": HORIZON_CANDLES,
        "fee_per_side_pct": format(settings.fee_per_side_pct, "f"),
        "slippage_per_side_pct": format(settings.slippage_per_side_pct, "f"),
    }
    if payload.get("feature_names") != list(FEATURE_NAMES) or payload.get("outcome") != expected_outcome:
        raise ProbabilityModelError("probability artifact schema mismatch")
    sample_counts = payload.get("sample_counts")
    count_keys = {"total_matured", "train", "calibration", "test"}
    if (
        not isinstance(sample_counts, dict)
        or set(sample_counts) != count_keys
        or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in sample_counts.values())
        or sample_counts["total_matured"] < sum(sample_counts[name] for name in ("train", "calibration", "test"))
    ):
        raise ProbabilityModelError("probability artifact sample counts are invalid")
    if status != "INSUFFICIENT_SAMPLE":
        model = payload.get("model")
        blocks = payload.get("isotonic_blocks")
        metrics = payload.get("metrics")
        if not isinstance(model, dict) or set(model) != {"means", "scales", "weights", "intercept"}:
            raise ProbabilityModelError("probability artifact model is invalid")
        if any(not isinstance(model[name], list) or len(model[name]) != len(FEATURE_NAMES) for name in ("means", "scales", "weights")):
            raise ProbabilityModelError("probability artifact model is invalid")
        numbers = model["means"] + model["scales"] + model["weights"] + [model["intercept"]]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in numbers):
            raise ProbabilityModelError("probability artifact model is invalid")
        if any(value <= 0 for value in model["scales"]):
            raise ProbabilityModelError("probability artifact model scales are invalid")
        if not isinstance(blocks, list) or not blocks:
            raise ProbabilityModelError("probability artifact calibration is invalid")
        previous_max: float | None = None
        previous_probability = -1.0
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {"min_score", "max_score", "positive", "count", "probability"}:
                raise ProbabilityModelError("probability artifact calibration is invalid")
            if (
                not isinstance(block["count"], int) or isinstance(block["count"], bool)
                or not isinstance(block["positive"], int) or isinstance(block["positive"], bool)
                or block["count"] <= 0 or not 0 <= block["positive"] <= block["count"]
            ):
                raise ProbabilityModelError("probability artifact calibration is invalid")
            if not all(
                isinstance(block[name], (int, float)) and not isinstance(block[name], bool) and math.isfinite(block[name])
                for name in ("min_score", "max_score", "probability")
            ):
                raise ProbabilityModelError("probability artifact calibration is invalid")
            minimum = float(block["min_score"])
            maximum = float(block["max_score"])
            probability = float(block["probability"])
            expected_probability = block["positive"] / block["count"]
            if (
                minimum > maximum
                or previous_max is not None and minimum <= previous_max
                or not 0.0 <= probability <= 1.0
                or probability < previous_probability
                or not math.isclose(probability, expected_probability, rel_tol=0.0, abs_tol=1e-15)
            ):
                raise ProbabilityModelError("probability artifact calibration is invalid")
            previous_max = maximum
            previous_probability = probability
        if (
            not isinstance(metrics, dict)
            or set(metrics) != {"brier", "baseline_brier", "ece"}
            or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or not 0.0 <= float(value) <= 1.0
                for value in metrics.values()
            )
        ):
            raise ProbabilityModelError("probability artifact metrics are invalid")
        metrics_valid = metrics["brier"] < metrics["baseline_brier"] and metrics["ece"] <= 0.10
        if (status == "VALID") != metrics_valid:
            raise ProbabilityModelError("probability artifact status and metrics disagree")
    return payload


def load_artifact(path: str | Path, settings: Settings) -> dict[str, Any]:
    payload = read_artifact(path, settings)
    if payload["status"] != "VALID":
        raise ProbabilityModelError(f"probability artifact status is {payload['status']}")
    return payload


@dataclass(frozen=True)
class GateEvidence:
    status: str
    probability: float | None
    support: int
    positive: int
    wilson_lower_bound: float | None
    probability_threshold: float
    wilson_threshold: float
    minimum_support: int
    allowed: bool
    reason: str
    artifact_hash: str | None

    def as_json_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "probability": self.probability,
            "support": self.support,
            "positive": self.positive,
            "wilson_lower_bound": self.wilson_lower_bound,
            "probability_threshold": self.probability_threshold,
            "wilson_threshold": self.wilson_threshold,
            "minimum_support": self.minimum_support,
            "allowed": self.allowed,
            "reason": self.reason,
            "artifact_hash": self.artifact_hash,
        }


def evaluate_gate(features: dict[str, float], settings: Settings) -> GateEvidence:
    probability_threshold = float(settings.probability_threshold)
    wilson_threshold = float(settings.probability_wilson_threshold)
    minimum_support = settings.probability_min_support
    try:
        if not settings.probability_model_path:
            raise ProbabilityModelError("probability artifact path is not configured")
        artifact = load_artifact(settings.probability_model_path, settings)
        vector = [_finite(float(features[name])) for name in FEATURE_NAMES]
        raw = _raw_probability(vector, artifact["model"])
        block = _calibrated(raw, artifact["isotonic_blocks"])
        probability = _finite(float(block["probability"]))
        support = int(block["count"])
        positive = int(block["positive"])
        lower = wilson_lower(positive, support)
        failures = []
        if probability < probability_threshold:
            failures.append("calibrated probability below threshold")
        if lower < wilson_threshold:
            failures.append("Wilson lower bound below threshold")
        if support < minimum_support:
            failures.append("runtime support below minimum")
        allowed = not failures
        reason = "gate passed" if allowed else "; ".join(failures)
        return GateEvidence(
            "VALID", probability, support, positive, lower,
            probability_threshold, wilson_threshold, minimum_support,
            allowed, reason, artifact["artifact_hash"],
        )
    except (KeyError, TypeError, ValueError, ProbabilityModelError) as exc:
        return GateEvidence(
            "UNAVAILABLE", None, 0, 0, None,
            probability_threshold, wilson_threshold, minimum_support,
            False, str(exc), None,
        )
