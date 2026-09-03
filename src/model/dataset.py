"""Training rows, replayed from a Stage 3 run.

**One row per attempt, not per payment.** (I-11) A payment attempted four times made
four decisions with four different contexts; collapsing them into one row conflates
them and throws away the thing the model is supposed to learn.

Each row is rebuilt by replaying its attempt through ``build_features`` with
``as_of = attempt.scheduled_at``, so the row contains exactly what would have been
known when the decision was made — not what the store holds now.

**Only the baseline arm's attempts are used.** Control's action choice is uncorrelated
with the error code: it retries everything on a fixed schedule, so its rows carry no
information about which action suits which failure. Training on them would teach the
model the unconditional base rate and nothing else.

The split is temporal, never random. (I-10) A case that straddles a boundary goes
entirely to the earlier split, so no case_id appears twice and no future attempt of a
training case leaks into validation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.features.build import FEATURE_NAMES, Candidate, build_features
from src.policy.engine import PolicyEngine
from src.store import db

DAY = 86400

TRAINING_ARM = "baseline"

# research/06 §6.5. Days are 1-indexed from the run's start.
SPLIT_DAYS: dict[str, tuple[int, int]] = {
    "train": (1, 21),
    "valid": (22, 26),
    "test": (27, 30),
}

DATA_DIR = Path(__file__).resolve().parents[2] / "eval" / "model"


@dataclass(frozen=True, slots=True)
class Row:
    case_id: str
    attempt_id: str
    attempt_number: int
    as_of: int
    day: int
    split: str
    label: int
    features: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Provenance:
    run_id: str
    seed: int
    scenario: str
    arm: str
    n_rows: int
    n_positive: int
    positive_rate: float
    first_ts: int
    last_ts: int
    split_days: dict[str, list[int]]
    split_rows: dict[str, int]
    split_positives: dict[str, int]
    dropped_straddling_attempts: int = 0
    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def day_of_run(ts: int, start_ts: int) -> int:
    """1-indexed day of the run window."""
    return (ts - start_ts) // DAY + 1


def split_for_day(day: int) -> str | None:
    for name, (low, high) in SPLIT_DAYS.items():
        if low <= day <= high:
            return name
    return None  # the trailing window: executed, but never trained or scored on


def build_rows(
    conn: sqlite3.Connection,
    run: db.Run,
    *,
    arm: str = TRAINING_ARM,
    engine: PolicyEngine | None = None,
) -> list[Row]:
    """Replay every attempt this arm made into a point-in-time training row."""
    engine = engine or PolicyEngine().load()
    attempts = db.fetch_all(
        conn,
        "SELECT a.*, c.arm AS arm FROM attempts a JOIN cases c ON c.id = a.case_id "
        "WHERE c.arm = ? ORDER BY a.executed_at, a.id",
        (arm,),
    )

    # I-10. Two properties have to hold together, and they pull against each other:
    #
    #   * no case_id appears in more than one split — otherwise the model trains on
    #     attempt 1 of a case and is scored on attempt 3 of the same case, same
    #     customer, same rail, same failure;
    #   * every training timestamp precedes every validation timestamp, which
    #     precedes every test timestamp.
    #
    # A case is assigned to the split of its *first* attempt, so it can never appear
    # twice. A later attempt of that case which falls outside that split's own day
    # range is then dropped rather than dragged backwards: keeping it would put a
    # day-25 row in the training set and break the ordering property, which is the
    # stronger of the two guarantees. The count of drops is recorded in provenance so
    # the cost is visible rather than silent.
    case_split: dict[str, str] = {}
    rows: list[Row] = []
    dropped = 0
    for record in attempts:
        as_of = record["scheduled_at"] or record["executed_at"]
        day = day_of_run(as_of, run.start_ts)
        case_id = record["case_id"]

        split = case_split.get(case_id)
        if split is None:
            split = split_for_day(day)
            if split is None:
                continue  # trailing-window attempt: executed, not trained on
            case_split[case_id] = split

        # The case is pinned to `split`; this attempt has drifted out of its window.
        if split_for_day(day) != split:
            dropped += 1
            continue

        features = build_features(
            conn,
            case_id,
            Candidate(
                action=record["action"],
                target_rail=record["target_rail"],
                scheduled_at=as_of,
            ),
            as_of,
            engine=engine,
        )
        rows.append(
            Row(
                case_id=case_id,
                attempt_id=record["id"],
                attempt_number=record["attempt_number"],
                as_of=as_of,
                day=day,
                split=split,
                label=1 if record["outcome"] == "success" else 0,
                features=features,
            )
        )
    rows.sort(key=lambda r: (r.as_of, r.attempt_id))
    build_rows.dropped_straddling = dropped  # type: ignore[attr-defined]
    return rows


def provenance(rows: Sequence[Row], run: db.Run, arm: str = TRAINING_ARM) -> Provenance:
    positives = sum(r.label for r in rows)
    return Provenance(
        run_id=run.run_id,
        seed=run.seed,
        scenario=run.scenario,
        arm=arm,
        n_rows=len(rows),
        n_positive=positives,
        positive_rate=round(positives / len(rows), 4) if rows else 0.0,
        first_ts=min((r.as_of for r in rows), default=0),
        last_ts=max((r.as_of for r in rows), default=0),
        split_days={k: list(v) for k, v in SPLIT_DAYS.items()},
        split_rows={
            name: sum(1 for r in rows if r.split == name) for name in SPLIT_DAYS
        },
        split_positives={
            name: sum(r.label for r in rows if r.split == name) for name in SPLIT_DAYS
        },
        dropped_straddling_attempts=getattr(build_rows, "dropped_straddling", 0),
    )


def write_dataset(
    rows: Sequence[Row], prov: Provenance, out_dir: Path | str = DATA_DIR
) -> tuple[Path, Path]:
    """CSV plus a provenance sidecar. CSV because it is diffable and needs no engine."""
    import csv

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / "dataset.csv"
    prov_path = directory / "dataset_provenance.json"

    header = ["case_id", "attempt_id", "attempt_number", "as_of", "day", "split", "label"]
    header += list(FEATURE_NAMES)
    with data_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(
                [
                    row.case_id,
                    row.attempt_id,
                    row.attempt_number,
                    row.as_of,
                    row.day,
                    row.split,
                    row.label,
                ]
                + [row.features[name] for name in FEATURE_NAMES]
            )
    prov_path.write_text(prov.to_json(), encoding="utf-8")
    return data_path, prov_path


def to_frame(rows: Sequence[Row]):
    """pandas DataFrame of the rows. Imported lazily so the ml extra stays optional."""
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "case_id": r.case_id,
                "as_of": r.as_of,
                "day": r.day,
                "split": r.split,
                "label": r.label,
                **r.features,
            }
            for r in rows
        ]
    )
