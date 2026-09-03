"""The scoring interface refuses rather than degrades.

Two failure modes that would otherwise be silent:

* **A missing model.** Falling back to a heuristic would let the treatment arm run as a
  slightly different baseline while still being labelled treatment, and the eval would
  compare two things that are not what the report says they are.
* **Feature drift.** Filling absent features with zeros is how a model quietly starts
  scoring something other than what it was trained on. Extra features are just as bad:
  they mean the caller and the model disagree about what a row is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.model.score import FeatureDrift, ModelNotAvailable, Scorer, reset_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def _train_tiny(directory: Path) -> None:
    """A two-feature model, trained in-process, purely to have a real booster."""
    import lightgbm as lgb
    import pandas as pd

    frame = pd.DataFrame(
        {
            "a": [float(i % 7) for i in range(400)],
            "b": [float(i % 3) for i in range(400)],
            "label": [1 if i % 2 == 0 else 0 for i in range(400)],
        }
    )
    booster = lgb.train(
        {"objective": "binary", "verbosity": -1, "min_data_in_leaf": 5},
        lgb.Dataset(frame[["a", "b"]], label=frame["label"]),
        num_boost_round=5,
    )
    directory.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(directory / "model.txt"))
    (directory / "feature_names.json").write_text(
        json.dumps({"features": ["a", "b"], "categorical": []}), encoding="utf-8"
    )


# -- a missing model raises ---------------------------------------------------


def test_missing_model_raises(tmp_path: Path) -> None:
    with pytest.raises(ModelNotAvailable):
        Scorer(tmp_path)


def test_missing_feature_manifest_raises(tmp_path: Path) -> None:
    (tmp_path / "model.txt").write_text("not a model", encoding="utf-8")
    with pytest.raises(ModelNotAvailable):
        Scorer(tmp_path)


def test_the_error_names_the_fix(tmp_path: Path) -> None:
    with pytest.raises(ModelNotAvailable) as excinfo:
        Scorer(tmp_path)
    assert "src.model.train" in str(excinfo.value)


def test_there_is_no_heuristic_fallback(tmp_path: Path) -> None:
    """`score` must raise, not return a default probability."""
    from src.model import score as score_module

    with pytest.raises(ModelNotAvailable):
        score_module.score({"a": 1.0}, model_dir=tmp_path)


# -- feature drift raises -----------------------------------------------------


def test_a_missing_feature_raises(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    scorer = Scorer(tmp_path)
    with pytest.raises(FeatureDrift) as excinfo:
        scorer.score({"a": 1.0})
    assert excinfo.value.missing == ["b"]


def test_an_unexpected_feature_raises(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    scorer = Scorer(tmp_path)
    with pytest.raises(FeatureDrift) as excinfo:
        scorer.score({"a": 1.0, "b": 2.0, "c": 3.0})
    assert excinfo.value.unexpected == ["c"]


def test_drift_is_not_silently_filled_with_zeros(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    scorer = Scorer(tmp_path)
    with pytest.raises(FeatureDrift):
        scorer.score({})


def test_the_drift_message_names_both_sides(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    with pytest.raises(FeatureDrift) as excinfo:
        Scorer(tmp_path).score({"a": 1.0, "z": 0.0})
    message = str(excinfo.value)
    assert "b" in message and "z" in message


# -- the happy path -----------------------------------------------------------


def test_a_matching_row_scores(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    probability = Scorer(tmp_path).score({"a": 1.0, "b": 2.0})
    assert 0.0 <= probability <= 1.0


def test_batch_scoring_matches_single_scoring(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    scorer = Scorer(tmp_path)
    rows = [{"a": float(i), "b": float(i % 3)} for i in range(5)]
    batch = scorer.score_batch(rows)
    singles = [scorer.score(row) for row in rows]
    assert batch == pytest.approx(singles)


def test_an_empty_batch_is_empty(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    assert Scorer(tmp_path).score_batch([]) == []


def test_batch_validates_every_row(tmp_path: Path) -> None:
    _train_tiny(tmp_path)
    with pytest.raises(FeatureDrift):
        Scorer(tmp_path).score_batch([{"a": 1.0, "b": 2.0}, {"a": 1.0}])


def test_feature_order_does_not_matter(tmp_path: Path) -> None:
    """Rows are reindexed by name, so a differently-ordered dict scores the same."""
    _train_tiny(tmp_path)
    scorer = Scorer(tmp_path)
    assert scorer.score({"a": 1.0, "b": 2.0}) == scorer.score({"b": 2.0, "a": 1.0})


def test_the_model_is_loaded_once(tmp_path: Path) -> None:
    from src.model import score as score_module

    _train_tiny(tmp_path)
    first = score_module.get_scorer(tmp_path)
    second = score_module.get_scorer(tmp_path)
    assert first is second


# -- the real artefacts, when they exist --------------------------------------


def test_the_trained_model_matches_the_declared_feature_set() -> None:
    """If the model on disk was trained against a different feature list, the
    treatment arm would raise FeatureDrift on its first decision."""
    from src.features.build import FEATURE_NAMES
    from src.model.dataset import DATA_DIR

    manifest = DATA_DIR / "feature_names.json"
    if not manifest.exists():
        pytest.skip("no trained model on disk")
    declared = json.loads(manifest.read_text(encoding="utf-8"))["features"]
    assert declared == list(FEATURE_NAMES)
