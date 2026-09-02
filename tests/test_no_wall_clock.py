"""No wall clock anywhere in src/.

Every function that needs the current time takes ``now`` (unix seconds) as an explicit
argument. A 30-day simulation has to run in seconds, which is impossible if anything
underneath reads the system clock — and a run that depends on when it was started is
not reproducible, which would quietly break arm comparison in Stage 3.

Detection is AST-based rather than a text grep. A grep would fire on the prose in
``src/api/main.py``, which names ``datetime.now()`` precisely to say it is banned, and
would miss an aliased import. Parsing the code sees calls and only calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Dotted call names that read the system clock.
BANNED_CALLS: frozenset[str] = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.perf_counter",
        "time.localtime",
        "time.gmtime",
    }
)

# Bare names that would be a clock read however they were imported.
BANNED_BARE: frozenset[str] = frozenset({"utcnow", "time_ns"})


def _dotted(node: ast.AST) -> str | None:
    """Render ``a.b.c`` from an attribute/name chain, or None if it is not one."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def clock_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if dotted is None:
            continue
        # Match on the tail so `datetime.datetime.now()` is caught too.
        for banned in BANNED_CALLS:
            if dotted == banned or dotted.endswith("." + banned):
                found.append((node.lineno, dotted))
        tail = dotted.rsplit(".", 1)[-1]
        if tail in BANNED_BARE:
            found.append((node.lineno, dotted))
    return found


def test_src_has_python_files_to_scan() -> None:
    # Guards against the scan silently passing because it found nothing to read.
    assert len(source_files()) >= 10


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_clock_reads(path: Path) -> None:
    offenders = clock_calls(path)
    assert offenders == [], (
        f"{path.relative_to(SRC.parent)} reads the system clock at "
        f"{offenders}. Take `now: int` as an argument instead."
    )


def test_the_detector_actually_detects(tmp_path: Path) -> None:
    # A test that cannot fail is not a test. Prove the scanner bites.
    sample = tmp_path / "offender.py"
    sample.write_text(
        "import time\n"
        "from datetime import datetime\n"
        "def f():\n"
        "    return datetime.now(), time.time()\n",
        encoding="utf-8",
    )
    assert len(clock_calls(sample)) == 2


def test_the_detector_ignores_prose(tmp_path: Path) -> None:
    # `src/api/main.py` names datetime.now() in its docstring to say it is banned.
    sample = tmp_path / "prose.py"
    sample.write_text(
        '"""Nothing here calls datetime.now() or time.time()."""\n'
        "# not even in a comment: date.today()\n"
        "X = 'datetime.utcnow()'\n",
        encoding="utf-8",
    )
    assert clock_calls(sample) == []


def test_conversion_helpers_are_not_flagged(tmp_path: Path) -> None:
    # fromtimestamp turns a passed-in integer into a civil date. That is arithmetic,
    # not a clock read, and the simulator relies on it.
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from datetime import datetime, timezone\n"
        "def f(ts):\n"
        "    return datetime.fromtimestamp(ts, tz=timezone.utc)\n",
        encoding="utf-8",
    )
    assert clock_calls(sample) == []
