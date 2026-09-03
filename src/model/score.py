"""The scoring interface.

    score(features: dict) -> float

Two refusals, both deliberate:

* **A missing model raises.** No heuristic fallback, no default probability. If the
  model is not on disk the treatment arm must fail loudly rather than quietly degrade
  into a slightly different baseline while still calling itself the treatment.
* **Feature drift raises.** The incoming dict must match `feature_names.json` exactly.
  Filling absent features with zeros is how a model silently starts scoring something
  other than what it was trained on.

The model is loaded once per Scorer and never reloaded, so a run cannot straddle two
versions of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from src.model.dataset import DATA_DIR

MODEL_FILE = "model.txt"
FEATURES_FILE = "feature_names.json"


class ModelNotAvailable(Exception):
    """No trained model on disk. Not recoverable by falling back to a guess."""


class FeatureDrift(Exception):
    """The feature dict does not match what the model was trained on."""

    def __init__(self, missing: Sequence[str], unexpected: Sequence[str]) -> None:
        self.missing = list(missing)
        self.unexpected = list(unexpected)
        super().__init__(
            f"feature drift — missing {self.missing}, unexpected {self.unexpected}"
        )


class Scorer:
    """Wraps one trained model. Build it once and pass it around."""

    def __init__(self, model_dir: Path | str = DATA_DIR) -> None:
        self.model_dir = Path(model_dir)
        model_path = self.model_dir / MODEL_FILE
        features_path = self.model_dir / FEATURES_FILE
        if not model_path.exists() or not features_path.exists():
            raise ModelNotAvailable(
                f"no trained model in {self.model_dir}. Run "
                f"`python -m src.model.train` first; TRIAGE will not guess."
            )

        import lightgbm as lgb

        self._booster = lgb.Booster(model_file=str(model_path))
        manifest = json.loads(features_path.read_text(encoding="utf-8"))
        self.feature_names: list[str] = list(manifest["features"])
        self.categorical: list[str] = list(manifest.get("categorical", []))

    def validate(self, features: dict[str, Any]) -> None:
        expected = set(self.feature_names)
        given = set(features)
        missing, unexpected = sorted(expected - given), sorted(given - expected)
        if missing or unexpected:
            raise FeatureDrift(missing, unexpected)

    def score(self, features: dict[str, Any]) -> float:
        """P(this attempt succeeds). One row in, one probability out."""
        return self.score_batch([features])[0]

    def score_batch(self, rows: Sequence[dict[str, Any]]) -> list[float]:
        """Scoring candidates in one call — the treatment arm enumerates many."""
        if not rows:
            return []
        import pandas as pd

        for row in rows:
            self.validate(row)
        frame = pd.DataFrame([{k: row[k] for k in self.feature_names} for row in rows])
        for column in self.categorical:
            frame[column] = frame[column].astype("category")
        return [float(p) for p in self._booster.predict(frame)]


_DEFAULT: Scorer | None = None


def get_scorer(model_dir: Path | str = DATA_DIR) -> Scorer:
    """Process-wide scorer, loaded once."""
    global _DEFAULT
    if _DEFAULT is None or Path(model_dir) != _DEFAULT.model_dir:
        _DEFAULT = Scorer(model_dir)
    return _DEFAULT


def score(features: dict[str, Any], model_dir: Path | str = DATA_DIR) -> float:
    return get_scorer(model_dir).score(features)


def reset_cache() -> None:
    """Drop the cached scorer. For tests that write a model mid-run."""
    global _DEFAULT
    _DEFAULT = None
