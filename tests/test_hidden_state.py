"""I-12 — the simulator's hidden state is never readable by the decision path.

The world knows the customer's true balance, real salary date, card validity, PIN and
device binding, responsiveness, and the outage timeline. If policy or the executor
could read any of it, the whole evaluation is circular: a lookup table graded against
its own answer key.

Two guarantees are asserted here, and both are structural rather than conventional:

1. ``AttemptOutcome`` carries four fields and nothing that names latent state.
2. ``src/policy/``, ``src/executor/`` and ``src/arms/`` have no import edge into
   ``src.simulator`` at all — the runner receives the world as an argument, typed by
   a Protocol it declares itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.simulator.world import AttemptOutcome, World

SRC = Path(__file__).resolve().parents[1] / "src"

EXPECTED_OUTCOME_FIELDS = {"success", "error_code", "error_source", "latency_ms"}

# Anything naming latent state. Substring match, so `true_balance`, `balance_paise`
# and `customer_balance` all trip it.
LATENT_TOKENS: tuple[str, ...] = (
    "balance",
    "salary",
    "burn",
    "income",
    "valid_until",
    "expiry",
    "card_valid",
    "pin_set",
    "device_bound",
    "clumsi",
    "responsive",
    "propensity",
    "limit_prone",
    "constrained",
    "outage",
    "latent",
    "hidden",
    "truth",
    "true_",
    "oracle",
    "world",
    "seed",
    "debug",
)

# Modules that must never reach into the simulator.
DECISION_PACKAGES = ("policy", "executor", "arms")


def _decision_path_files() -> list[Path]:
    files: list[Path] = []
    for package in DECISION_PACKAGES:
        directory = SRC / package
        if directory.exists():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
    return modules


# -- the outcome contract -----------------------------------------------------


def test_attempt_outcome_has_exactly_four_fields() -> None:
    assert set(AttemptOutcome.__dataclass_fields__) == EXPECTED_OUTCOME_FIELDS


@pytest.mark.parametrize("token", LATENT_TOKENS)
def test_attempt_outcome_names_nothing_latent(token: str) -> None:
    offenders = [f for f in AttemptOutcome.__dataclass_fields__ if token in f.lower()]
    assert offenders == [], f"AttemptOutcome leaks latent state through {offenders}"


def test_attempt_outcome_is_frozen() -> None:
    outcome = AttemptOutcome(True, None, None, 100)
    with pytest.raises((AttributeError, TypeError)):
        outcome.success = False  # type: ignore[misc]


def test_attempt_outcome_carries_no_extra_attributes() -> None:
    # slots=True means an instance has no __dict__, so it cannot grow a debug
    # payload after construction however hard a caller tries.
    outcome = AttemptOutcome(False, "insufficient_funds", "bank", 1200)
    assert not hasattr(outcome, "__dict__")
    with pytest.raises((AttributeError, TypeError)):
        outcome.latent_balance = 1  # type: ignore[attr-defined]


def test_world_returns_only_attempt_outcomes(world: World) -> None:
    from src.store.db import CaseView

    from tests.conftest import NOW

    outcome = world.attempt(
        CaseView(
            id="case_HIDDEN",
            customer_id="cust_HIDDEN",
            merchant_id="mch_HIDDEN",
            method="upi",
            rail="@oksbi",
            amount_paise=499000,
            error_code="insufficient_funds",
            failed_at=NOW,
            attempt_number=1,
        ),
        "RETRY_SCHEDULED",
        None,
        NOW,
    )
    assert type(outcome) is AttemptOutcome
    assert not hasattr(outcome, "__dict__")
    assert set(AttemptOutcome.__dataclass_fields__) == EXPECTED_OUTCOME_FIELDS


# -- the observable projection ------------------------------------------------


def test_customer_profile_exposes_only_observable_fields(world: World) -> None:
    # research/05 §5.1 puts exactly these on the decide request; a real gateway holds
    # them already. Nothing else about the customer may leave the world.
    profile = world.customer_profile("cust_HIDDEN")
    assert set(profile) == {"city_tier", "payer_bank", "vpa_handle"}


def test_merchant_profile_exposes_only_observable_fields(world: World) -> None:
    assert set(world.merchant_profile("mch_HIDDEN")) == {"mcc", "ticket_band"}


def test_latent_customer_state_is_private() -> None:
    from src.simulator import world as world_module

    # Named with a leading underscore and never exported. If these ever become part
    # of the public surface, the boundary has already been crossed.
    assert hasattr(world_module, "_CustomerLatent")
    assert hasattr(world_module, "_MerchantLatent")


# -- the module boundary ------------------------------------------------------


def test_there_are_decision_path_files_to_check() -> None:
    assert len(_decision_path_files()) >= 3


@pytest.mark.parametrize(
    "path", _decision_path_files(), ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_decision_path_never_imports_the_simulator(path: Path) -> None:
    offenders = sorted(
        module
        for module in _imported_modules(path)
        if module == "src.simulator" or module.startswith("src.simulator.")
    )
    assert offenders == [], (
        f"{path.relative_to(SRC.parent)} imports {offenders}. The runner receives "
        f"the world as an injected argument; it must not reach for it."
    )


def test_runner_declares_the_world_structurally() -> None:
    # The type exists so the executor can be type-checked against the world without
    # importing it. Removing it would invite a real import back in.
    from src.executor.runner import AttemptResolver, OutcomeLike

    assert set(OutcomeLike.__annotations__) == EXPECTED_OUTCOME_FIELDS
    assert hasattr(AttemptResolver, "attempt")


def test_policy_engine_module_imports_nothing_from_the_project_but_stdlib() -> None:
    # The policy table is the root of the dependency graph. If it grew an import of
    # anything under src/, the "table decides, nothing else does" claim would weaken.
    modules = _imported_modules(SRC / "policy" / "engine.py")
    assert [m for m in modules if m.startswith("src.")] == []
