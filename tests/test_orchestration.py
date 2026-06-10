"""Unit tests for the Dagster orchestration layer.

We test the orchestration package without bringing up a Dagster
webserver/daemon. The tests cover:

* Asset / schedule / sensor registries are well-formed (every entry
  has the expected key shape, freshness policy, etc.).
* The Definitions object loads cleanly.
* Resource construction validates inputs.
* GovernanceWriter emits well-formed rows.

End-to-end materialization with a real Postgres lives in
test_pg_integration.py.
"""

from __future__ import annotations

import pytest

# Skip entire module if Dagster is not installed (orchestration is an
# optional extra; not all dev installs have it).
dagster = pytest.importorskip("dagster")


# ============================================================================
# Asset registry
# ============================================================================


def test_all_assets_have_expected_key_shape() -> None:
    """Every asset key starts with 'raw' or 'derived' (our schema convention)."""
    from orchestration.assets import ALL_ASSETS

    assert len(ALL_ASSETS) >= 15, (
        "expected >= 15 assets: 10 raw (incl PUMS) + 3 FEC + LEIE + "
        "USAspending + 4 FRAUD-F7 CMS/NJ healthcare raw + derived surface"
    )
    for asset_def in ALL_ASSETS:
        for key in asset_def.keys:
            assert key.path[0] in {"raw", "derived"}, (
                f"asset key {key} does not start with raw/derived"
            )
            assert len(key.path) == 2, (
                f"asset key {key} should be 2-deep (schema, table)"
            )


def test_raw_assets_declare_freshness_policy() -> None:
    """Every raw asset must declare a freshness policy (BBG hygiene).

    Derived assets do not need their own freshness policy because they
    inherit the bound from their upstream parent (Dagster's freshness
    propagation). Asserting on raw is sufficient.
    """
    from orchestration.assets import ALL_ASSETS

    for asset_def in ALL_ASSETS:
        for key in asset_def.keys:
            if key.path[0] != "raw":
                continue
            spec = asset_def.specs_by_key[key]
            assert spec.freshness_policy is not None, (
                f"raw asset {key} missing FreshnessPolicy; "
                "every BBG-grade source must declare staleness semantics."
            )


def test_derived_assets_declare_upstream_deps() -> None:
    """Every derived asset must declare at least one upstream dep (raw or derived).

    Multi-tier derived pipelines need derived-on-derived edges: e.g.,
    Tier 4 v3 has derived.v_entity_fraud_risk (an L3a SQL view) depending
    on derived.fraud_signal_observation (the L1 table populated by the
    SQL dispatcher). The intermediate L2 (v_entity_fraud_features) is a
    view-on-view, not an asset, so the L3a -> L1 edge is correct lineage.
    The contract: every derived asset has >=1 dep, and every dep is in
    {raw, derived}.

    EXCEPTION: config-registry assets. A small whitelist of derived
    assets are static configuration registries seeded by a migration
    (not derived from upstream data). They legitimately have zero
    upstream deps because their lineage is the migration itself, not
    a runtime data flow. Adding a fake dep would be misleading.
    """
    from orchestration.assets import ALL_ASSETS

    # Static config-registry assets seeded by migration. These have no
    # runtime upstream and the test should not flag them as missing deps.
    CONFIG_REGISTRY_ASSETS: set[tuple[str, ...]] = {
        ("derived", "fraud_signal_config"),
    }

    for asset_def in ALL_ASSETS:
        for key in asset_def.keys:
            if key.path[0] != "derived":
                continue
            spec = asset_def.specs_by_key[key]
            dep_keys = [d.asset_key for d in spec.deps]
            if tuple(key.path) in CONFIG_REGISTRY_ASSETS:
                assert len(dep_keys) == 0, (
                    f"config-registry asset {key} unexpectedly declared "
                    f"upstream deps {dep_keys}; either remove the deps "
                    "or remove it from CONFIG_REGISTRY_ASSETS."
                )
                continue
            assert len(dep_keys) >= 1, (
                f"derived asset {key} has no upstream deps; "
                "lineage edge must be explicit."
            )
            for dk in dep_keys:
                assert dk.path[0] in {"raw", "derived"}, (
                    f"derived asset {key} depends on {dk}, but only "
                    "raw.* or derived.* deps are expected at this layer."
                )


def test_derived_assets_declare_automation_condition() -> None:
    """Every derived asset must declare an AutomationCondition.

    This is the architectural commitment: derived.* refreshes are
    event-driven (parent materialization), not cron-polled. Asserting
    on the asset definition catches any future asset that is added
    without this contract.
    """
    from orchestration.assets import ALL_ASSETS

    for asset_def in ALL_ASSETS:
        for key in asset_def.keys:
            if key.path[0] != "derived":
                continue
            spec = asset_def.specs_by_key[key]
            assert spec.automation_condition is not None, (
                f"derived asset {key} missing AutomationCondition; "
                "derived assets must declare event-driven refresh."
            )


def test_no_cron_schedule_for_derived_assets() -> None:
    """Derived assets must NOT be on any cron schedule.

    Refreshing derived.* on a cron *and* via AutomationCondition is a
    silent footgun: it produces redundant work and obscures lineage in
    the Dagster UI. Asserting absence of any schedule that targets a
    derived group keeps the boundary clean.
    """
    from dagster import AssetSelection

    from orchestration.assets import ALL_ASSETS
    from orchestration.schedules import ALL_SCHEDULES

    derived_keys = {
        key
        for asset_def in ALL_ASSETS
        for key in asset_def.keys
        if key.path[0] == "derived"
    }
    for sched in ALL_SCHEDULES:
        target = sched.target
        if not isinstance(target, AssetSelection):
            continue
        resolved = target.resolve(ALL_ASSETS)
        intersect = derived_keys & set(resolved)
        assert not intersect, (
            f"schedule {sched.name!r} targets derived assets {intersect}; "
            "derived assets must use AutomationCondition.eager(), not cron."
        )


def test_specific_lineage_edges() -> None:
    """Spot-check the most important lineage edges."""
    from orchestration.assets import ALL_ASSETS

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    burden = by_key["derived/housing_burden_ratio"]
    burden_spec = burden.specs_by_key[next(iter(burden.keys))]
    burden_deps = {
        "/".join(d.asset_key.path) for d in burden_spec.deps
    }
    # Migration 032 extended the view with NJ DCA property-tax
    # context columns; the dep edge is now real (the SQL view JOINs
    # raw.nj_property_tax_county and the asset's fingerprint includes
    # property_tax_share_of_income).
    assert burden_deps == {
        "raw/acs_housing",
        "raw/acs_median_household_income",
        "raw/nj_property_tax_county",
    }

    real_inc = by_key["derived/f_acs_mhi_real"]
    real_inc_spec = real_inc.specs_by_key[next(iter(real_inc.keys))]
    real_inc_deps = {
        "/".join(d.asset_key.path) for d in real_inc_spec.deps
    }
    assert real_inc_deps == {
        "raw/acs_median_household_income",
        "raw/cpi_u",
    }


# ============================================================================
# Schedule registry
# ============================================================================


def test_all_schedules_have_unique_names() -> None:
    """Schedule names must be unique (Dagster requires it)."""
    from orchestration.schedules import ALL_SCHEDULES

    names = [s.name for s in ALL_SCHEDULES]
    assert len(names) == len(set(names)), f"duplicate schedule names: {names}"


def test_all_schedules_use_eastern_timezone() -> None:
    """All schedule cadences are anchored to ET (matches release calendar)."""
    from orchestration.schedules import ALL_SCHEDULES

    for s in ALL_SCHEDULES:
        assert s.execution_timezone == "America/New_York", (
            f"schedule {s.name} uses {s.execution_timezone!r} but the "
            "release_calendar table assumes America/New_York."
        )


def test_all_schedules_have_valid_cron_strings() -> None:
    """Cron strings must be 5-field standard cron syntax."""
    from orchestration.schedules import ALL_SCHEDULES

    for s in ALL_SCHEDULES:
        cron = s.cron_schedule
        assert cron is not None, f"schedule {s.name} has no cron"
        fields = str(cron).split()
        assert len(fields) == 5, (
            f"schedule {s.name} cron {cron!r} is not 5-field"
        )


# ============================================================================
# Sensor registry
# ============================================================================


def test_freshness_violation_sensor_registered() -> None:
    from orchestration.sensors import ALL_SENSORS

    names = {s.name for s in ALL_SENSORS}
    assert "freshness_violation_sensor" in names


# ============================================================================
# Asset checks
# ============================================================================


def test_every_raw_asset_has_at_least_one_check() -> None:
    """Each raw asset should have >=1 quality gate."""
    from orchestration.asset_checks import ALL_ASSET_CHECKS
    from orchestration.assets import ALL_ASSETS

    raw_keys = {
        next(iter(a.keys)).to_user_string()
        for a in ALL_ASSETS
        if next(iter(a.keys)).path[0] == "raw"
    }

    checked_keys: set[str] = set()
    for chk in ALL_ASSET_CHECKS:
        for spec in chk.check_specs:
            checked_keys.add(spec.asset_key.to_user_string())

    missing = raw_keys - checked_keys
    assert not missing, (
        f"raw assets without any AssetCheck: {sorted(missing)}; "
        "every raw asset must have at least one quality gate."
    )


# ============================================================================
# Tier 4 v3: fraud-risk surface
# ============================================================================
#
# These tests pin the asset-graph contract for the Dagster wiring of the
# Tier 4 v3 fraud-risk pipeline (migrations 050-052 + the L1 dispatcher).
# They verify shape and lineage only; live-pg materialization is exercised
# by tests/test_pg_integration.py.


def test_fraud_structural_signal_ids_match_dispatcher() -> None:
    """The Python signal-id constant must match the SQL dispatcher (migration 051).

    If a future migration adds, removes, or renames a per-signal refresher
    in derived.refresh_all_fraud_signal_observations, the asset check that
    consumes FRAUD_STRUCTURAL_SIGNAL_IDS must fail loudly so we don't
    silently green-light a missing-signal regression. We hardcode the
    expected set here (rather than parse the SQL) because the test should
    fail BOTH when the SQL drifts AND when the Python constant drifts.
    """
    from orchestration.assets import FRAUD_STRUCTURAL_SIGNAL_IDS

    expected = {
        "candidate_broken_pcc",
        "candidate_multiple_pccs",
        "candidate_namesakes",
        "candidate_no_pcc",
        "committee_address_clusters",
        "committee_name_collisions",
        "treasurer_concentration",
        "treasurer_is_candidate",
    }
    assert set(FRAUD_STRUCTURAL_SIGNAL_IDS) == expected, (
        f"FRAUD_STRUCTURAL_SIGNAL_IDS = {set(FRAUD_STRUCTURAL_SIGNAL_IDS)} "
        f"does not match the eight v2.A signals dispatched by "
        f"derived.refresh_all_fraud_signal_observations (migration 051). "
        f"Either the SQL dispatcher gained/lost a signal and the Python "
        f"constant lagged, or the Python constant drifted. Reconcile both."
    )
    # Sanity: tuple is sorted for deterministic asset-check metadata.
    assert list(FRAUD_STRUCTURAL_SIGNAL_IDS) == sorted(FRAUD_STRUCTURAL_SIGNAL_IDS), (
        "FRAUD_STRUCTURAL_SIGNAL_IDS must be sorted; the asset check "
        "exposes it as metadata and stable ordering keeps run-over-run "
        "diffs trivially comparable."
    )


def test_fraud_signal_observation_asset_present_with_correct_deps() -> None:
    """derived.fraud_signal_observation must depend on raw.fec_candidate + raw.fec_committee.

    Substrate-honesty contract: the L1 adapter dispatcher reads from the
    derived.fec_* views, but those views read from raw.fec_candidate and
    raw.fec_committee (cn / cm). We declare the dep edges to the raw
    assets directly so AutomationCondition.eager() fires when EITHER
    cn or cm refreshes. raw.fec_contribution is intentionally NOT a dep:
    the v2.A structural signals are cn/cm-only.
    """
    from orchestration.assets import ALL_ASSETS

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    assert "derived/fraud_signal_observation" in by_key
    asset_def = by_key["derived/fraud_signal_observation"]
    spec = asset_def.specs_by_key[next(iter(asset_def.keys))]
    dep_keys = {"/".join(d.asset_key.path) for d in spec.deps}
    assert dep_keys == {"raw/fec_candidate", "raw/fec_committee"}, (
        f"derived.fraud_signal_observation deps = {dep_keys}; must depend "
        "on BOTH raw.fec_candidate AND raw.fec_committee so cn/cm refresh "
        "fans out. raw.fec_contribution is intentionally excluded (the "
        "v2.A signals are cn/cm-only)."
    )
    assert spec.automation_condition is not None, (
        "derived.fraud_signal_observation must declare AutomationCondition "
        "so cn/cm refresh propagates without operator intervention."
    )


def test_v_entity_fraud_risk_asset_present_with_correct_deps() -> None:
    """derived.v_entity_fraud_risk must depend on every asset that writes L1.

    Substrate-honesty contract: the L3a view reads from L2
    (v_entity_fraud_features), which reads from L1 (fraud_signal_observation).
    Multiple refresher assets write to L1 on disjoint signal_id slices:
    derived.fraud_signal_observation (the structural-FEC dispatcher),
    derived.signal_entity_on_leie (FRAUD-F5b LEIE cross-source signal),
    derived.signal_donor_employed_by_nj_contractor (FRAUD-F1
    USAspending cross-source signal),
    derived.signal_candidate_funded_by_nj_contractor_employees (the
    candidate-side projection of the F1 signal),
    derived.signal_entity_funded_and_excluded (FRAUD-F1+F5
    intersection: federally-excluded individual receiving federal
    contracts),
    derived.signal_donor_on_leie (FRAUD-F5c: federally-excluded
    individual donating to NJ campaigns), and
    derived.signal_candidate_funded_by_excluded_donors
    (FRAUD-F5d: candidate-side projection of donor_on_leie). All
    seven L1 writers must be declared upstream of L3a so any
    refresh propagates the fingerprint change downstream eagerly.

    Migration 061 added an eighth dep, derived.fraud_signal_config:
    a static config registry that the L2 view INNER-JOINs for
    per-signal thresholds and signal_family tags. An operator
    UPDATE on that table must propagate to L3a fingerprint, hence
    the dep edge.
    """
    from orchestration.assets import ALL_ASSETS

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    assert "derived/v_entity_fraud_risk" in by_key
    asset_def = by_key["derived/v_entity_fraud_risk"]
    spec = asset_def.specs_by_key[next(iter(asset_def.keys))]
    dep_keys = {"/".join(d.asset_key.path) for d in spec.deps}
    expected_deps = {
        "derived/fraud_signal_observation",
        "derived/signal_entity_on_leie",
        "derived/signal_donor_employed_by_nj_contractor",
        "derived/signal_candidate_funded_by_nj_contractor_employees",
        "derived/signal_entity_funded_and_excluded",
        "derived/signal_donor_on_leie",
        "derived/signal_candidate_funded_by_excluded_donors",
        # Migration 064: UEI-deterministic SAM x USAspending match
        # (FRAUD-F2). family='sam_bearing'; new family enables the
        # diversity bonus to reward LEIE+SAM corroboration.
        "derived/signal_entity_excluded_via_sam_uei",
        # Migration 065: SAM-individual donor cross-source signal
        # (FRAUD-F2 donor-side). Parallel to donor_on_leie.
        "derived/signal_donor_on_sam",
        # Migration 066: candidate-side projection of donor_on_sam
        # (FRAUD-F2 candidate-side). Parallel to
        # candidate_funded_by_excluded_donors.
        "derived/signal_candidate_funded_by_sam_excluded_donors",
        # Detection-quality registry (migration 061): static config
        # the L2 view joins for per-signal thresholds + family tags.
        "derived/fraud_signal_config",
    }
    assert dep_keys == expected_deps, (
        f"derived.v_entity_fraud_risk deps = {dep_keys}; must declare every "
        f"asset that writes the L1 table: {expected_deps}. The L2 "
        "intermediate is a view-on-view (not an asset), so dep edges "
        "skip straight from each L1 writer to L3a."
    )
    assert spec.automation_condition is not None, (
        "derived.v_entity_fraud_risk must declare AutomationCondition so "
        "L1 refresh propagates fingerprint changes."
    )


def test_fraud_signal_observation_has_all_three_asset_checks() -> None:
    """L1 + L3a-view must each have their architectural quality gates.

    fraud_signal_observation gets:
      - row_count_positive (vacuous-pass when cn empty)
      - signal_coverage    (all 8 signal_ids present when L1 non-empty)

    v_entity_fraud_risk gets:
      - risk_score_positive_when_l1_present

    These are the three failure modes the schema CHECKs cannot catch:
    missing data (1+2) and silent downstream regressions (3).
    """
    from orchestration.asset_checks import ALL_ASSET_CHECKS

    expected = {
        ("derived/fraud_signal_observation", "row_count_positive"),
        ("derived/fraud_signal_observation", "signal_coverage"),
        ("derived/v_entity_fraud_risk",      "risk_score_positive_when_l1_present"),
    }
    actual: set[tuple[str, str]] = set()
    for chk in ALL_ASSET_CHECKS:
        for spec in chk.check_specs:
            actual.add((spec.asset_key.to_user_string(), spec.name))
    missing = expected - actual
    assert not missing, (
        f"Tier 4 v3 asset checks missing from ALL_ASSET_CHECKS: "
        f"{sorted(missing)}. Every L1/L3a quality gate must register in "
        "ALL_ASSET_CHECKS or the Dagster daemon will not run it."
    )


def test_pums_assets_present_and_paired() -> None:
    """raw.acs_pums_person + raw.acs_pums_housing both exist; housing depends on person."""
    from orchestration.assets import ALL_ASSETS

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    assert "raw/acs_pums_person"  in by_key
    assert "raw/acs_pums_housing" in by_key

    # Housing reads after the person COPY; declare a dep edge to make
    # the asset graph honest.
    housing = by_key["raw/acs_pums_housing"]
    housing_spec = housing.specs_by_key[next(iter(housing.keys))]
    housing_dep_keys = {
        "/".join(d.asset_key.path) for d in housing_spec.deps
    }
    assert housing_dep_keys == {"raw/acs_pums_person"}, (
        "raw.acs_pums_housing must depend on raw.acs_pums_person; the "
        "single fetch loads both tables in one transaction."
    )


def test_pums_burden_segmented_depends_on_both_pums_raw() -> None:
    """derived.pums_burden_segmented must depend on BOTH raw.acs_pums_*.

    This is a substrate honesty contract: the compute reads from both
    tables, so both must appear as deps. A regression where one is
    missing would mean a refresh of (e.g.) raw.acs_pums_housing alone
    would NOT trigger an automatic recompute via AutomationCondition.eager().
    """
    from orchestration.assets import ALL_ASSETS

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    assert "derived/pums_burden_segmented" in by_key
    asset_def = by_key["derived/pums_burden_segmented"]
    spec = asset_def.specs_by_key[next(iter(asset_def.keys))]
    dep_keys = {"/".join(d.asset_key.path) for d in spec.deps}
    assert dep_keys == {"raw/acs_pums_person", "raw/acs_pums_housing"}, (
        f"derived.pums_burden_segmented deps = {dep_keys}; must include "
        "BOTH PUMS raw assets so AutomationCondition.eager() fires on "
        "either upstream refresh."
    )


def test_pums_burden_county_segmented_depends_on_both_pums_raw() -> None:
    """derived.pums_burden_county_segmented must depend on BOTH raw.acs_pums_*.

    Same substrate-honesty contract as the PUMA-grain version. The
    crosswalk (ref.puma2020_county_xwalk) is reference data, not an
    asset, so it does NOT appear in deps; that is intentional.
    """
    from orchestration.assets import ALL_ASSETS

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    assert "derived/pums_burden_county_segmented" in by_key
    asset_def = by_key["derived/pums_burden_county_segmented"]
    spec = asset_def.specs_by_key[next(iter(asset_def.keys))]
    dep_keys = {"/".join(d.asset_key.path) for d in spec.deps}
    assert dep_keys == {"raw/acs_pums_person", "raw/acs_pums_housing"}, (
        f"derived.pums_burden_county_segmented deps = {dep_keys}; must "
        "depend on BOTH raw PUMS assets. The crosswalk is ref data, not "
        "an asset, so it intentionally does not appear here."
    )


def test_fraud_f7_healthcare_raw_assets_registered() -> None:
    """The five FRAUD-F7 CMS / NJ / NPPES healthcare raw assets must be wired in.

    Pins the asset-graph contract for the FRAUD-F7 substrate slice. Each
    asset must:

    * be present in ALL_ASSETS with a 2-deep ``raw.<table>`` key,
    * declare a FreshnessPolicy (raw-asset hygiene -- enforced globally by
      test_raw_assets_declare_freshness_policy, asserted here too so a
      regression names the offending FRAUD-F7 key directly),
    * declare ZERO upstream deps (these are leaf raw sources, exactly like
      raw.hhs_oig_leie which they mirror), and
    * have >= 1 AssetCheck registered in ALL_ASSET_CHECKS (the
      every-raw-asset-has-a-gate contract, asserted here for the new keys).
    """
    from orchestration.asset_checks import ALL_ASSET_CHECKS
    from orchestration.assets import ALL_ASSETS

    expected_keys = {
        "raw/cms_partd_prescriber",
        "raw/cms_physician_provider",
        "raw/cms_open_payments_general",
        "raw/nj_medicaid_exclusion",
        "raw/nppes_provider",
    }

    by_key = {
        next(iter(a.keys)).to_user_string(): a
        for a in ALL_ASSETS
    }
    missing = expected_keys - set(by_key)
    assert not missing, (
        f"FRAUD-F7 healthcare raw assets missing from ALL_ASSETS: "
        f"{sorted(missing)}. Each CMS / NJ substrate ingester must have a "
        "registered raw-ingestion asset (mirrors raw.hhs_oig_leie)."
    )

    checked_keys: set[str] = set()
    for chk in ALL_ASSET_CHECKS:
        for spec in chk.check_specs:
            checked_keys.add(spec.asset_key.to_user_string())

    for key_str in expected_keys:
        asset_def = by_key[key_str]
        key = next(iter(asset_def.keys))
        assert key.path[0] == "raw" and len(key.path) == 2, (
            f"{key_str} must be a 2-deep raw.<table> key"
        )
        spec = asset_def.specs_by_key[key]
        assert spec.freshness_policy is not None, (
            f"{key_str} must declare a FreshnessPolicy"
        )
        assert len(spec.deps) == 0, (
            f"{key_str} must be a leaf raw source with no upstream deps "
            f"(mirrors raw.hhs_oig_leie); got {[d.asset_key for d in spec.deps]}"
        )
        assert key_str in checked_keys, (
            f"{key_str} has no AssetCheck in ALL_ASSET_CHECKS; every raw "
            "asset must have at least one quality gate."
        )


def test_dol_fiscal_quarter_helper() -> None:
    """The DOL fiscal-quarter helper must agree with the official calendar.

    Spot-check several dates: FY follows the convention that
    FY2024 = Oct 2023 - Sep 2024, with quarters Q1=Oct-Dec, Q2=Jan-Mar,
    Q3=Apr-Jun, Q4=Jul-Sep.
    """
    import datetime as dt

    from orchestration.assets import _current_dol_fiscal_quarter

    # Oct 1 of calendar year: start of FY+1 Q1
    assert _current_dol_fiscal_quarter(dt.date(2025, 10, 1)) == (2026, 1)
    # Dec 31 calendar 2025: still FY26 Q1
    assert _current_dol_fiscal_quarter(dt.date(2025, 12, 31)) == (2026, 1)
    # Jan 1 calendar 2026: FY26 Q2
    assert _current_dol_fiscal_quarter(dt.date(2026, 1, 1)) == (2026, 2)
    # Apr 28 calendar 2026: FY26 Q3
    assert _current_dol_fiscal_quarter(dt.date(2026, 4, 28)) == (2026, 3)
    # Sep 30 calendar 2026: FY26 Q4
    assert _current_dol_fiscal_quarter(dt.date(2026, 9, 30)) == (2026, 4)


def test_previous_fiscal_quarter_helper() -> None:
    """Walking backwards must wrap Q1 -> previous-year Q4."""
    from orchestration.assets import _previous_fiscal_quarter

    assert _previous_fiscal_quarter(2026, 4) == (2026, 3)
    assert _previous_fiscal_quarter(2026, 1) == (2025, 4)
    assert _previous_fiscal_quarter(2026, 2) == (2026, 1)


# ============================================================================
# Resources
# ============================================================================


def test_pg_resource_from_env_requires_pg_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env raises if PG_DSN is unset (do not silently use defaults)."""
    from orchestration.resources import PgResource

    monkeypatch.delenv("PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="PG_DSN"):
        PgResource.from_env()


def test_governance_signal_rejects_invalid_severity() -> None:
    """GovernanceWriter.emit refuses unknown severity values."""
    from orchestration.resources import (
        GovernanceWriter,
        HealthSignal,
        PgResource,
    )

    pg = PgResource(dsn="postgresql://nonexistent/foo")
    writer = GovernanceWriter(pg=pg)
    bad_signal = HealthSignal(
        dataset_id="raw.x",
        signal_name="test",
        severity="WARNING",  # type: ignore[arg-type]  -- invalid severity
        details={},
    )
    with pytest.raises(ValueError, match="Invalid severity"):
        writer.emit(bad_signal)


# ============================================================================
# Definitions object
# ============================================================================


def test_definitions_loads_with_all_components() -> None:
    """The top-level Definitions assembles assets + checks + schedules + sensors."""
    from orchestration.asset_checks import ALL_ASSET_CHECKS
    from orchestration.assets import ALL_ASSETS
    from orchestration.definitions import defs
    from orchestration.schedules import ALL_SCHEDULES
    from orchestration.sensors import ALL_SENSORS

    asset_keys = defs.resolve_asset_graph().get_all_asset_keys()
    expected_n = sum(len(list(a.keys)) for a in ALL_ASSETS)
    assert len(asset_keys) == expected_n
    assert len(defs.asset_checks) == len(ALL_ASSET_CHECKS)
    assert len(defs.schedules) == len(ALL_SCHEDULES)
    assert len(defs.sensors) == len(ALL_SENSORS)
