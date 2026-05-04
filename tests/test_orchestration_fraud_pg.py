"""Live-Postgres tests for Tier 4 v3 fraud-risk asset checks.

Step 2.5 of the v3 fraud-engine plan wires the L1 dispatcher
(``derived.refresh_all_fraud_signal_observations``) into Dagster as the
``derived.fraud_signal_observation`` asset, and adds three asset checks
on top:

* ``row_count_positive``                 -- L1 must have rows when cn is loaded
* ``signal_coverage``                    -- all 8 signal_ids present after
                                            a refresh
* ``risk_score_positive_when_l1_present`` -- L3a must surface at least one
                                            entity with risk_score > 0

The check FUNCTION shapes are pinned by the registry tests in
``test_orchestration.py``; what those cannot exercise is whether the SQL
inside each check is correct against a real Postgres + a populated v3
chain. That's what this file does.

Two scenarios:

(A) Clean DB, no candidate rows. Every check must vacuous-pass with the
    documented ``reason`` value. This is the contract that protects the
    pre-loader window: a fresh deployment must not page the operator
    just because cn/cm haven't been ingested yet.

(B) Fully populated DB (engineered anomalies trigger every v2.A signal).
    Every check must pass cleanly, with metadata that matches the L1 +
    L3a state produced by the dispatcher.

We DO NOT test the failure cases here (e.g., "what if a refresher silently
dropped to zero rows") because crafting a DB state where one of the eight
plpgsql refreshers returns empty would mean either monkey-patching the
function (defeats the point) or seeding raw data that triggers exactly
seven of eight signals (brittle and high-effort). The registry tests in
test_orchestration.py already pin that all three checks are wired in;
the SQL inside each is straightforward (a COUNT + a GROUP BY) and is
covered by the success-path assertions on metadata below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.test_migrations_fraud_signal_adapter import (
    _run_dispatcher,
    _seed_engineered_anomalies,
)

if TYPE_CHECKING:
    import psycopg


pytestmark = pytest.mark.live_pg


# ---------------------------------------------------------------------------
# Fixture: clean DB with all migrations + seeds applied.
# ---------------------------------------------------------------------------


@pytest.fixture
def fraud_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply migrations + seeds against a clean DB; yield the conn."""
    from scripts.migrate import (
        MIGRATIONS_DIR,
        SEEDS_DIR,
        apply_migrations,
        discover,
    )

    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
        cur.execute(
            "DO $$ "
            "DECLARE r record; "
            "BEGIN "
            "  FOR r IN SELECT viewname FROM pg_views "
            "           WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) "
            "            || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _pg_resource_for(conn: psycopg.Connection) -> Any:
    """Wrap an already-connected psycopg.Connection in a PgResource-like shim.

    The asset-check functions take ``pg: PgResource`` and call
    ``pg.connect()`` as a context manager. The simplest way to thread a
    test conn through is a local shim with the same surface. We commit
    on exit so ``governance.dataset_health`` rows from
    ``GovernanceWriter.emit`` are visible to the next assertion.
    """
    from contextlib import contextmanager

    class _Shim:
        @contextmanager
        def connect(self) -> Any:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    return _Shim()


class _CapturingGovernance:
    """Stand-in for ``GovernanceWriter`` that captures emissions in-memory.

    The asset-check functions call ``governance.emit(HealthSignal(...))``
    on every run. The capture stub validates the input via
    ``GovernanceWriter.emit``'s severity gate (so we don't lose the
    "rejects invalid severity" contract) and stores the signal so a
    test can assert on it. We DO NOT round-trip the signal through
    ``governance.dataset_health`` here -- ``conn.info.dsn`` does not
    expose the password, so a real ``GovernanceWriter`` cannot open a
    fresh connection. The governance write path is independently
    exercised by ``test_governance_signal_rejects_invalid_severity``
    in ``test_orchestration.py``.
    """

    def __init__(self) -> None:
        from orchestration.resources import HealthSignal

        self._HealthSignal = HealthSignal
        self.emitted: list[Any] = []

    def emit(self, signal: Any) -> None:
        from orchestration.resources import _VALID_SEVERITIES

        if signal.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity {signal.severity!r}; expected one of "
                f"{sorted(_VALID_SEVERITIES)}",
            )
        self.emitted.append(signal)


def _governance_for(_conn: psycopg.Connection) -> Any:
    """Return a capturing GovernanceWriter stub bound to the test session."""
    return _CapturingGovernance()


def _run_check(check_fn: Any, conn: psycopg.Connection) -> Any:
    """Invoke an @asset_check function with shimmed dependencies.

    Dagster decorates the function and exposes the underlying callable
    through ``check_fn.op.compute_fn.decorated_fn``. We bypass the
    Dagster execution machinery entirely (no resources, no logging
    context, no run records) and just call the original Python function
    with the two args it actually uses.
    """
    underlying = cast("Any", check_fn).op.compute_fn.decorated_fn
    return underlying(
        context=None,
        pg=_pg_resource_for(conn),
        governance=_governance_for(conn),
    )


def _unwrap_metadata(result: Any) -> dict[str, Any]:
    """Unwrap a Dagster ``AssetCheckResult.metadata`` dict to plain Python.

    AssetCheckResult auto-wraps each value in a typed MetadataValue
    (IntMetadataValue.value, TextMetadataValue.text, JsonMetadataValue.data,
    etc.). For test assertions we want the underlying scalar so we can
    use plain ``==`` / ``>=`` against ints and lists.
    """
    out: dict[str, Any] = {}
    for k, v in result.metadata.items():
        if hasattr(v, "value"):
            out[k] = v.value
        elif hasattr(v, "text"):
            out[k] = v.text
        elif hasattr(v, "data"):
            out[k] = v.data
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Scenario A: clean DB, every check must vacuous-pass.
# ---------------------------------------------------------------------------


def test_row_count_positive_passes_vacuously_when_no_candidates(
    fraud_db: psycopg.Connection,
) -> None:
    """No raw.fec_candidate rows -> the check passes with the documented reason.

    A fresh deployment hasn't loaded cn/cm yet; failing here would
    page the operator on a perfectly normal pre-load state.
    """
    from orchestration.asset_checks import (
        fraud_signal_observation_row_count_positive,
    )

    result = _run_check(fraud_signal_observation_row_count_positive, fraud_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["n_candidates"] == 0
    assert md["n_observations"] == 0
    assert md["reason"] == "vacuous_pass_no_candidates_loaded"


def test_signal_coverage_passes_vacuously_when_l1_empty(
    fraud_db: psycopg.Connection,
) -> None:
    """Empty L1 -> the coverage check passes silently.

    The row_count_positive check is the one that surfaces the
    "table is empty when it shouldn't be" condition; coverage
    deliberately avoids double-warning.
    """
    from orchestration.asset_checks import (
        fraud_signal_observation_signal_coverage,
    )

    result = _run_check(fraud_signal_observation_signal_coverage, fraud_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["reason"] == "vacuous_pass_empty_table"
    assert md["missing_signal_ids"] == []


def test_risk_score_check_passes_vacuously_when_l1_empty(
    fraud_db: psycopg.Connection,
) -> None:
    """Empty L1 -> the L3a check passes silently."""
    from orchestration.asset_checks import (
        fraud_risk_score_positive_when_l1_present,
    )

    result = _run_check(fraud_risk_score_positive_when_l1_present, fraud_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["reason"] == "vacuous_pass_l1_empty"
    assert md["n_l1_observations"] == 0


# ---------------------------------------------------------------------------
# Scenario B: fully populated DB (engineered anomalies + dispatcher run)
# Every check must pass cleanly with the expected positive-state metadata.
# ---------------------------------------------------------------------------


def test_row_count_positive_passes_when_l1_populated(
    fraud_db: psycopg.Connection,
) -> None:
    """Engineered anomalies -> L1 has rows -> the check passes."""
    from orchestration.asset_checks import (
        fraud_signal_observation_row_count_positive,
    )

    _seed_engineered_anomalies(fraud_db)
    n_total = _run_dispatcher(fraud_db)
    assert n_total >= 8

    result = _run_check(fraud_signal_observation_row_count_positive, fraud_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["n_candidates"] >= 1
    assert md["n_observations"] >= 8
    assert md["reason"] == "ok"


def test_signal_coverage_passes_when_dispatcher_emits_all_eight(
    fraud_db: psycopg.Connection,
) -> None:
    """Engineered anomalies trigger all 8 v2.A signals -> coverage passes."""
    from orchestration.asset_checks import (
        fraud_signal_observation_signal_coverage,
    )
    from orchestration.assets import FRAUD_STRUCTURAL_SIGNAL_IDS

    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    result = _run_check(fraud_signal_observation_signal_coverage, fraud_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["missing_signal_ids"] == []
    assert set(md["present_signal_ids"]) == set(FRAUD_STRUCTURAL_SIGNAL_IDS)
    per_signal = md["per_signal_row_count"]
    for sig in FRAUD_STRUCTURAL_SIGNAL_IDS:
        assert per_signal[sig] >= 1, f"signal {sig} has zero L1 rows"
    assert md["reason"] == "ok"


def test_risk_score_check_passes_when_l3a_has_positive_scores(
    fraud_db: psycopg.Connection,
) -> None:
    """Engineered anomalies produce non-zero risk_scores in L3a view."""
    from orchestration.asset_checks import (
        fraud_risk_score_positive_when_l1_present,
    )

    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    result = _run_check(fraud_risk_score_positive_when_l1_present, fraud_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["n_l1_observations"] >= 8
    assert md["n_l3a_entities"] >= 1
    assert md["n_l3a_score_positive"] >= 1
    assert md["reason"] == "ok"


# ---------------------------------------------------------------------------
# End-to-end: the asset compute and the asset checks share state.
# ---------------------------------------------------------------------------


def test_asset_compute_then_checks_smoke(
    fraud_db: psycopg.Connection,
) -> None:
    """Calling the dispatcher once leaves L1 + L3a in a state every check accepts.

    This is the smoke test for the integration that step 2.5 ships:
    the Dagster asset materializes by calling the SQL dispatcher; the
    three attached asset checks then run against that state. None of
    them should fail in the happy path.
    """
    from orchestration.asset_checks import (
        fraud_risk_score_positive_when_l1_present,
        fraud_signal_observation_row_count_positive,
        fraud_signal_observation_signal_coverage,
    )

    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    for chk in (
        fraud_signal_observation_row_count_positive,
        fraud_signal_observation_signal_coverage,
        fraud_risk_score_positive_when_l1_present,
    ):
        result = _run_check(chk, fraud_db)
        assert result.passed is True, (
            f"asset check {chk!r} unexpectedly failed in the happy path: "
            f"{_unwrap_metadata(result)}"
        )
