"""A4 + A5 — determinism and cold boot.

**A4.** Run both scenarios twice at seed 42 and assert the scored report is identical
both as JSON and as rendered markdown. This has held since Stage 2; the point of
re-running it here is that the population is now 8000 payments across three arms, and
the treatment arm calls a model — the most likely place for an ordering dependency or
a floating-point drift to creep in.

**A5.** Boot the API against a store that does not exist yet and assert `/health` and
the evaluation endpoints still work. Catches anything that only works because a stale
file happens to be lying around.

    uv run python scripts/verify_determinism.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.report import build, render  # noqa: E402
from eval.run_arms import run  # noqa: E402

ARMS = ["control", "baseline", "treatment"]
PASS, FAIL = 0, 0
LINES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    LINES.append(f"{mark}|{name}|{detail}")
    print(f"  {mark:<4} {name:<56} {detail}")


def determinism(scenario: str, workdir: Path) -> None:
    """Two full runs, same seed, same everything."""
    payloads, markdowns = [], []
    for label in ("a", "b"):
        result = run(
            seed=42,
            n_payments=8000,
            days=30,
            scenario=scenario,
            arms=ARMS,
            trailing_days=7,
            db_path=workdir / f"{scenario}-{label}.db",
        )
        card = build(result.db_path, result.run_id)
        from eval.score import to_api_shape

        payloads.append(json.dumps(to_api_shape(card), sort_keys=True))
        markdowns.append(render(card))

    check(
        f"{scenario}: report JSON identical across two runs",
        payloads[0] == payloads[1],
        f"{len(payloads[0])} bytes",
    )
    check(
        f"{scenario}: rendered markdown identical",
        markdowns[0] == markdowns[1],
        f"{len(markdowns[0])} bytes",
    )
    body = json.loads(payloads[0])
    arms = body["arms"]
    detail = " / ".join(f"{a} {arms[a]['rate']:.3f}" for a in ARMS if a in arms)
    check(f"{scenario}: three arms scored", len(arms) == 3, detail)
    gaps = body["uplift"]
    check(
        f"{scenario}: both gaps reported separately",
        "baseline_vs_control" in gaps and "treatment_vs_baseline" in gaps,
        f"taxonomy {gaps['baseline_vs_control']['pp']:+.1f}pp · "
        f"model {gaps['treatment_vs_baseline']['pp']:+.1f}pp",
    )


def cold_boot() -> None:
    """Boot against a store that does not exist and a runs dir that does."""
    import os

    from fastapi.testclient import TestClient

    from src.api.main import create_app

    scratch = Path(tempfile.mkdtemp())
    missing_db = scratch / "does-not-exist.db"
    check("cold-boot store absent before boot", not missing_db.exists(), str(missing_db))

    os.environ["TRIAGE_DB_DIR"] = str(ROOT / "eval" / "runs")
    with TestClient(create_app(db_path=missing_db)) as client:
        health = client.get("/health")
        check("cold boot: GET /health", health.status_code == 200, str(health.status_code))
        check(
            "cold boot: 110 codes loaded",
            health.json().get("policy_codes_loaded") == 110,
            str(health.json().get("policy_codes_loaded")),
        )
        runs = client.get("/v1/eval/runs")
        check("cold boot: GET /v1/eval/runs", runs.status_code == 200)
        items = runs.json()["items"]
        check("cold boot: committed runs are visible", len(items) >= 1, f"{len(items)} runs")
        if items:
            report = client.get(f"/v1/eval/report/{items[0]['run_id']}")
            check(
                "cold boot: GET /v1/eval/report/{id}",
                report.status_code == 200,
                items[0]["run_id"],
            )
        decide = client.post(
            "/v1/recovery/decide",
            json={"error_code": "insufficient_funds", "now": 1737025200, "method": "upi"},
        )
        check(
            "cold boot: POST /decide still decides",
            decide.status_code == 200
            and decide.json()["action"] == "RETRY_SCHEDULED",
            decide.json().get("action", "?"),
        )
    check("cold boot: store created on demand", missing_db.exists(), "schema written")
    shutil.rmtree(scratch, ignore_errors=True)


def read_only_mode() -> None:
    """A deployed instance must refuse to mutate the store."""
    from fastapi.testclient import TestClient

    from src.api.main import create_app

    scratch = Path(tempfile.mkdtemp())
    store = scratch / "ro.db"
    from src.store import db

    db.open_db(store).close()

    with TestClient(create_app(db_path=store, read_only=True)) as client:
        check(
            "read-only: /health advertises it",
            client.get("/health").json().get("read_only") is True,
        )
        run_attempt = client.post("/v1/simulator/run", json={"n_payments": 10, "days": 2})
        check(
            "read-only: POST /v1/simulator/run -> 503",
            run_attempt.status_code == 503,
            str(run_attempt.status_code),
        )
        check(
            "read-only: 503 carries the envelope",
            set(run_attempt.json().get("error", {}))
            == {"code", "description", "field", "source", "step", "reason"},
        )
        case = client.post(
            "/v1/recovery/cases",
            json={
                "payment_id": "pay_RO",
                "error_code": "insufficient_funds",
                "method": "upi",
                "amount": 1000,
                "failed_at": 1737025200,
            },
        )
        check(
            "read-only: POST /v1/recovery/cases -> 503",
            case.status_code == 503,
            str(case.status_code),
        )
        decide = client.post(
            "/v1/recovery/decide",
            json={"error_code": "card_expired", "now": 1737025200},
        )
        check(
            "read-only: POST /decide still works (stateless)",
            decide.status_code == 200,
            decide.json().get("action", "?"),
        )
        check(
            "read-only: taxonomy still readable",
            client.get("/v1/errors/meta/coverage").json()["recoverable_codes"] == 27,
        )
    shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    print("TRIAGE determinism, cold boot and read-only verification")
    workdir = Path(tempfile.mkdtemp())
    try:
        print("\nA4 — determinism at 8000 payments, three arms")
        for scenario in ("normal", "bank_outage"):
            determinism(scenario, workdir)

        print("\nA5 — cold boot")
        cold_boot()

        print("\nB1 — read-only mode")
        read_only_mode()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "-" * 64)
    print(f"PASS {PASS}   FAIL {FAIL}")
    (ROOT / ".verify").mkdir(exist_ok=True)
    (ROOT / ".verify" / "determinism.txt").write_text(
        "\n".join(LINES) + "\n", encoding="utf-8"
    )
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
