"""Deterministic policy engine.

Loads ``error_policy.json`` and answers exactly one question: what does the decision
table say about this error code? Nothing here knows about a specific payment — that
arrives in Stage 2.

Two rules define this module:

* I-1 — the table decides the action class. Nothing else ever does.
* I-2 — an unknown code raises. There is no default branch and no fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Repo root: src/policy/engine.py -> src/policy -> src -> TRIAGE
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "error_policy.json"

# The eight action classes, in the canonical order used by CLAUDE.md and the Stage 5
# taxonomy board. The order is part of the contract: /v1/errors/meta/actions renders
# in this sequence.
ACTIONS: tuple[str, ...] = (
    "RETRY_NOW",
    "RETRY_SCHEDULED",
    "SWITCH_RAIL",
    "SWITCH_INSTRUMENT",
    "NUDGE_CUSTOMER",
    "AWAIT_STATUS",
    "STOP",
    "MERCHANT_ALERT",
)

# Silently recoverable: the three classes that may schedule another attempt.
# The 27-of-110 finding is exactly the membership of this set. (I-4)
RETRYING_ACTIONS: frozenset[str] = frozenset(
    {"RETRY_NOW", "RETRY_SCHEDULED", "SWITCH_RAIL"}
)

# The only classes the Stage 4 model is ever consulted for, and then only to rank
# candidates *within* the class the table already permitted. (I-1)
MODEL_ELIGIBLE_ACTIONS: frozenset[str] = frozenset({"RETRY_SCHEDULED", "SWITCH_RAIL"})

FAMILIES: tuple[str, ...] = ("A", "B", "S", "X")

FAMILY_LABELS: dict[str, str] = {
    "A": "Cards and netbanking",
    "B": "UPI and wallets",
    "S": "Shared across methods",
    "X": "Merchant configuration",
}

# Fields every entry in error_policy.json must carry.
REQUIRED_FIELDS: tuple[str, ...] = (
    "code",
    "family",
    "action",
    "min_wait_hours",
    "recoverable",
    "policy_note",
    "razorpay_explanation",
    "razorpay_next_steps",
)

_TEXT_FIELDS: tuple[str, ...] = (
    "policy_note",
    "razorpay_explanation",
    "razorpay_next_steps",
)

_UNMAPPED = "UNMAPPED"


class PolicyError(Exception):
    """Base for every policy-engine failure."""


class PolicyLoadError(PolicyError):
    """The policy table is missing, unreadable, or internally inconsistent.

    Carries every problem found, not just the first. A broken table must fail at
    startup, not surface as a wrong decision three stages later.
    """

    def __init__(self, message: str, problems: Iterable[str] = ()) -> None:
        self.problems: list[str] = list(problems)
        if self.problems:
            detail = "\n".join(f"  - {p}" for p in self.problems)
            message = f"{message}\n{detail}"
        super().__init__(message)


class UnknownErrorCodeError(PolicyError):
    """The code is not in the table.

    Never downgrade this to a default action: defaulting an unrecognised code to
    RETRY_SCHEDULED is the single most dangerous shortcut available here. (I-2)
    """

    def __init__(self, code: Any) -> None:
        self.code = code
        super().__init__(f"{code!r} is not a recognised payment failure reason")


@dataclass(frozen=True, slots=True)
class PolicyEntry:
    """One row of the decision table. Immutable — callers never mutate policy."""

    code: str
    family: str
    action: str
    min_wait_hours: int
    recoverable: bool
    policy_note: str
    razorpay_explanation: str
    razorpay_next_steps: str

    @property
    def is_retrying(self) -> bool:
        """True when this class may schedule another attempt on the same payment."""
        return self.action in RETRYING_ACTIONS

    @property
    def is_model_eligible(self) -> bool:
        """True when the Stage 4 model may rank executions within this class. (I-1)"""
        return self.action in MODEL_ELIGIBLE_ACTIONS


class PolicyEngine:
    """Owns one loaded copy of the decision table.

    Deliberately not a module-level singleton: the API builds one instance in its
    lifespan and hangs it off ``app.state``; tests build their own.
    """

    def __init__(self, path: Path | str = DEFAULT_POLICY_PATH) -> None:
        self.path = Path(path)
        self._entries: dict[str, PolicyEntry] = {}
        self._actions: dict[str, str] = {}
        self._version: str = ""
        self._source: str = ""
        self._loaded = False

    # -- loading --------------------------------------------------------------

    def load(self) -> "PolicyEngine":
        """Read, parse and validate the table. Returns self so calls can chain.

        Raises PolicyLoadError on anything wrong with the file or its contents.
        """
        raw = self._read()
        problems: list[str] = []

        version = raw.get("version")
        if not isinstance(version, str) or not version.strip():
            problems.append("top level: 'version' is missing or not a non-empty string")
            version = ""

        source = raw.get("source")
        if not isinstance(source, str):
            source = ""

        actions = raw.get("actions")
        if not isinstance(actions, dict):
            problems.append("top level: 'actions' is missing or not an object")
            actions = {}
        else:
            missing = [a for a in ACTIONS if a not in actions]
            extra = [a for a in actions if a not in ACTIONS]
            if missing:
                problems.append(f"actions: missing description(s) for {sorted(missing)}")
            if extra:
                problems.append(f"actions: unrecognised class(es) {sorted(extra)}")
            for name, desc in actions.items():
                if not isinstance(desc, str) or not desc.strip():
                    problems.append(f"actions.{name}: description is empty")

        codes = raw.get("codes")
        if not isinstance(codes, list) or not codes:
            problems.append("top level: 'codes' is missing, not a list, or empty")
            codes = []

        entries: dict[str, PolicyEntry] = {}
        for index, row in enumerate(codes):
            entry = self._validate_row(index, row, entries, problems)
            if entry is not None:
                entries[entry.code] = entry

        if problems:
            raise PolicyLoadError(
                f"{self.path} failed validation ({len(problems)} problem(s))",
                problems,
            )

        self._entries = entries
        self._actions = {name: actions[name] for name in ACTIONS}
        self._version = version
        self._source = source
        self._loaded = True
        return self

    def _read(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyLoadError(
                f"cannot read policy table at {self.path}: {exc}"
            ) from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyLoadError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise PolicyLoadError(
                f"{self.path} must contain a JSON object at the top level"
            )
        return raw

    def _validate_row(
        self,
        index: int,
        row: Any,
        seen: dict[str, PolicyEntry],
        problems: list[str],
    ) -> PolicyEntry | None:
        """Check one row. Appends every problem found; returns None if unusable."""
        where = f"codes[{index}]"
        if not isinstance(row, dict):
            problems.append(f"{where}: not an object")
            return None

        code = row.get("code")
        if not isinstance(code, str) or not code.strip():
            problems.append(f"{where}: 'code' is missing or not a non-empty string")
            return None
        where = f"codes[{index}] {code}"

        missing = [f for f in REQUIRED_FIELDS if f not in row]
        if missing:
            problems.append(f"{where}: missing field(s) {missing}")
            return None

        if code in seen:
            problems.append(f"{where}: duplicate code, already defined earlier")
            return None

        ok = True

        family = row["family"]
        if family not in FAMILIES:
            problems.append(f"{where}: family {family!r} is not one of {list(FAMILIES)}")
            ok = False

        action = row["action"]
        if action == _UNMAPPED:
            problems.append(f"{where}: action is still {_UNMAPPED}")
            ok = False
        elif action not in ACTIONS:
            problems.append(f"{where}: action {action!r} is not one of the eight classes")
            ok = False

        recoverable = row["recoverable"]
        if not isinstance(recoverable, bool):
            problems.append(
                f"{where}: recoverable must be a boolean, "
                f"got {type(recoverable).__name__}"
            )
            ok = False
        elif action in ACTIONS:
            expected = action in RETRYING_ACTIONS
            if recoverable is not expected:
                problems.append(
                    f"{where}: recoverable={recoverable} disagrees with action "
                    f"{action} (expected {expected}; recoverable means the class "
                    f"may retry without human intervention)"
                )
                ok = False

        min_wait = row["min_wait_hours"]
        if isinstance(min_wait, bool) or not isinstance(min_wait, int):
            problems.append(
                f"{where}: min_wait_hours must be an integer, "
                f"got {type(min_wait).__name__}"
            )
            ok = False
        elif min_wait < 0:
            problems.append(f"{where}: min_wait_hours is negative ({min_wait})")
            ok = False
        elif min_wait > 0 and action not in RETRYING_ACTIONS:
            problems.append(
                f"{where}: min_wait_hours={min_wait} on non-retrying action {action} "
                f"— only {sorted(RETRYING_ACTIONS)} may schedule a wait (I-4)"
            )
            ok = False

        for field in _TEXT_FIELDS:
            value = row[field]
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}: {field} is empty")
                ok = False
            elif _UNMAPPED in value:
                problems.append(f"{where}: {field} is still marked {_UNMAPPED}")
                ok = False

        if not ok:
            return None

        return PolicyEntry(
            code=code,
            family=family,
            action=action,
            min_wait_hours=min_wait,
            recoverable=recoverable,
            policy_note=row["policy_note"],
            razorpay_explanation=row["razorpay_explanation"],
            razorpay_next_steps=row["razorpay_next_steps"],
        )

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise PolicyLoadError(
                "policy table has not been loaded; call PolicyEngine.load() first"
            )

    # -- reading --------------------------------------------------------------

    @property
    def version(self) -> str:
        self._require_loaded()
        return self._version

    @property
    def source(self) -> str:
        self._require_loaded()
        return self._source

    @property
    def codes(self) -> list[str]:
        """Every code in the table, sorted."""
        self._require_loaded()
        return sorted(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, code: object) -> bool:
        return code in self._entries

    def resolve(self, code: str) -> PolicyEntry:
        """Look up one code. Exact match only.

        No normalisation, no case folding, no fuzzy matching: ``INSUFFICIENT_FUNDS``
        and ``insufficient_fund`` are unknown codes and must raise. (I-2)
        """
        self._require_loaded()
        try:
            return self._entries[code]
        except (KeyError, TypeError):
            raise UnknownErrorCodeError(code) from None

    def list_entries(
        self,
        *,
        family: str | None = None,
        action: str | None = None,
        recoverable: bool | None = None,
    ) -> list[PolicyEntry]:
        """Filtered view of the table, sorted by code.

        Raises ValueError on an unrecognised filter value. The API validates query
        parameters before it gets here and returns a 400; this guard is for direct
        callers, so a typo cannot silently return an empty list.
        """
        self._require_loaded()
        if family is not None and family not in FAMILIES:
            raise ValueError(f"family must be one of {list(FAMILIES)}, got {family!r}")
        if action is not None and action not in ACTIONS:
            raise ValueError(f"action must be one of {list(ACTIONS)}, got {action!r}")

        return [
            entry
            for entry in (self._entries[code] for code in sorted(self._entries))
            if (family is None or entry.family == family)
            and (action is None or entry.action == action)
            and (recoverable is None or entry.recoverable is recoverable)
        ]

    def action_description(self, action: str) -> str:
        self._require_loaded()
        return self._actions[action]

    def counts_by_action(self) -> dict[str, int]:
        """Counts for all eight classes, including any that are zero."""
        self._require_loaded()
        counts = dict.fromkeys(ACTIONS, 0)
        for entry in self._entries.values():
            counts[entry.action] += 1
        return counts

    def counts_by_family(self) -> dict[str, int]:
        """Counts for all four families, including any that are zero."""
        self._require_loaded()
        counts = dict.fromkeys(FAMILIES, 0)
        for entry in self._entries.values():
            counts[entry.family] += 1
        return counts

    def actions_catalogue(self) -> list[dict[str, Any]]:
        """The eight classes with descriptions and counts, in canonical order."""
        self._require_loaded()
        counts = self.counts_by_action()
        return [
            {
                "action": action,
                "description": self._actions[action],
                "code_count": counts[action],
                "recoverable": action in RETRYING_ACTIONS,
                "schedules_retry": action in RETRYING_ACTIONS,
                "model_eligible": action in MODEL_ELIGIBLE_ACTIONS,
            }
            for action in ACTIONS
        ]

    def coverage_summary(self) -> dict[str, Any]:
        """The 27-of-110 finding, in the shape the Stage 5 taxonomy board reads."""
        self._require_loaded()
        total = len(self._entries)
        by_action = self.counts_by_action()
        by_family = self.counts_by_family()
        recoverable = sum(1 for e in self._entries.values() if e.recoverable)

        family_recoverable: dict[str, int] = dict.fromkeys(FAMILIES, 0)
        for entry in self._entries.values():
            if entry.recoverable:
                family_recoverable[entry.family] += 1

        return {
            "policy_version": self._version,
            "source": self._source,
            "total_codes": total,
            "recoverable_codes": recoverable,
            "unrecoverable_codes": total - recoverable,
            "recoverable_share": round(recoverable / total, 4) if total else 0.0,
            "recoverable_actions": [a for a in ACTIONS if a in RETRYING_ACTIONS],
            "model_eligible_actions": [
                a for a in ACTIONS if a in MODEL_ELIGIBLE_ACTIONS
            ],
            "by_action": [
                {
                    "action": action,
                    "count": by_action[action],
                    "recoverable": action in RETRYING_ACTIONS,
                    "description": self._actions[action],
                }
                for action in ACTIONS
            ],
            "by_family": [
                {
                    "family": family,
                    "label": FAMILY_LABELS[family],
                    "count": by_family[family],
                    "recoverable_count": family_recoverable[family],
                }
                for family in FAMILIES
            ],
            "headline": (
                f"{recoverable} of {total} published failure reasons are recoverable "
                f"without human intervention."
            ),
        }
