"""Live-Postgres tests for the VISION_2026 §7.1 tax-substrate forcing function.

The check under test
--------------------

``tax_substrate_prior_year_seeded`` (attached to ``raw.nj_property_tax_county``
for asset-graph co-location; the check is platform-wide).

Contract:

  * Computes ``target_tax_year = (current calendar year) - 1``.
  * Reads two seed-loaded ref tables:
      ref.irs_federal_brackets WHERE tax_year = target_tax_year
      ref.nj_state_brackets    WHERE tax_year = target_tax_year
  * Before April 1 of the current year   -> vacuous_pass_before_april_1_grace_period
    (regardless of whether the seeds exist)
  * On or after April 1 of the current year -> require BOTH seeds to be present;
    otherwise FAIL (severity = WARN).

The "current year" and "April 1 deadline" are read from CURRENT_DATE inside
the SQL, so the tests have to be careful: we cannot pin a specific calendar
date without freezing the clock. Instead we test the structural invariants:

  1. With FULL substrate seeded (IRS + NJ for every year through current_year-1),
     the check ALWAYS passes -- in grace period AND after the deadline.
  2. With ZERO substrate seeded, the check passes IFF before April 1, otherwise
     fails.
  3. With ONLY IRS seeded (NJ missing), same conditional behavior.
  4. The metadata exposes the structural booleans (irs_seeded, nj_seeded,
     deadline_passed_april_1, target_tax_year) so a UI / operator can read
     the reason without parsing the SQL.

Because seeds 010..039 already populate IRS + NJ for tax years 2010..2024 in
the migration directory, the FULL-substrate path is the default after
``apply_migrations(seeds)``. The ZERO and PARTIAL paths require explicit
DELETE-from-ref before invoking the check.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    import psycopg


pytestmark = pytest.mark.live_pg


@pytest.fixture
def tax_substrate_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply migrations + seeds to a clean DB; yield the conn."""
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


def _run_check(check_fn: Any, conn: psycopg.Connection) -> Any:
    underlying = cast("Any", check_fn).op.compute_fn.decorated_fn
    return underlying(
        context=None,
        pg=_pg_resource_for(conn),
        governance=_CapturingGovernance(),
    )


def _unwrap_metadata(result: Any) -> dict[str, Any]:
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
# Tests
# ---------------------------------------------------------------------------


class TestTaxSubstrateForcingFunction:
    def test_full_substrate_passes_regardless_of_deadline(
        self, tax_substrate_db: psycopg.Connection
    ) -> None:
        """With IRS + NJ substrate seeded for target_tax_year, the check
        passes whether or not we are past April 1.

        Note: the repo seeds (010..039) cover 2010-2024. As (current_year - 1)
        rolls forward each January, an unseeded year naturally appears -- that
        is the forcing function working correctly in production. To make this
        test calendar-independent we explicitly insert a synthetic row for
        target_tax_year before invoking the check.
        """
        from orchestration.asset_checks import tax_substrate_prior_year_seeded

        with tax_substrate_db.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(YEAR FROM CURRENT_DATE)::INT - 1"
            )
            row = cur.fetchone()
            assert row is not None
            target_year = int(row[0])
            # Seed a sentinel IRS row (single, MFJ unaffected) and an NJ
            # bracket row for target_tax_year. Both target tables only
            # require >= 1 row apiece for the check to pass; we use the
            # zero-floor base bracket to avoid any other constraint
            # surprises.
            cur.execute(
                "INSERT INTO ref.irs_federal_brackets "
                "  (tax_year, filing_status, bracket_ord, bracket_floor, "
                "   marginal_rate, source_url, source_citation) "
                "VALUES (%s, 'single', 1, 0, 0.10, "
                "        'http://test/irs', 'synthetic for forcing-function test') "
                "ON CONFLICT DO NOTHING",
                (target_year,),
            )
            cur.execute(
                "INSERT INTO ref.nj_state_brackets "
                "  (tax_year, filing_status, bracket_ord, bracket_floor, "
                "   marginal_rate, source_url, source_citation) "
                "VALUES (%s, 'single', 1, 0, 0.014, "
                "        'http://test/nj', 'synthetic for forcing-function test') "
                "ON CONFLICT DO NOTHING",
                (target_year,),
            )
        tax_substrate_db.commit()

        result = _run_check(tax_substrate_prior_year_seeded, tax_substrate_db)
        md = _unwrap_metadata(result)
        assert result.passed is True, (
            f"Full-substrate run should pass; got metadata={md}"
        )
        assert md["irs_seeded"] is True
        assert md["nj_seeded"] is True
        # Either branch ("ok" or grace-period) is acceptable; the substrate
        # is sufficient regardless. Pin the metadata shape.
        assert md["reason"] in ("ok", "vacuous_pass_before_april_1_grace_period")
        assert md["irs_federal_rows"] > 0
        assert md["nj_state_rows"] > 0
        assert isinstance(md["target_tax_year"], int)
        assert md["target_tax_year"] == target_year

    def test_metadata_emits_structural_booleans(
        self, tax_substrate_db: psycopg.Connection
    ) -> None:
        """Operators read the metadata to understand WHY the check passed
        or failed; pin the schema so UI consumers don't break silently."""
        from orchestration.asset_checks import tax_substrate_prior_year_seeded

        result = _run_check(tax_substrate_prior_year_seeded, tax_substrate_db)
        md = _unwrap_metadata(result)
        for k in (
            "target_tax_year",
            "deadline_passed_april_1",
            "irs_federal_rows",
            "nj_state_rows",
            "irs_seeded",
            "nj_seeded",
            "reason",
        ):
            assert k in md, f"missing metadata key {k!r}; got {sorted(md)}"

    def test_missing_substrate_grace_period_or_alarm(
        self, tax_substrate_db: psycopg.Connection
    ) -> None:
        """Wipe the prior-year seeds; behavior depends on the date.

        Before April 1: the absence is grace-period -> pass.
        On/after April 1: the absence is an alarm    -> fail with reason
                           = 'deadline_passed_with_missing_seeds'.
        We discover which branch we're in by reading the same CURRENT_DATE
        the check uses, so the test is calendar-correct on any run date.
        """
        from orchestration.asset_checks import tax_substrate_prior_year_seeded

        # Compute target_tax_year + deadline state via the same path the check uses.
        with tax_substrate_db.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(YEAR FROM CURRENT_DATE)::INT - 1, "
                "       (CURRENT_DATE >= make_date(EXTRACT(YEAR FROM CURRENT_DATE)::INT, 4, 1))"
            )
            row = cur.fetchone()
            assert row is not None
            target_year, deadline_passed = int(row[0]), bool(row[1])
            cur.execute(
                "DELETE FROM ref.irs_federal_brackets WHERE tax_year = %s",
                (target_year,),
            )
            cur.execute(
                "DELETE FROM ref.nj_state_brackets    WHERE tax_year = %s",
                (target_year,),
            )
        tax_substrate_db.commit()

        result = _run_check(tax_substrate_prior_year_seeded, tax_substrate_db)
        md = _unwrap_metadata(result)
        assert md["irs_seeded"] is False
        assert md["nj_seeded"] is False
        if deadline_passed:
            assert result.passed is False
            assert md["reason"] == "deadline_passed_with_missing_seeds"
        else:
            assert result.passed is True
            assert md["reason"] == "vacuous_pass_before_april_1_grace_period"

    def test_irs_seeded_but_nj_missing_still_alarms_after_deadline(
        self, tax_substrate_db: psycopg.Connection
    ) -> None:
        """Both halves are required; an asymmetric backfill must alarm
        post-deadline because /personalize cannot answer NJ tax questions
        without the matching NJ-1040 seed."""
        from orchestration.asset_checks import tax_substrate_prior_year_seeded

        with tax_substrate_db.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(YEAR FROM CURRENT_DATE)::INT - 1, "
                "       (CURRENT_DATE >= make_date(EXTRACT(YEAR FROM CURRENT_DATE)::INT, 4, 1))"
            )
            row = cur.fetchone()
            assert row is not None
            target_year, deadline_passed = int(row[0]), bool(row[1])
            # Seed IRS for target_year (in case the year isn't yet covered
            # by the repo seeds), then explicitly remove NJ for target_year
            # to simulate the asymmetric-backfill scenario.
            cur.execute(
                "INSERT INTO ref.irs_federal_brackets "
                "  (tax_year, filing_status, bracket_ord, bracket_floor, "
                "   marginal_rate, source_url, source_citation) "
                "VALUES (%s, 'single', 1, 0, 0.10, "
                "        'http://test/irs', 'synthetic for asymmetric test') "
                "ON CONFLICT DO NOTHING",
                (target_year,),
            )
            cur.execute(
                "DELETE FROM ref.nj_state_brackets WHERE tax_year = %s",
                (target_year,),
            )
        tax_substrate_db.commit()

        result = _run_check(tax_substrate_prior_year_seeded, tax_substrate_db)
        md = _unwrap_metadata(result)
        assert md["irs_seeded"] is True
        assert md["nj_seeded"] is False
        if deadline_passed:
            assert result.passed is False
            assert md["reason"] == "deadline_passed_with_missing_seeds"
        else:
            assert result.passed is True

    def test_severity_is_warn_not_error(
        self, tax_substrate_db: psycopg.Connection
    ) -> None:
        """The check is a forcing function, not a substrate-blocking error.

        Severity = WARN means the asset-graph stays green even when the
        seed is missing; the engine returns NULL for unseeded years
        (correct substrate-honesty behavior) and the platform stays online.
        Severity = ERROR would be wrong here because it would propagate to
        every downstream affordability asset.
        """
        from dagster import AssetCheckSeverity

        from orchestration.asset_checks import tax_substrate_prior_year_seeded

        result = _run_check(tax_substrate_prior_year_seeded, tax_substrate_db)
        assert result.severity == AssetCheckSeverity.WARN

    def test_governance_signal_is_emitted(
        self, tax_substrate_db: psycopg.Connection
    ) -> None:
        """Independent of pass/fail, the check must emit a HealthSignal so
        non-Dagster consumers (the freshness substrate, dashboards) see the
        same outcome."""
        from orchestration.asset_checks import tax_substrate_prior_year_seeded

        underlying = cast(
            "Any", tax_substrate_prior_year_seeded
        ).op.compute_fn.decorated_fn
        gov = _CapturingGovernance()
        underlying(
            context=None,
            pg=_pg_resource_for(tax_substrate_db),
            governance=gov,
        )
        assert len(gov.emitted) == 1
        sig = gov.emitted[0]
        assert sig.dataset_id == "raw.nj_property_tax_county"
        assert sig.signal_name == "check.tax_substrate_prior_year_seeded"
        assert sig.severity in ("info", "warn")
