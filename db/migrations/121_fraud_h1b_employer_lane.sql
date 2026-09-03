-- ============================================================================
-- Migration: 121_fraud_h1b_employer_lane
--
-- FRAUD-V1 / POP-3: H-1B visa-fraud evidence lane (New Jersey employers).
--
-- RATIONALE is in work_left.txt (session 2026-09-02). Healthcare and FEC
-- lanes already exist; this adds employer-keyed H-1B leads from two public
-- sources:
--   * DOL OFLC LCA disclosures (raw.lca_disclosure, already shipped POP-2)
--   * USCIS H-1B Employer Data Hub (new raw.uscis_h1b_employer, POP-3)
--
-- SIGNALS (four)
-- --------------
-- 1. employer_below_prevailing_wage (family h1b_wage, severity 5)
--    CERTIFIED H-1B LCA whose offered annualized wage is below the
--    annualized prevailing wage by at least h1b_below_pw_min_gap_usd.
--    Statutory predicate: INA §212(n); 20 CFR 655.731.
--    raw_value = SUM(annualized_pw - annualized_wage_from) for the employer.
--
-- 2. employer_h1b_denial_rate_outlier (family h1b_adjudication, severity 4)
--    USCIS petitioner whose denial_rate = denials/(approvals+denials) sits
--    in the top tail of NJ employers with >= h1b_denial_min_petitions
--    decisions. Empirical percentile, not a statutory violation.
--    raw_value = denial_rate.
--
-- 3. employer_lca_uscis_volume_gap (family h1b_cross_source, severity 3)
--    Employer present in BOTH sources whose certified LCA worker count
--    divided by USCIS approvals is in the top tail of NJ matched employers.
--    LCA overstates approvals by construction (pre-adjudication); the
--    signal only flags the extreme tail. raw_value = lca_workers / approvals.
--
-- 4. employer_certified_withdrawn_rate_outlier (family h1b_wage, severity 3)
--    Employer whose CERTIFIED-WITHDRAWN share of decided LCAs is in the
--    top tail (benching / file-then-abandon lead). raw_value = cw_rate.
--
-- HONEST FRAMING
-- --------------
-- These are leads. A below-PW row can be an annualization artifact; a high
-- denial rate can be a new petitioner or a specialty with a tough lottery.
-- The UI must not say "visa fraud."
--
-- NO MAGIC NUMBERS: all cutoffs live in ref.platform_constants.
-- entity_kind='employer'; cycle = fiscal year CHAR(4).
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.7.0-fraud-h1b-employer-lane-v1',
    'Pillar 2 (civic integrity) FRAUD-V1 / POP-3. Adds entity_kind=employer '
    'and four H-1B visa-fraud LEADS for New Jersey: below-prevailing-wage '
    '(statutory INA 212(n) / 20 CFR 655.731), USCIS denial-rate tail, '
    'LCA-vs-USCIS volume gap, and certified-withdrawn rate tail. Ships '
    'raw.uscis_h1b_employer (USCIS H-1B Employer Data Hub). Scores remain '
    'peer-percentile composites, not P(fraud).',
    '2026-09-02',
    'Stacks on 3.6.0-fraud-provider-billing-growth-outlier-v1. Master '
    'refresher 28 -> 32 (TIER 11).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. entity_kind CHECK widening: + 'employer'
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_observation
    DROP CONSTRAINT IF EXISTS fraud_signal_observation_entity_kind_check;

ALTER TABLE derived.fraud_signal_observation
    ADD CONSTRAINT fraud_signal_observation_entity_kind_check
    CHECK (entity_kind = ANY (ARRAY[
        'committee'::TEXT,
        'candidate'::TEXT,
        'treasurer'::TEXT,
        'address'::TEXT,
        'donor_cluster'::TEXT,
        'contractor'::TEXT,
        'donor'::TEXT,
        'nj_state_candidate'::TEXT,
        'provider'::TEXT,
        'employer'::TEXT
    ]));

COMMENT ON CONSTRAINT fraud_signal_observation_entity_kind_check
    ON derived.fraud_signal_observation IS
    'Whitelist of entity_kind values. employer added by mig 121 (canonical '
    'H-1B petitioner / LCA employer name) for FRAUD-V1. Ten kinds as of '
    'mig 121.';


-- ----------------------------------------------------------------------------
-- 2. signal_family CHECK widening: + h1b_wage, h1b_adjudication, h1b_cross_source
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_config
    DROP CONSTRAINT IF EXISTS fraud_signal_config_signal_family_check;

ALTER TABLE derived.fraud_signal_config
    ADD CONSTRAINT fraud_signal_config_signal_family_check
    CHECK (signal_family IN (
        'leie_bearing',
        'workforce',
        'address',
        'structural',
        'sam_bearing',
        'state_exclusion',
        'cms_utilization',
        'cms_temporal',
        'h1b_wage',
        'h1b_adjudication',
        'h1b_cross_source'
    ));

COMMENT ON COLUMN derived.fraud_signal_config.signal_family IS
    'Whitelist of signal_family values. Eleven families as of migration 121. '
    'h1b_wage / h1b_adjudication / h1b_cross_source are held independent so '
    'an employer firing more than one family earns the diversity bonus.';


-- ----------------------------------------------------------------------------
-- 3. citation_authority + upstream_source CHECK widening
-- ----------------------------------------------------------------------------
ALTER TABLE ref.fraud_signal_human_explanation
    DROP CONSTRAINT IF EXISTS fraud_signal_human_explanation_authority_chk;

ALTER TABLE ref.fraud_signal_human_explanation
    ADD CONSTRAINT fraud_signal_human_explanation_authority_chk
    CHECK (citation_authority IN (
        'FEC',
        'HHS-OIG',
        'GSA-SAM',
        'FAR-Council',
        'DOJ',
        'CRS',
        'platform',
        'NJ-OSC',
        'DOL-OFLC',
        'USCIS'
    ));

ALTER TABLE ref.fraud_signal_severity_calibration
    DROP CONSTRAINT IF EXISTS fraud_signal_severity_calibration_basis_chk;

ALTER TABLE ref.fraud_signal_severity_calibration
    ADD CONSTRAINT fraud_signal_severity_calibration_basis_chk
    CHECK (calibration_basis IN (
        'fec_mur',
        'oig_report',
        'doj_filing',
        'crs_analysis',
        'far_authority',
        'fec_advisory',
        'empirical_pctile',
        'state_exclusion',
        'inferred_identity',
        'statutory_cfr'
    ));

ALTER TABLE ref.fraud_signal_evidence_url_template
    DROP CONSTRAINT IF EXISTS fraud_signal_evidence_url_template_upstream_chk;

ALTER TABLE ref.fraud_signal_evidence_url_template
    ADD CONSTRAINT fraud_signal_evidence_url_template_upstream_chk
    CHECK (upstream_source IN (
        'FEC.gov',
        'OIG.gov',
        'SAM.gov',
        'USAspending.gov',
        'platform-internal',
        'NJ.gov',
        'CMS.gov',
        'DOL.gov',
        'USCIS.gov'
    ));


-- ----------------------------------------------------------------------------
-- 4. raw.uscis_h1b_employer (POP-3)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.uscis_h1b_employer (
    fiscal_year              SMALLINT      NOT NULL
        CHECK (fiscal_year BETWEEN 2009 AND 2099),
    employer_name            TEXT          NOT NULL,
    employer_canonical_name  TEXT          NOT NULL,
    tax_id_last4             TEXT          NOT NULL DEFAULT '',
    naics_code               TEXT,
    petitioner_city          TEXT          NOT NULL DEFAULT '',
    petitioner_state         TEXT          NOT NULL DEFAULT '',
    petitioner_zip           CHAR(5),
    initial_approval         INTEGER       NOT NULL DEFAULT 0
        CHECK (initial_approval >= 0),
    initial_denial           INTEGER       NOT NULL DEFAULT 0
        CHECK (initial_denial >= 0),
    continuing_approval      INTEGER       NOT NULL DEFAULT 0
        CHECK (continuing_approval >= 0),
    continuing_denial        INTEGER       NOT NULL DEFAULT 0
        CHECK (continuing_denial >= 0),
    source_filename          TEXT          NOT NULL,
    source_sha256            CHAR(64)      NOT NULL,
    source_vintage           TEXT          NOT NULL,
    data_quality             TEXT          NOT NULL DEFAULT 'measured'
        CHECK (data_quality IN ('measured', 'computed', 'modeled')),
    ingested_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (
        fiscal_year,
        employer_canonical_name,
        petitioner_state,
        petitioner_city,
        tax_id_last4
    )
);

CREATE INDEX IF NOT EXISTS idx_uscis_h1b_state_fy
    ON raw.uscis_h1b_employer (petitioner_state, fiscal_year);

CREATE INDEX IF NOT EXISTS idx_uscis_h1b_canonical_fy
    ON raw.uscis_h1b_employer (employer_canonical_name, fiscal_year);

COMMENT ON TABLE raw.uscis_h1b_employer IS
    'USCIS H-1B Employer Data Hub, one row per (FY, canonical employer, '
    'petitioner city/state, last-4 EIN). Approvals/denials are FIRST '
    'decisions on initial and continuing petitions, not unique workers. '
    'POP-3. data_quality=measured. formula 3.7.0-fraud-h1b-employer-lane-v1.';


-- ----------------------------------------------------------------------------
-- 5. NJ H-1B LCA read surface
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_lca_nj_h1b AS
SELECT
    l.*,
    (UPPER(TRIM(l.worksite_state)) = 'NJ')   AS is_nj_worksite,
    (UPPER(TRIM(l.employer_state)) = 'NJ')   AS is_nj_employer
FROM raw.lca_disclosure l
WHERE l.visa_class = 'H-1B'
  AND (
      UPPER(TRIM(l.worksite_state)) = 'NJ'
      OR UPPER(TRIM(l.employer_state)) = 'NJ'
  );

COMMENT ON VIEW derived.v_lca_nj_h1b IS
    'NJ-scoped H-1B LCA rows (worksite or employer state = NJ). Read '
    'surface for FRAUD-V1 wage / withdrawn detectors. formula 3.7.0.';


-- ----------------------------------------------------------------------------
-- 6. Platform constants (cited, versioned)
-- ----------------------------------------------------------------------------
INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date)
VALUES
(
    'h1b_below_pw_min_gap_usd',
    500,
    'Minimum annualized offered-wage shortfall versus prevailing wage '
    '(USD) before a CERTIFIED H-1B LCA counts as a below-PW lead.',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    '20 CFR 655.731 requires the H-1B wage to equal or exceed the '
    'prevailing wage. A $500 annualized floor is a platform calibration '
    'that drops hourly-to-annual rounding noise (Hour x 2080 GENERATED '
    'columns in raw.lca_disclosure) without silently forgiving material '
    'shortfalls. Not a DOL enforcement threshold.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'h1b_denial_tail_pctile',
    0.99,
    'CUME_DIST cutoff (top 1%) for employer_h1b_denial_rate_outlier '
    'among NJ USCIS petitioners in a fiscal year.',
    'https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub',
    'Empirical platform calibration. Matches the 99th-percentile tail '
    'used by CMS utilization detectors. A high denial rate is a lead, '
    'not a finding — new petitioners and lottery-heavy specialties '
    'legitimately sit in the tail.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'h1b_denial_min_petitions',
    10,
    'Minimum USCIS first-decisions (approvals + denials) for an employer '
    'to enter the denial-rate ranking.',
    'https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub',
    'Empirical platform calibration. CUME_DIST on a handful of decisions '
    'is noise; ten first-decisions is the minimum for a rate to be '
    'interpretable at employer grain.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'h1b_gap_tail_pctile',
    0.99,
    'CUME_DIST cutoff (top 1%) for employer_lca_uscis_volume_gap among '
    'NJ employers present in both LCA and USCIS files.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. LCA volume overstates USCIS '
    'approvals by construction (pre-adjudication). Only the extreme '
    'tail of the LCA-workers / approvals ratio is a lead.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'h1b_gap_min_lca_workers',
    20,
    'Minimum certified H-1B LCA worker count for an employer to enter '
    'the LCA-vs-USCIS volume-gap ranking.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. Small employers with 1-2 LCAs and '
    'zero USCIS approvals produce explosive ratios; twenty certified '
    'workers is the material-exposure floor.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'h1b_cw_tail_pctile',
    0.99,
    'CUME_DIST cutoff (top 1%) for employer_certified_withdrawn_rate_outlier.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. A high CERTIFIED-WITHDRAWN share '
    'is a recognized benching / file-then-abandon lead, not proof of '
    'a violation.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'h1b_cw_min_cases',
    10,
    'Minimum decided NJ H-1B LCA cases for an employer to enter the '
    'certified-withdrawn ranking.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. Same rationale as the denial-rate '
    'petition floor: rates on tiny denominators are not leads.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


-- ----------------------------------------------------------------------------
-- 7. Refreshers
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_below_prevailing_wage(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_min_gap  NUMERIC := derived.f_platform_constant('h1b_below_pw_min_gap_usd');
BEGIN
    IF v_min_gap IS NULL THEN
        RAISE EXCEPTION
            'employer_below_prevailing_wage: missing h1b_below_pw_min_gap_usd'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_below_prevailing_wage';

    WITH src AS (
        SELECT
            employer_canonical_name,
            SUM(annualized_pw - annualized_wage_from) AS gap_usd,
            COUNT(*)                                  AS n_cases
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status = 'CERTIFIED'
          AND annualized_wage_from IS NOT NULL
          AND annualized_pw IS NOT NULL
          AND (annualized_pw - annualized_wage_from) >= v_min_gap
        GROUP BY 1
    ),
    pop AS (
        SELECT COUNT(DISTINCT employer_canonical_name)::NUMERIC AS n_in_bucket
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status = 'CERTIFIED'
    ),
    flag AS (
        SELECT COUNT(*)::NUMERIC AS n_flagged FROM src
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        s.employer_canonical_name,
        'employer_below_prevailing_wage',
        s.gap_usd,
        5::SMALLINT,
        'kind=employer|visa=H-1B|fy=' || p_cycle,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (f.n_flagged / NULLIF(pop.n_in_bucket, 0))
        ),
        '/risk/employer/' || replace(s.employer_canonical_name, ' ', '%20')
            || '?signal=employer_below_prevailing_wage&cycle=' || p_cycle
    FROM src s
    CROSS JOIN pop
    CROSS JOIN flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_below_prevailing_wage(CHAR) IS
    'FRAUD-V1: CERTIFIED NJ H-1B LCAs with annualized offered wage below '
    'prevailing wage by >= h1b_below_pw_min_gap_usd. INA 212(n) / 20 CFR '
    '655.731. Idempotent DELETE+INSERT. Returns 0 on empty LCA substrate.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_h1b_denial_rate_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted  INT;
    v_year      INT     := CAST(p_cycle AS INT);
    v_tail      NUMERIC := derived.f_platform_constant('h1b_denial_tail_pctile');
    v_min_pets  NUMERIC := derived.f_platform_constant('h1b_denial_min_petitions');
BEGIN
    IF v_tail IS NULL OR v_min_pets IS NULL THEN
        RAISE EXCEPTION
            'employer_h1b_denial_rate_outlier: missing platform_constants'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_h1b_denial_rate_outlier';

    WITH emp AS (
        SELECT
            employer_canonical_name,
            SUM(initial_approval + continuing_approval) AS approvals,
            SUM(initial_denial + continuing_denial)     AS denials
        FROM raw.uscis_h1b_employer
        WHERE fiscal_year = v_year
          AND UPPER(TRIM(petitioner_state)) = 'NJ'
        GROUP BY 1
    ),
    ranked AS (
        SELECT
            employer_canonical_name,
            denials::NUMERIC / (approvals + denials) AS denial_rate,
            CUME_DIST() OVER (
                ORDER BY denials::NUMERIC / (approvals + denials)
            ) AS pctile
        FROM emp
        WHERE (approvals + denials) >= v_min_pets
          AND (approvals + denials) > 0
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        r.employer_canonical_name,
        'employer_h1b_denial_rate_outlier',
        r.denial_rate,
        4::SMALLINT,
        'kind=employer|src=uscis|state=NJ|fy=' || p_cycle,
        r.pctile,
        '/risk/employer/' || replace(r.employer_canonical_name, ' ', '%20')
            || '?signal=employer_h1b_denial_rate_outlier&cycle=' || p_cycle
    FROM ranked r
    WHERE r.pctile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_h1b_denial_rate_outlier(CHAR) IS
    'FRAUD-V1: NJ USCIS H-1B petitioners in the top tail of denial rate. '
    'Empirical. Returns 0 when USCIS substrate is empty.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_lca_uscis_volume_gap(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_tail     NUMERIC := derived.f_platform_constant('h1b_gap_tail_pctile');
    v_min_lca  NUMERIC := derived.f_platform_constant('h1b_gap_min_lca_workers');
BEGIN
    IF v_tail IS NULL OR v_min_lca IS NULL THEN
        RAISE EXCEPTION
            'employer_lca_uscis_volume_gap: missing platform_constants'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_lca_uscis_volume_gap';

    WITH lca AS (
        SELECT
            employer_canonical_name,
            SUM(COALESCE(total_workers, 1)) AS lca_workers
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status IN ('CERTIFIED', 'CERTIFIED-WITHDRAWN')
        GROUP BY 1
    ),
    uscis AS (
        SELECT
            employer_canonical_name,
            SUM(initial_approval + continuing_approval) AS approvals
        FROM raw.uscis_h1b_employer
        WHERE fiscal_year = v_year
        GROUP BY 1
    ),
    joined AS (
        SELECT
            l.employer_canonical_name,
            l.lca_workers,
            COALESCE(u.approvals, 0) AS approvals,
            l.lca_workers::NUMERIC
                / GREATEST(COALESCE(u.approvals, 0), 1)::NUMERIC AS gap_ratio
        FROM lca l
        JOIN uscis u USING (employer_canonical_name)
        WHERE l.lca_workers >= v_min_lca
    ),
    ranked AS (
        SELECT
            *,
            CUME_DIST() OVER (ORDER BY gap_ratio) AS pctile
        FROM joined
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        r.employer_canonical_name,
        'employer_lca_uscis_volume_gap',
        r.gap_ratio,
        3::SMALLINT,
        'kind=employer|src=lca_x_uscis|fy=' || p_cycle,
        r.pctile,
        '/risk/employer/' || replace(r.employer_canonical_name, ' ', '%20')
            || '?signal=employer_lca_uscis_volume_gap&cycle=' || p_cycle
    FROM ranked r
    WHERE r.pctile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_lca_uscis_volume_gap(CHAR) IS
    'FRAUD-V1: extreme LCA-certified-workers / USCIS-approvals ratio for '
    'employers in both sources. Empirical tail. Returns 0 if either source '
    'is empty.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_certified_withdrawn_rate_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_tail     NUMERIC := derived.f_platform_constant('h1b_cw_tail_pctile');
    v_min      NUMERIC := derived.f_platform_constant('h1b_cw_min_cases');
BEGIN
    IF v_tail IS NULL OR v_min IS NULL THEN
        RAISE EXCEPTION
            'employer_certified_withdrawn_rate_outlier: missing constants'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_certified_withdrawn_rate_outlier';

    WITH emp AS (
        SELECT
            employer_canonical_name,
            COUNT(*) FILTER (WHERE case_status = 'CERTIFIED-WITHDRAWN') AS n_cw,
            COUNT(*) FILTER (
                WHERE case_status IN (
                    'CERTIFIED', 'CERTIFIED-WITHDRAWN', 'DENIED', 'WITHDRAWN'
                )
            ) AS n_decided
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
        GROUP BY 1
    ),
    ranked AS (
        SELECT
            employer_canonical_name,
            n_cw::NUMERIC / n_decided AS cw_rate,
            CUME_DIST() OVER (ORDER BY n_cw::NUMERIC / n_decided) AS pctile
        FROM emp
        WHERE n_decided >= v_min
          AND n_decided > 0
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        r.employer_canonical_name,
        'employer_certified_withdrawn_rate_outlier',
        r.cw_rate,
        3::SMALLINT,
        'kind=employer|visa=H-1B|fy=' || p_cycle,
        r.pctile,
        '/risk/employer/' || replace(r.employer_canonical_name, ' ', '%20')
            || '?signal=employer_certified_withdrawn_rate_outlier&cycle=' || p_cycle
    FROM ranked r
    WHERE r.pctile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_certified_withdrawn_rate_outlier(CHAR) IS
    'FRAUD-V1: NJ H-1B employers in the top tail of CERTIFIED-WITHDRAWN '
    'share. Empirical benching / file-then-abandon lead.';


-- ----------------------------------------------------------------------------
-- 8. fraud_signal_config rows
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES
(
    'employer_below_prevailing_wage',
    'h1b_wage',
    500,
    'CERTIFIED NJ H-1B LCA with annualized offered wage below prevailing '
    'wage by >= h1b_below_pw_min_gap_usd. Statutory 20 CFR 655.731. '
    'raw_value = USD gap. Severity 5.'
),
(
    'employer_h1b_denial_rate_outlier',
    'h1b_adjudication',
    0,
    'NJ USCIS H-1B petitioner in the top 1% of denial rate among employers '
    'with >= h1b_denial_min_petitions first-decisions. Empirical. '
    'raw_value = denial rate. Severity 4.'
),
(
    'employer_lca_uscis_volume_gap',
    'h1b_cross_source',
    0,
    'Employer in both LCA and USCIS files whose certified-worker / approval '
    'ratio is in the top 1% of NJ matched employers. Empirical. '
    'raw_value = ratio. Severity 3.'
),
(
    'employer_certified_withdrawn_rate_outlier',
    'h1b_wage',
    0,
    'NJ H-1B employer in the top 1% of CERTIFIED-WITHDRAWN share. Empirical '
    'benching lead. raw_value = rate. Severity 3.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 9. Evidence view: + employer_meta
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_entity_fraud_evidence AS
WITH cand_meta AS (
    SELECT
        cycle,
        cand_id,
        cand_name,
        cand_office,
        cand_office_st,
        cand_office_district,
        cand_pty_affiliation,
        cand_ici,
        cand_status,
        cand_election_yr,
        (cand_office_st = 'NJ')                                  AS is_nj
    FROM raw.fec_candidate
),
cmte_meta AS (
    SELECT
        cycle,
        cmte_id,
        cmte_nm,
        cmte_st,
        cmte_city,
        cmte_zip,
        tres_nm,
        cand_id                                                  AS pcc_cand_id,
        (cmte_st = 'NJ')                                         AS is_nj
    FROM raw.fec_committee
),
treas_meta AS (
    SELECT
        cycle,
        UPPER(TRIM(tres_nm))                                     AS treasurer_id,
        BOOL_OR(cmte_st = 'NJ')                                  AS is_nj,
        COUNT(DISTINCT cmte_id)                                  AS n_committees_treasured,
        COUNT(DISTINCT cmte_id) FILTER (WHERE cmte_st = 'NJ')    AS n_nj_committees_treasured
    FROM raw.fec_committee
    WHERE tres_nm IS NOT NULL AND tres_nm <> ''
    GROUP BY 1, 2
),
nj_state_meta AS (
    SELECT
        candidate_id                                              AS nj_candidate_id,
        full_name                                                 AS nj_full_name,
        TRUE                                                      AS is_nj
    FROM ref.nj_state_candidate
),
provider_meta AS (
    SELECT DISTINCT ON (provider_npi, provider_data_year)
        provider_npi,
        provider_data_year,
        provider_name,
        is_nj
    FROM (
        SELECT
            npi                                                   AS provider_npi,
            data_year                                             AS provider_data_year,
            NULLIF(TRIM(
                COALESCE(prscrbr_first_name, '') || ' ' ||
                COALESCE(prscrbr_last_org_name, '')
            ), '')                                                AS provider_name,
            (prscrbr_state_abrvtn = 'NJ')                         AS is_nj,
            1                                                     AS pref
        FROM raw.cms_partd_prescriber
        UNION ALL
        SELECT
            npi                                                   AS provider_npi,
            data_year                                             AS provider_data_year,
            NULLIF(TRIM(
                COALESCE(prvdr_first_name, '') || ' ' ||
                COALESCE(prvdr_last_org_name, '')
            ), '')                                                AS provider_name,
            (prvdr_state_abrvtn = 'NJ')                           AS is_nj,
            2                                                     AS pref
        FROM raw.cms_physician_provider
        UNION ALL
        SELECT
            covered_recipient_npi                                 AS provider_npi,
            program_year                                          AS provider_data_year,
            NULLIF(TRIM(
                COALESCE(recipient_first_name, '') || ' ' ||
                COALESCE(recipient_last_name, '')
            ), '')                                                AS provider_name,
            (recipient_state = 'NJ')                              AS is_nj,
            3                                                     AS pref
        FROM raw.cms_open_payments_general
        WHERE covered_recipient_npi ~ '^[0-9]{10}$'
    ) u
    ORDER BY provider_npi, provider_data_year, pref
),
employer_meta AS (
    SELECT DISTINCT ON (employer_canonical_name, fiscal_year)
        employer_canonical_name,
        fiscal_year,
        employer_name,
        is_nj
    FROM (
        SELECT
            employer_canonical_name,
            fiscal_year,
            employer_name,
            TRUE AS is_nj,
            1 AS pref
        FROM derived.v_lca_nj_h1b
        UNION ALL
        SELECT
            employer_canonical_name,
            fiscal_year,
            employer_name,
            (UPPER(TRIM(petitioner_state)) = 'NJ') AS is_nj,
            2 AS pref
        FROM raw.uscis_h1b_employer
    ) u
    ORDER BY employer_canonical_name, fiscal_year, pref, employer_name
)
SELECT
    o.cycle,
    o.entity_kind,
    o.entity_id,
    o.signal_id,
    o.raw_value,
    COALESCE(sc.severity_level, o.severity)                      AS severity,
    o.peer_bucket,
    o.peer_percentile,
    o.materialized_at,

    CASE o.entity_kind
        WHEN 'candidate'          THEN COALESCE(cand.is_nj,  FALSE)
        WHEN 'committee'          THEN COALESCE(cmte.is_nj,  FALSE)
        WHEN 'treasurer'          THEN COALESCE(treas.is_nj, FALSE)
        WHEN 'address'            THEN (SPLIT_PART(o.entity_id, '|', 3) = 'NJ')
        WHEN 'nj_state_candidate' THEN COALESCE(nj.is_nj, TRUE)
        WHEN 'provider'           THEN COALESCE(prov.is_nj, FALSE)
        WHEN 'employer'           THEN COALESCE(emp.is_nj, FALSE)
        ELSE FALSE
    END                                                          AS is_nj,

    CASE o.entity_kind
        WHEN 'candidate'          THEN cand.cand_name
        WHEN 'committee'          THEN cmte.cmte_nm
        WHEN 'treasurer'          THEN o.entity_id
        WHEN 'address'            THEN SPLIT_PART(o.entity_id, '|', 1)
                                       || COALESCE(', ' || SPLIT_PART(o.entity_id, '|', 2), '')
                                       || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 3), '')
                                       || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 4), '')
        WHEN 'nj_state_candidate' THEN nj.nj_full_name
        WHEN 'provider'           THEN COALESCE(prov.provider_name, o.entity_id)
        WHEN 'employer'           THEN COALESCE(emp.employer_name, o.entity_id)
        ELSE o.entity_id
    END                                                          AS display_name,

    cand.cand_office                                             AS office_code,
    cand.cand_office_st                                          AS office_state,
    cand.cand_office_district                                    AS office_district,
    cand.cand_pty_affiliation                                    AS office_party,
    cand.cand_ici                                                AS office_incumbent_status,
    cand.cand_election_yr                                        AS office_election_year,

    treas.n_committees_treasured                                 AS treasurer_n_committees,
    treas.n_nj_committees_treasured                              AS treasurer_n_nj_committees,

    cmte.cmte_st                                                 AS committee_state,
    cmte.cmte_city                                               AS committee_city,
    cmte.tres_nm                                                 AS committee_treasurer_name,
    cmte.pcc_cand_id                                             AS committee_pcc_candidate_id,

    he.rule_text                                                 AS rule_text,
    he.citation_authority                                        AS citation_authority,
    he.citation_section                                          AS citation_section,
    he.citation_url                                              AS citation_url,

    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        COALESCE(he.plain_english_template, ''),
        '{{entity_id}}',       o.entity_id),
        '{{cycle}}',           o.cycle),
        '{{raw_value}}',       COALESCE(o.raw_value::TEXT, '')),
        '{{peer_percentile}}', COALESCE(ROUND(o.peer_percentile * 100, 1)::TEXT, '')),
        '{{entity_kind}}',     COALESCE(o.entity_kind, '')),
        '{{peer_bucket}}',     COALESCE(o.peer_bucket, ''))
                                                                 AS rendered_explanation,

    sc.calibration_basis                                         AS severity_basis,
    sc.precedent_url                                             AS severity_precedent_url,
    sc.precedent_summary                                         AS severity_precedent_summary,

    REPLACE(REPLACE(
        COALESCE(eut.url_template, o.evidence_url),
        '{{entity_id}}', o.entity_id),
        '{{cycle}}',     o.cycle)
                                                                 AS upstream_verify_url,
    eut.button_label                                             AS upstream_verify_label,
    eut.upstream_source                                          AS upstream_source,

    he.formula_version                                           AS formula_version
FROM   derived.fraud_signal_observation        o
LEFT JOIN cand_meta                            cand
       ON o.entity_kind = 'candidate'
      AND cand.cycle    = o.cycle
      AND cand.cand_id  = o.entity_id
LEFT JOIN cmte_meta                            cmte
       ON o.entity_kind = 'committee'
      AND cmte.cycle    = o.cycle
      AND cmte.cmte_id  = o.entity_id
LEFT JOIN treas_meta                           treas
       ON o.entity_kind     = 'treasurer'
      AND treas.cycle       = o.cycle
      AND treas.treasurer_id = UPPER(TRIM(o.entity_id))
LEFT JOIN nj_state_meta                        nj
       ON o.entity_kind     = 'nj_state_candidate'
      AND nj.nj_candidate_id = o.entity_id
LEFT JOIN provider_meta                        prov
       ON o.entity_kind          = 'provider'
      AND prov.provider_npi      = o.entity_id
      AND prov.provider_data_year = o.cycle::INT
LEFT JOIN employer_meta                        emp
       ON o.entity_kind = 'employer'
      AND emp.employer_canonical_name = o.entity_id
      AND emp.fiscal_year = o.cycle::INT
LEFT JOIN ref.fraud_signal_human_explanation        he   ON he.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_severity_calibration     sc   ON sc.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_evidence_url_template    eut  ON eut.signal_id = o.signal_id;

COMMENT ON VIEW derived.v_entity_fraud_evidence IS
    'Canonical evidence join. Mig 121 adds employer_meta (LCA preferred, '
    'USCIS fallback) so /risk/employer/<canonical> resolves a display name.';


-- ----------------------------------------------------------------------------
-- 10. Employer lead queue view
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_h1b_employer_leads AS
SELECT
    o.cycle,
    o.entity_id,
    MAX(e.display_name)                                          AS display_name,
    BOOL_OR(COALESCE(e.is_nj, FALSE))                            AS is_nj,
    MAX(r.risk_score)                                            AS risk_score,
    COUNT(DISTINCT o.signal_id)::INT                             AS n_signals,
    MAX(o.severity)                                              AS max_severity,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_below_prevailing_wage'
    )                                                            AS below_pw_gap_usd,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_h1b_denial_rate_outlier'
    )                                                            AS denial_rate,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_lca_uscis_volume_gap'
    )                                                            AS lca_uscis_gap_ratio,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_certified_withdrawn_rate_outlier'
    )                                                            AS certified_withdrawn_rate,
    MAX(o.signal_id) FILTER (
        WHERE o.severity = (
            SELECT MAX(o2.severity)
            FROM derived.fraud_signal_observation o2
            WHERE o2.cycle = o.cycle
              AND o2.entity_kind = 'employer'
              AND o2.entity_id = o.entity_id
        )
    )                                                            AS preview_signal_id
FROM derived.fraud_signal_observation o
LEFT JOIN derived.v_entity_fraud_evidence e
       ON e.cycle = o.cycle
      AND e.entity_kind = o.entity_kind
      AND e.entity_id = o.entity_id
      AND e.signal_id = o.signal_id
LEFT JOIN derived.v_entity_fraud_risk r
       ON r.cycle = o.cycle
      AND r.entity_kind = o.entity_kind
      AND r.entity_id = o.entity_id
WHERE o.entity_kind = 'employer'
  AND o.signal_id IN (
        'employer_below_prevailing_wage',
        'employer_h1b_denial_rate_outlier',
        'employer_lca_uscis_volume_gap',
        'employer_certified_withdrawn_rate_outlier'
  )
GROUP BY o.cycle, o.entity_id;

COMMENT ON VIEW derived.v_h1b_employer_leads IS
    'One row per (cycle, employer) with stacked H-1B FRAUD-V1 signals. '
    'Read surface for /h1b. formula 3.7.0-fraud-h1b-employer-lane-v1.';


-- ----------------------------------------------------------------------------
-- 11. Master refresher: TIER 11; 28 -> 32
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_total INT := 0;
    n_each  INT;
BEGIN
    SELECT derived.refresh_treasurer_concentration_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_candidate_no_pcc_observations(p_cycle)           INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_candidate_broken_pcc_observations(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_candidate_multiple_pccs_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_committee_address_clusters_observations(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_committee_name_collisions_observations(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_candidate_namesakes_observations(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_treasurer_is_candidate_observations(p_cycle)     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_on_leie(p_cycle)                   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_entity_on_leie_strict_address(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_donor_on_leie(p_cycle)                    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_nj_state_candidate_on_leie(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_excluded_via_sam_uei(p_cycle)      INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_donor_on_sam(p_cycle)                     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_funded_and_excluded(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_donor_employed_by_nj_contractor(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_provider_excluded_billing(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_provider_excluded_billing_partb(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_state_excluded_provider_billing(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_services_per_beneficiary_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_antipsychotic_elderly_outlier(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_name_resolved_excluded_provider_billing(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_excluded_provider_received_open_payments(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_provider_billing_growth_outlier(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- TIER 11: H-1B employer visa-fraud leads (4 -- mig 121)
    SELECT derived.refresh_signal_employer_below_prevailing_wage(p_cycle)   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_h1b_denial_rate_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_lca_uscis_volume_gap(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_certified_withdrawn_rate_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
    'Master fraud-signal refresher. Invokes all 32 seeded signal refreshers '
    'in substrate-dependency tier order. Mig 121 raises 28 -> 32 by adding '
    'TIER 11 (H-1B employer visa-fraud leads). Empty substrate returns 0.';


COMMIT;
