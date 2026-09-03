-- ============================================================================
-- Migration: 122_fraud_h1b_attestation_whd
--
-- FRAUD-V1b: LCA attestation columns + DOL WHD official lists + four
-- additional H-1B employer leads. RATIONALE is in work_left.txt
-- (session 2026-09-02b). Stacks on mig 121.
--
-- SIGNALS (four new; 32 -> 36)
-- ----------------------------
-- 5. employer_on_whd_willful_or_debarred (family h1b_enforcement, severity 5)
--    Canonical name matches an *active* WHD debarment or willful-violator
--    row AND the employer appears in NJ LCA or NJ USCIS for the cycle.
--    Official list. 20 CFR 655.736 / 655.750(d); Fact Sheet 62S.
--
-- 6. employer_level1_wage_share_outlier (family h1b_wage, severity 3)
--    Share of CERTIFIED NJ H-1B LCAs with PW_WAGE_LEVEL = 'I' in the
--    extreme upper tail. Level I is a legal OFLC wage tier, not fraud;
--    only the peer tail is a lead. Empirical.
--
-- 7. employer_secondary_entity_share_outlier (family h1b_wage, severity 3)
--    Share of CERTIFIED NJ H-1B LCAs with SECONDARY_ENTITY = 'Y'
--    (ETA-9035 §F.a.2 third-party placement attestation) in the tail.
--
-- 8. employer_h1b_dependent_plus_anomaly (family h1b_cross_source, severity 4)
--    H-1B_DEPENDENT = 'Y' on at least one CERTIFIED NJ LCA AND the
--    employer also fires below-PW, volume-gap, Level I, or secondary-
--    entity. Dependency cutoffs are statutory (20 CFR 655.736); we use
--    the attestation bit as a bucket, not a recomputed headcount.
--
-- NO MAGIC NUMBERS: cutoffs live in ref.platform_constants.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    'Pillar 2 FRAUD-V1b. Extends raw.lca_disclosure with ETA-9035 '
    'attestation columns (FEIN, H-1B_DEPENDENT, WILLFUL_VIOLATOR, '
    'SECONDARY_ENTITY, PW_WAGE_LEVEL), adds raw.dol_whd_h1b_list '
    '(WHD debarment + willful-violator HTML lists), and four H-1B '
    'leads: official-list match, Level I wage-share tail, secondary-'
    'entity share tail, H-1B-dependent plus corroborating anomaly. '
    'Scores remain peer-percentile composites, not P(fraud).',
    '2026-09-02',
    'Stacks on 3.7.0-fraud-h1b-employer-lane-v1. Master refresher '
    '32 -> 36 (TIER 11).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. LCA attestation columns (nullable; pre-v4 files stay NULL)
-- ----------------------------------------------------------------------------
ALTER TABLE raw.lca_disclosure
    ADD COLUMN IF NOT EXISTS employer_fein TEXT,
    ADD COLUMN IF NOT EXISTS h1b_dependent CHAR(1),
    ADD COLUMN IF NOT EXISTS willful_violator CHAR(1),
    ADD COLUMN IF NOT EXISTS secondary_entity CHAR(1),
    ADD COLUMN IF NOT EXISTS secondary_entity_business_name TEXT,
    ADD COLUMN IF NOT EXISTS pw_wage_level TEXT;

ALTER TABLE raw.lca_disclosure
    DROP CONSTRAINT IF EXISTS lca_disclosure_h1b_dependent_chk;
ALTER TABLE raw.lca_disclosure
    ADD CONSTRAINT lca_disclosure_h1b_dependent_chk
    CHECK (h1b_dependent IS NULL OR h1b_dependent IN ('Y', 'N'));

ALTER TABLE raw.lca_disclosure
    DROP CONSTRAINT IF EXISTS lca_disclosure_willful_violator_chk;
ALTER TABLE raw.lca_disclosure
    ADD CONSTRAINT lca_disclosure_willful_violator_chk
    CHECK (willful_violator IS NULL OR willful_violator IN ('Y', 'N'));

ALTER TABLE raw.lca_disclosure
    DROP CONSTRAINT IF EXISTS lca_disclosure_secondary_entity_chk;
ALTER TABLE raw.lca_disclosure
    ADD CONSTRAINT lca_disclosure_secondary_entity_chk
    CHECK (secondary_entity IS NULL OR secondary_entity IN ('Y', 'N'));

ALTER TABLE raw.lca_disclosure
    DROP CONSTRAINT IF EXISTS lca_disclosure_pw_wage_level_chk;
ALTER TABLE raw.lca_disclosure
    ADD CONSTRAINT lca_disclosure_pw_wage_level_chk
    CHECK (pw_wage_level IS NULL OR pw_wage_level IN ('I', 'II', 'III', 'IV'));

COMMENT ON COLUMN raw.lca_disclosure.employer_fein IS
    'EMPLOYER_FEIN from ETA-9035, digits only. Join key to USCIS last-4 '
    'via RIGHT(employer_fein, 4). Never unique by itself. Added mig 122.';

COMMENT ON COLUMN raw.lca_disclosure.h1b_dependent IS
    'H-1B_DEPENDENT attestation (Y/N). Statutory bucket under 20 CFR '
    '655.736, not a score. Added mig 122.';

COMMENT ON COLUMN raw.lca_disclosure.willful_violator IS
    'WILLFUL_VIOLATOR self-attestation on the LCA (Y/N). Distinct from '
    'the WHD official willful list. Added mig 122.';

COMMENT ON COLUMN raw.lca_disclosure.secondary_entity IS
    'SECONDARY_ENTITY third-party-placement attestation, ETA-9035 '
    '§F.a.2 (Y/N). Added mig 122.';

COMMENT ON COLUMN raw.lca_disclosure.pw_wage_level IS
    'OFLC prevailing-wage level I-IV. Level I is a legal tier, not a '
    'fraud marker. Added mig 122.';


-- ----------------------------------------------------------------------------
-- 2. raw.dol_whd_h1b_list (official WHD HTML lists)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.dol_whd_h1b_list (
    list_kind                TEXT          NOT NULL
        CHECK (list_kind IN ('debarment', 'willful')),
    employer_name            TEXT          NOT NULL,
    employer_canonical_name  TEXT          NOT NULL,
    employer_address         TEXT,
    city                     TEXT,
    state                    TEXT,
    willful_violator         BOOLEAN,
    debarment_start          DATE,
    debarment_end            DATE,
    determination_date       DATE,
    determining_agency       TEXT,
    list_effective_date      DATE,
    source_page_updated      DATE,
    source_url               TEXT          NOT NULL,
    source_filename          TEXT          NOT NULL,
    source_sha256            CHAR(64)      NOT NULL,
    data_quality             TEXT          NOT NULL DEFAULT 'measured'
        CHECK (data_quality IN ('measured', 'computed', 'modeled')),
    ingested_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (list_kind, employer_canonical_name)
);

CREATE INDEX IF NOT EXISTS idx_whd_h1b_canonical
    ON raw.dol_whd_h1b_list (employer_canonical_name);

COMMENT ON TABLE raw.dol_whd_h1b_list IS
    'DOL WHD H-1B debarment and willful-violator lists, parsed from the '
    'public HTML tables. Full-replace per ingest. National grain; join to '
    'NJ LCA/USCIS by canonical employer name. data_quality=measured. '
    'formula 3.8.0-fraud-h1b-attestation-enforcement-v1. '
    'https://www.dol.gov/agencies/whd/immigration/h1b/debarment '
    'https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list';


-- ----------------------------------------------------------------------------
-- 3. citation_authority: + DOL-WHD
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
        'USCIS',
        'DOL-WHD'
    ));


-- ----------------------------------------------------------------------------
-- 4. signal_family: + h1b_enforcement
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
        'h1b_cross_source',
        'h1b_enforcement'
    ));

COMMENT ON COLUMN derived.fraud_signal_config.signal_family IS
    'Whitelist of signal_family values. Twelve families as of migration 122. '
    'h1b_enforcement is the official WHD list family, held independent so '
    'an employer on the list plus a wage tail earns the diversity bonus.';


-- ----------------------------------------------------------------------------
-- 5. Platform constants
-- ----------------------------------------------------------------------------
INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date)
VALUES
(
    'h1b_willful_lookback_years',
    5,
    'Years a WHD willful-violator determination stays active for the '
    'employer_on_whd_willful_or_debarred join.',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    '20 CFR 655.736: an employer found to have committed a willful '
    'failure or a misrepresentation of a material fact is a willful '
    'violator for a period of 5 years from the date of the finding. '
    'This constant is that statutory duration, not a platform cutoff.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'h1b_level1_tail_pctile',
    0.99,
    'CUME_DIST cutoff (top 1%) for employer_level1_wage_share_outlier '
    'among NJ H-1B employers in a fiscal year.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. Level I is a legal OFLC wage tier '
    '(historically mapped near the 17th OEWS percentile; that mapping '
    'is NOT hardcoded here). Only the extreme share tail is a lead.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'h1b_level1_min_cases',
    10,
    'Minimum CERTIFIED NJ H-1B LCAs with a non-null PW_WAGE_LEVEL for '
    'an employer to enter the Level I share ranking.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. Same cell-size rationale as '
    'h1b_cw_min_cases: rates on tiny denominators are not leads.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'h1b_secondary_tail_pctile',
    0.99,
    'CUME_DIST cutoff (top 1%) for employer_secondary_entity_share_outlier.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. SECONDARY_ENTITY = Y is the official '
    'third-party-placement attestation (ETA-9035 §F.a.2), not a violation. '
    'Only the extreme share tail is a lead.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'h1b_secondary_min_cases',
    10,
    'Minimum CERTIFIED NJ H-1B LCAs with a non-null SECONDARY_ENTITY '
    'flag for an employer to enter the secondary-entity share ranking.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. Same cell-size rationale as '
    'h1b_level1_min_cases.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


-- ----------------------------------------------------------------------------
-- 6. Refreshers
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_on_whd_willful_or_debarred(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_lookback NUMERIC := derived.f_platform_constant('h1b_willful_lookback_years');
BEGIN
    IF v_lookback IS NULL THEN
        RAISE EXCEPTION
            'employer_on_whd_willful_or_debarred: missing h1b_willful_lookback_years'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_on_whd_willful_or_debarred';

    WITH active AS (
        SELECT DISTINCT employer_canonical_name
        FROM raw.dol_whd_h1b_list
        WHERE (
            list_kind = 'debarment'
            AND (debarment_start IS NULL OR debarment_start <= CURRENT_DATE)
            AND (debarment_end   IS NULL OR debarment_end   >= CURRENT_DATE)
        )
           OR (
            list_kind = 'willful'
            AND (
                determination_date IS NULL
                OR (
                    determination_date
                    + make_interval(years => v_lookback::INT)
                ) >= CURRENT_DATE
            )
        )
    ),
    nj_emp AS (
        SELECT DISTINCT employer_canonical_name
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
        UNION
        SELECT DISTINCT employer_canonical_name
        FROM raw.uscis_h1b_employer
        WHERE fiscal_year = v_year
          AND UPPER(TRIM(petitioner_state)) = 'NJ'
    ),
    hit AS (
        SELECT a.employer_canonical_name
        FROM active a
        JOIN nj_emp n USING (employer_canonical_name)
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        h.employer_canonical_name,
        'employer_on_whd_willful_or_debarred',
        1::NUMERIC,
        5::SMALLINT,
        'kind=employer|src=whd|fy=' || p_cycle,
        1::NUMERIC,
        '/risk/employer/' || replace(h.employer_canonical_name, ' ', '%20')
            || '?signal=employer_on_whd_willful_or_debarred&cycle=' || p_cycle
    FROM hit h;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_on_whd_willful_or_debarred(CHAR) IS
    'FRAUD-V1b: NJ H-1B employer whose canonical name is on an active '
    'DOL WHD debarment or willful-violator row. Official list. 20 CFR '
    '655.736 / 655.750(d). Idempotent DELETE+INSERT.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_level1_wage_share_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_tail     NUMERIC := derived.f_platform_constant('h1b_level1_tail_pctile');
    v_min      NUMERIC := derived.f_platform_constant('h1b_level1_min_cases');
BEGIN
    IF v_tail IS NULL OR v_min IS NULL THEN
        RAISE EXCEPTION
            'employer_level1_wage_share_outlier: missing platform_constants'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_level1_wage_share_outlier';

    WITH emp AS (
        SELECT
            employer_canonical_name,
            COUNT(*) FILTER (WHERE pw_wage_level IS NOT NULL) AS n_leveled,
            COUNT(*) FILTER (WHERE pw_wage_level = 'I')       AS n_level1
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status = 'CERTIFIED'
        GROUP BY 1
    ),
    ranked AS (
        SELECT
            employer_canonical_name,
            n_level1::NUMERIC / n_leveled AS level1_share,
            CUME_DIST() OVER (
                ORDER BY n_level1::NUMERIC / n_leveled
            ) AS pctile
        FROM emp
        WHERE n_leveled >= v_min
          AND n_leveled > 0
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        r.employer_canonical_name,
        'employer_level1_wage_share_outlier',
        r.level1_share,
        3::SMALLINT,
        'kind=employer|visa=H-1B|pw_level|fy=' || p_cycle,
        r.pctile,
        '/risk/employer/' || replace(r.employer_canonical_name, ' ', '%20')
            || '?signal=employer_level1_wage_share_outlier&cycle=' || p_cycle
    FROM ranked r
    WHERE r.pctile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_level1_wage_share_outlier(CHAR) IS
    'FRAUD-V1b: NJ H-1B employers in the top tail of CERTIFIED LCA share '
    'filed at PW_WAGE_LEVEL = I. Level I is a legal tier; the tail is '
    'the lead. Empirical. Returns 0 when attestation columns are empty.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_secondary_entity_share_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_tail     NUMERIC := derived.f_platform_constant('h1b_secondary_tail_pctile');
    v_min      NUMERIC := derived.f_platform_constant('h1b_secondary_min_cases');
BEGIN
    IF v_tail IS NULL OR v_min IS NULL THEN
        RAISE EXCEPTION
            'employer_secondary_entity_share_outlier: missing platform_constants'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_secondary_entity_share_outlier';

    WITH emp AS (
        SELECT
            employer_canonical_name,
            COUNT(*) FILTER (WHERE secondary_entity IS NOT NULL) AS n_flagged,
            COUNT(*) FILTER (WHERE secondary_entity = 'Y')       AS n_secondary
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status = 'CERTIFIED'
        GROUP BY 1
    ),
    ranked AS (
        SELECT
            employer_canonical_name,
            n_secondary::NUMERIC / n_flagged AS secondary_share,
            CUME_DIST() OVER (
                ORDER BY n_secondary::NUMERIC / n_flagged
            ) AS pctile
        FROM emp
        WHERE n_flagged >= v_min
          AND n_flagged > 0
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        r.employer_canonical_name,
        'employer_secondary_entity_share_outlier',
        r.secondary_share,
        3::SMALLINT,
        'kind=employer|visa=H-1B|secondary_entity|fy=' || p_cycle,
        r.pctile,
        '/risk/employer/' || replace(r.employer_canonical_name, ' ', '%20')
            || '?signal=employer_secondary_entity_share_outlier&cycle=' || p_cycle
    FROM ranked r
    WHERE r.pctile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_secondary_entity_share_outlier(CHAR) IS
    'FRAUD-V1b: NJ H-1B employers in the top tail of CERTIFIED LCA share '
    'with SECONDARY_ENTITY = Y (third-party placement). Empirical.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_h1b_dependent_plus_anomaly(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT := CAST(p_cycle AS INT);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_h1b_dependent_plus_anomaly';

    WITH dependent AS (
        SELECT DISTINCT employer_canonical_name
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year   = v_year
          AND case_status   = 'CERTIFIED'
          AND h1b_dependent = 'Y'
    ),
    corroborating AS (
        SELECT
            entity_id,
            COUNT(*)::INT AS n_anomalies
        FROM derived.fraud_signal_observation
        WHERE cycle = p_cycle
          AND entity_kind = 'employer'
          AND signal_id IN (
                'employer_below_prevailing_wage',
                'employer_lca_uscis_volume_gap',
                'employer_level1_wage_share_outlier',
                'employer_secondary_entity_share_outlier'
          )
        GROUP BY 1
    ),
    hit AS (
        SELECT
            d.employer_canonical_name,
            c.n_anomalies
        FROM dependent d
        JOIN corroborating c
          ON c.entity_id = d.employer_canonical_name
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        h.employer_canonical_name,
        'employer_h1b_dependent_plus_anomaly',
        h.n_anomalies,
        4::SMALLINT,
        'kind=employer|h1b_dependent|fy=' || p_cycle,
        1::NUMERIC,
        '/risk/employer/' || replace(h.employer_canonical_name, ' ', '%20')
            || '?signal=employer_h1b_dependent_plus_anomaly&cycle=' || p_cycle
    FROM hit h;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_h1b_dependent_plus_anomaly(CHAR) IS
    'FRAUD-V1b: H-1B_DEPENDENT = Y AND at least one corroborating H-1B '
    'anomaly (below-PW, volume-gap, Level I tail, secondary-entity tail). '
    'Must run after those four refreshers. 20 CFR 655.736 bucket.';


-- ----------------------------------------------------------------------------
-- 7. fraud_signal_config
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES
(
    'employer_on_whd_willful_or_debarred',
    'h1b_enforcement',
    1,
    'NJ H-1B employer on an active DOL WHD debarment or willful-violator '
    'row (canonical-name join). Official list. raw_value = 1. Severity 5.'
),
(
    'employer_level1_wage_share_outlier',
    'h1b_wage',
    0,
    'NJ H-1B employer in the top 1% of CERTIFIED LCA share at '
    'PW_WAGE_LEVEL = I. Empirical. raw_value = share. Severity 3.'
),
(
    'employer_secondary_entity_share_outlier',
    'h1b_wage',
    0,
    'NJ H-1B employer in the top 1% of CERTIFIED LCA share with '
    'SECONDARY_ENTITY = Y. Empirical. raw_value = share. Severity 3.'
),
(
    'employer_h1b_dependent_plus_anomaly',
    'h1b_cross_source',
    1,
    'H-1B_DEPENDENT = Y plus a corroborating wage / volume / placement '
    'anomaly. raw_value = count of corroborating signals. Severity 4.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 8. Employer lead queue: + four new signals
-- DROP first: CREATE OR REPLACE cannot rename/reorder view columns, and
-- the new metrics are inserted before preview_signal_id.
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS derived.v_h1b_employer_leads;
CREATE VIEW derived.v_h1b_employer_leads AS
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
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_on_whd_willful_or_debarred'
    )                                                            AS on_whd_list,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_level1_wage_share_outlier'
    )                                                            AS level1_wage_share,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_secondary_entity_share_outlier'
    )                                                            AS secondary_entity_share,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_h1b_dependent_plus_anomaly'
    )                                                            AS dependent_anomaly_count,
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
        'employer_certified_withdrawn_rate_outlier',
        'employer_on_whd_willful_or_debarred',
        'employer_level1_wage_share_outlier',
        'employer_secondary_entity_share_outlier',
        'employer_h1b_dependent_plus_anomaly'
  )
GROUP BY o.cycle, o.entity_id;

COMMENT ON VIEW derived.v_h1b_employer_leads IS
    'One row per (cycle, employer) with stacked H-1B FRAUD-V1 + V1b '
    'signals. Read surface for /h1b. formula '
    '3.8.0-fraud-h1b-attestation-enforcement-v1.';


-- ----------------------------------------------------------------------------
-- 9. Master refresher: TIER 11; 32 -> 36
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

    -- TIER 11: H-1B employer visa-fraud leads (8 -- mig 121 + 122)
    SELECT derived.refresh_signal_employer_below_prevailing_wage(p_cycle)   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_h1b_denial_rate_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_lca_uscis_volume_gap(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_certified_withdrawn_rate_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_on_whd_willful_or_debarred(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_level1_wage_share_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_secondary_entity_share_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    -- Compound: must follow the four corroborating H-1B refreshers.
    SELECT derived.refresh_signal_employer_h1b_dependent_plus_anomaly(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
    'Master fraud-signal refresher. Invokes all 36 seeded signal refreshers '
    'in substrate-dependency tier order. Mig 122 raises 32 -> 36 by adding '
    'four H-1B attestation / WHD-enforcement leads in TIER 11. Empty '
    'substrate returns 0.';


COMMIT;
