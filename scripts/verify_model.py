"""Re-verify the trained model against its own recorded metrics.

`metrics.json` is written by the trainer and read by the report. If `model.txt` were
ever stale, corrupted, or trained against a different feature list, the report would
keep quoting the old numbers and nothing would fail. This script re-derives them from
the model on disk and asserts they still match.

It also re-asserts the Stage 4 finding: `candidate_delay_hours` carries ~zero gain,
because the training data is on-policy and contains no variation in the timing
decision. That claim is the centrepiece of the write-up. If it silently changed, the
README and the report are both wrong.

    python -m scripts.verify_model     (or: uv run python scripts/verify_model.py)

Exits non-zero on the first failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.features.build import FEATURE_NAMES  # noqa: E402
from src.model.dataset import DATA_DIR  # noqa: E402
from src.model.score import (  # noqa: E402
    FeatureDrift,
    ModelNotAvailable,
    Scorer,
    reset_cache,
)
from src.model.train import calibration_table  # noqa: E402

TOLERANCE = 1e-3  # "match to 3dp"
PASS, FAIL = 0, 0
LINES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    LINES.append(f"{mark}|{name}|{detail}")
    print(f"  {mark:<4} {name:<56} {detail}")
    return ok


def main() -> int:
    print("TRIAGE model verification")
    print(f"  artefacts  {DATA_DIR}")

    for required in ("model.txt", "feature_names.json", "metrics.json", "importances.json"):
        if not (DATA_DIR / required).exists():
            check(f"{required} present", False, "missing")
            print("\nFATAL: run `python -m src.model.train` first.")
            return 1
    check("all four artefacts present", True)

    metrics = json.loads((DATA_DIR / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((DATA_DIR / "feature_names.json").read_text(encoding="utf-8"))
    importances = json.loads((DATA_DIR / "importances.json").read_text(encoding="utf-8"))

    # -- 1. the model matches the declared feature set ------------------------
    scorer = Scorer(DATA_DIR)
    check(
        "feature_names.json matches build.FEATURE_NAMES",
        manifest["features"] == list(FEATURE_NAMES),
        f"{len(manifest['features'])} features",
    )
    check(
        "the booster expects that many features",
        scorer._booster.num_feature() == len(FEATURE_NAMES),
        f"booster {scorer._booster.num_feature()} vs declared {len(FEATURE_NAMES)}",
    )
    check(
        "metrics.json agrees on the count",
        metrics.get("n_features") == len(FEATURE_NAMES),
        str(metrics.get("n_features")),
    )

    # -- 2. re-score the held-out test split ----------------------------------
    dataset_path = DATA_DIR / "dataset.csv"
    if not dataset_path.exists():
        check(
            "dataset.csv present for re-scoring",
            False,
            "regenerate with `python -m src.model.train`",
        )
        print("\nFATAL: cannot re-score without the dataset.")
        return 1

    import pandas as pd

    frame = pd.read_csv(dataset_path)
    test = frame[frame["split"] == "test"]
    check("test split is non-empty", len(test) > 0, f"{len(test)} rows")

    rows = test[list(FEATURE_NAMES)].to_dict("records")
    probabilities = scorer.score_batch(rows)
    labels = test["label"].tolist()

    from sklearn.metrics import average_precision_score, roc_auc_score

    pr_auc = float(average_precision_score(labels, probabilities))
    roc_auc = float(roc_auc_score(labels, probabilities))
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels)

    recorded = metrics["splits"]["test"]
    check(
        "PR-AUC matches metrics.json to 3dp",
        abs(pr_auc - recorded["pr_auc"]) < TOLERANCE,
        f"recomputed {pr_auc:.4f} vs recorded {recorded['pr_auc']:.4f}",
    )
    check(
        "ROC-AUC matches metrics.json to 3dp",
        abs(roc_auc - recorded["roc_auc"]) < TOLERANCE,
        f"recomputed {roc_auc:.4f} vs recorded {recorded['roc_auc']:.4f}",
    )
    check(
        "Brier matches metrics.json to 3dp",
        abs(brier - recorded["brier"]) < TOLERANCE,
        f"recomputed {brier:.4f} vs recorded {recorded['brier']:.4f}",
    )

    # -- 3. the calibration table still reproduces ----------------------------
    rebuilt = calibration_table(labels, probabilities)
    recorded_calibration = metrics.get("calibration_test", [])
    same = len(rebuilt) == len(recorded_calibration) and all(
        abs(a["predicted_mean"] - b["predicted_mean"]) < TOLERANCE
        and abs(a["observed_rate"] - b["observed_rate"]) < TOLERANCE
        for a, b in zip(rebuilt, recorded_calibration)
    )
    check(
        "calibration deciles reproduce",
        same,
        f"{len(rebuilt)} deciles",
    )
    if recorded_calibration:
        first, last = recorded_calibration[0], recorded_calibration[-1]
        check(
            "calibration is monotone end to end",
            last["observed_rate"] > first["observed_rate"],
            f"d1 {first['observed_rate']:.3f} -> d{last['decile']} "
            f"{last['observed_rate']:.3f}",
        )

    # -- 4. the refusals still refuse -----------------------------------------
    try:
        scorer.score({"a": 1.0})
        check("feature drift raises", False, "it did not")
    except FeatureDrift:
        check("feature drift raises", True)

    reset_cache()
    import tempfile

    try:
        Scorer(Path(tempfile.mkdtemp()))
        check("a missing model raises", False, "it did not")
    except ModelNotAvailable:
        check("a missing model raises", True)

    # -- 5. training provenance ------------------------------------------------
    provenance_path = DATA_DIR / "dataset_provenance.json"
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        check("trained on the baseline arm only", provenance["arm"] == "baseline",
              provenance["arm"])
        check("trained at seed 7", provenance["seed"] == 7, str(provenance["seed"]))
        check(
            "training seed differs from the evaluation seed (42)",
            provenance["seed"] != 42,
            "train 7 / eval 42 — no population overlap",
        )
        check("trained on the `normal` scenario", provenance["scenario"] == "normal",
              provenance["scenario"])
        check(
            "temporal split recorded",
            provenance["split_days"]["train"] == [1, 21],
            str(provenance["split_days"]),
        )
        check(
            "positive labels are a real minority-to-half share",
            0.05 < provenance["positive_rate"] < 0.95,
            f"{provenance['n_positive']}/{provenance['n_rows']} "
            f"= {provenance['positive_rate']:.1%}",
        )
    else:
        check("dataset_provenance.json present", False, "missing")

    # -- 6. the Stage 4 finding, re-asserted ----------------------------------
    print("\n  top features by gain")
    total = sum(row["gain"] for row in importances) or 1.0
    for row in importances[:15]:
        print(f"    {row['feature']:<32} {100 * row['gain'] / total:>6.2f}%  "
              f"{row['split']:>4} splits")

    delay = next(
        (r for r in importances if r["feature"] == "candidate_delay_hours"), None
    )
    check(
        "candidate_delay_hours is present in the importances",
        delay is not None,
    )
    if delay is not None:
        share = 100 * delay["gain"] / total
        check(
            "candidate_delay_hours still carries ~zero gain",
            share < 0.5,
            f"{share:.3f}% of gain — the Stage 4 finding: on-policy training data "
            f"contains no variation in the timing decision",
        )

    zero_gain = set(metrics.get("zero_gain_features", []))
    check(
        "metrics.json records candidate_delay_hours as unused",
        "candidate_delay_hours" in zero_gain,
        f"{len(zero_gain)} zero-gain features",
    )

    salary = next(
        (r for r in importances if r["feature"] == "days_to_salary_date"), None
    )
    if salary is not None:
        check(
            "days_to_salary_date is a top-5 feature",
            importances.index(salary) < 5,
            f"rank {importances.index(salary) + 1}, "
            f"{100 * salary['gain'] / total:.1f}% of gain — research/03 §3.3 predicted "
            f"this",
        )

    print("\n" + "-" * 64)
    print(f"PASS {PASS}   FAIL {FAIL}")
    out = ROOT / "eval" / "model" / "verification.txt"
    out.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    if FAIL:
        return 1
    print("Model artefacts verified against their own recorded metrics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
