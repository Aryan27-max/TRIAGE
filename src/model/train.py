"""LightGBM training.

Params are research/06 §6.5 verbatim. No hyperparameter search beyond early stopping:
at this data volume a search would fit the validation split, and the honest result is
worth more than a tuned one.

Categoricals are declared as LightGBM categoricals rather than one-hot encoded — with
110 error-code levels one-hot would produce a mostly-empty matrix and lose the ability
to report gain per code.

    python -m src.model.train

Writes to eval/model/: model.txt, feature_names.json, metrics.json, importances.json.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from src.model.dataset import (
    DATA_DIR,
    SPLIT_DAYS,
    TRAINING_ARM,
    build_rows,
    provenance,
    to_frame,
    write_dataset,
)
from src.policy.engine import PolicyEngine
from src.store import db

# research/06 §6.5, verbatim.
PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": ["auc", "average_precision"],
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "is_unbalance": True,
    "verbosity": -1,
    "seed": 42,
    "deterministic": True,
}

NUM_BOOST_ROUND = 800
EARLY_STOPPING_ROUNDS = 50

# Below this many positive labels the model is fitting noise. Not a hard stop — the
# result is still reported — but it is stated in metrics.json and in the report rather
# than being quietly trained through.
MIN_POSITIVE_LABELS = 500

CALIBRATION_DECILES = 10


def _brier(labels, probabilities) -> float:
    return sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels)


def _pr_auc(labels, probabilities) -> float:
    from sklearn.metrics import average_precision_score

    return float(average_precision_score(labels, probabilities))


def _roc_auc(labels, probabilities) -> float:
    from sklearn.metrics import roc_auc_score

    if len(set(labels)) < 2:
        return float("nan")
    return float(roc_auc_score(labels, probabilities))


def calibration_table(labels, probabilities, deciles: int = CALIBRATION_DECILES):
    """Predicted vs observed, by predicted-probability decile.

    This is what separates "the model learned nothing" from "the model learned
    something but the decision layer is misusing it". A flat observed column against a
    varying predicted column means no signal; a well-ordered one means the ranking is
    real and any lost uplift is the argmax's fault, not the model's.
    """
    paired = sorted(zip(probabilities, labels))
    if not paired:
        return []
    size = max(1, len(paired) // deciles)
    table = []
    for index in range(0, len(paired), size):
        chunk = paired[index : index + size]
        if not chunk:
            continue
        table.append(
            {
                "decile": len(table) + 1,
                "n": len(chunk),
                "predicted_mean": round(sum(p for p, _ in chunk) / len(chunk), 4),
                "observed_rate": round(sum(y for _, y in chunk) / len(chunk), 4),
            }
        )
    return table[:deciles]


def train(
    db_path: Path | str,
    run_id: str,
    *,
    out_dir: Path | str = DATA_DIR,
    arm: str = TRAINING_ARM,
) -> dict[str, Any]:
    """Build the dataset, split it temporally, train, and write every artefact."""
    import lightgbm as lgb
    import pandas as pd

    from src.features.build import CATEGORICAL_FEATURES, FEATURE_NAMES

    engine = PolicyEngine().load()
    conn = db.connect(db_path)
    try:
        run = db.get_run(conn, run_id)
        if run is None:
            raise ValueError(f"no run {run_id!r} in {db_path}")
        rows = build_rows(conn, run, arm=arm, engine=engine)
    finally:
        conn.close()

    if not rows:
        raise ValueError(f"run {run_id} produced no {arm} attempts to train on")

    prov = provenance(rows, run, arm=arm)
    directory = Path(out_dir)
    write_dataset(rows, prov, directory)

    frame = to_frame(rows)
    # The level list is part of the model, not an artefact of the batch. LightGBM
    # matches categoricals by integer code, and pandas derives those codes from the
    # values present — so a five-row candidate batch at inference time would assign
    # `error_code` a different code than training did, silently and plausibly.
    categories: dict[str, list[str]] = {}
    for column in CATEGORICAL_FEATURES:
        levels = sorted(frame[column].astype(str).unique().tolist())
        categories[column] = levels
        frame[column] = pd.Categorical(frame[column].astype(str), categories=levels)

    train_df = frame[frame["split"] == "train"]
    valid_df = frame[frame["split"] == "valid"]
    test_df = frame[frame["split"] == "test"]

    columns = list(FEATURE_NAMES)
    categorical = list(CATEGORICAL_FEATURES)

    warnings: list[str] = []
    positives = int(train_df["label"].sum())
    if positives < MIN_POSITIVE_LABELS:
        warnings.append(
            f"only {positives} positive labels in the training split "
            f"(< {MIN_POSITIVE_LABELS}); metrics below are fragile and the model may "
            f"be fitting noise"
        )
    for name, part in (("valid", valid_df), ("test", test_df)):
        if len(part) == 0:
            warnings.append(f"the {name} split is empty")
        elif part["label"].nunique() < 2:
            warnings.append(f"the {name} split has only one label class")

    train_set = lgb.Dataset(
        train_df[columns], label=train_df["label"], categorical_feature=categorical
    )
    valid_set = lgb.Dataset(
        valid_df[columns],
        label=valid_df["label"],
        categorical_feature=categorical,
        reference=train_set,
    )

    callbacks = [lgb.log_evaluation(100)]
    if len(valid_df) and valid_df["label"].nunique() > 1:
        callbacks.insert(0, lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False))

    model = lgb.train(
        PARAMS,
        train_set,
        valid_sets=[valid_set] if len(valid_df) else None,
        num_boost_round=NUM_BOOST_ROUND,
        callbacks=callbacks,
    )

    metrics: dict[str, Any] = {
        "run_id": run.run_id,
        "scenario": run.scenario,
        "seed": run.seed,
        "trained_on_arm": arm,
        "best_iteration": model.best_iteration or model.num_trees(),
        "split_days": {k: list(v) for k, v in SPLIT_DAYS.items()},
        "n_features": len(columns),
        "warnings": warnings,
        "min_positive_labels_threshold": MIN_POSITIVE_LABELS,
        "splits": {},
    }
    for name, part in (("train", train_df), ("valid", valid_df), ("test", test_df)):
        if not len(part):
            metrics["splits"][name] = {"n": 0}
            continue
        labels = part["label"].tolist()
        predicted = model.predict(part[columns]).tolist()
        metrics["splits"][name] = {
            "n": len(part),
            "positives": int(sum(labels)),
            "base_rate": round(sum(labels) / len(labels), 4),
            "pr_auc": round(_pr_auc(labels, predicted), 4),
            "roc_auc": round(_roc_auc(labels, predicted), 4),
            "brier": round(_brier(labels, predicted), 4),
            "lift_over_base_rate": (
                round(_pr_auc(labels, predicted) / (sum(labels) / len(labels)), 3)
                if sum(labels)
                else None
            ),
        }
    if len(test_df):
        metrics["calibration_test"] = calibration_table(
            test_df["label"].tolist(), model.predict(test_df[columns]).tolist()
        )

    gain = model.feature_importance("gain").tolist()
    split = model.feature_importance("split").tolist()
    importances = sorted(
        (
            {"feature": f, "gain": round(g, 2), "split": int(s)}
            for f, g, s in zip(columns, gain, split)
        ),
        key=lambda row: -row["gain"],
    )
    metrics["zero_gain_features"] = [r["feature"] for r in importances if r["gain"] == 0]

    directory.mkdir(parents=True, exist_ok=True)
    model.save_model(str(directory / "model.txt"))
    (directory / "feature_names.json").write_text(
        json.dumps(
            {
                "features": columns,
                "categorical": categorical,
                # Pinned so inference reproduces training's codes exactly.
                "categories": categories,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (directory / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "importances.json").write_text(
        json.dumps(importances, indent=2), encoding="utf-8"
    )
    return metrics


def _default_run() -> tuple[Path, str]:
    """The most recent `normal` run on disk, which is what training should use."""
    from eval.run_arms import RUNS_DIR

    candidates: list[tuple[Path, db.Run]] = []
    for path in sorted(RUNS_DIR.glob("*.db")):
        conn = db.connect(path)
        try:
            for run in db.list_runs(conn):
                if run.scenario == "normal" and TRAINING_ARM in run.arm_names:
                    candidates.append((path, run))
        finally:
            conn.close()
    if not candidates:
        raise SystemExit(
            "no `normal` run with a baseline arm in eval/runs/. "
            "Run: python -m eval.run_arms --seed 42 --scenario normal"
        )
    path, run = max(candidates, key=lambda pair: pair[1].n_payments)
    return path, run.run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.model.train",
        description="Train the LightGBM timing/ranking model on a Stage 3 run.",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--arm", default=TRAINING_ARM)
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    if args.db is None or args.run_id is None:
        path, run_id = _default_run()
    else:
        path, run_id = args.db, args.run_id

    metrics = train(path, run_id, out_dir=args.out, arm=args.arm)

    print(f"run           {metrics['run_id']} ({metrics['scenario']})")
    print(f"trained on    {metrics['trained_on_arm']} attempts")
    print(f"best iter     {metrics['best_iteration']}")
    for name, block in metrics["splits"].items():
        if not block.get("n"):
            print(f"  {name:<6} empty")
            continue
        print(
            f"  {name:<6} n={block['n']:<5} pos={block['positives']:<4} "
            f"base={block['base_rate']:.3f}  PR-AUC={block['pr_auc']:.3f}  "
            f"ROC-AUC={block['roc_auc']:.3f}  Brier={block['brier']:.3f}"
        )
    for warning in metrics["warnings"]:
        print(f"  WARNING: {warning}")
    print(f"artefacts     {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
