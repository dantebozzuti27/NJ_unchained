-- ============================================================================
-- Migration: 123_lca_nj_h1b_view_attestation
--
-- Postgres expands SELECT * at CREATE VIEW time. v_lca_nj_h1b was created
-- in mig 121 before raw.lca_disclosure gained the ETA-9035 attestation
-- columns, so those columns were invisible to the FRAUD-V1b refreshers.
-- CREATE OR REPLACE can only APPEND columns, so the new fields are listed
-- after is_nj_employer rather than via l.*.
-- ============================================================================

BEGIN;

CREATE OR REPLACE VIEW derived.v_lca_nj_h1b AS
SELECT
    l.fiscal_year,
    l.fiscal_quarter,
    l.case_number,
    l.worksite_idx,
    l.case_status,
    l.visa_class,
    l.received_date,
    l.decision_date,
    l.employment_start_date,
    l.employment_end_date,
    l.employer_name,
    l.employer_canonical_name,
    l.employer_naics,
    l.employer_state,
    l.employer_country,
    l.worksite_city,
    l.worksite_state,
    l.worksite_postal_code,
    l.total_workers,
    l.wage_rate_of_pay_from,
    l.wage_rate_of_pay_to,
    l.wage_unit_of_pay,
    l.annualized_wage_from,
    l.annualized_wage_to,
    l.prevailing_wage,
    l.pw_unit_of_pay,
    l.annualized_pw,
    l.pw_source,
    l.soc_code,
    l.job_title,
    l.source_filename,
    l.source_sha256,
    l.source_schema_version,
    l.data_quality,
    l.ingested_at,
    (UPPER(TRIM(l.worksite_state)) = 'NJ')   AS is_nj_worksite,
    (UPPER(TRIM(l.employer_state)) = 'NJ')   AS is_nj_employer,
    l.employer_fein,
    l.h1b_dependent,
    l.willful_violator,
    l.secondary_entity,
    l.secondary_entity_business_name,
    l.pw_wage_level
FROM raw.lca_disclosure l
WHERE l.visa_class = 'H-1B'
  AND (
      UPPER(TRIM(l.worksite_state)) = 'NJ'
      OR UPPER(TRIM(l.employer_state)) = 'NJ'
  );

COMMENT ON VIEW derived.v_lca_nj_h1b IS
    'NJ-scoped H-1B LCA rows (worksite or employer state = NJ). Recreated '
    'in mig 123 so attestation columns from mig 122 are visible to '
    'FRAUD-V1b refreshers. formula 3.8.0-fraud-h1b-attestation-enforcement-v1.';

COMMIT;
