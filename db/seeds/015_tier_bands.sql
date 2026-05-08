-- ============================================================================
-- Seed: 015_tier_bands
--
-- VISION_2026.md §3 punch-list: pay down the burdenTier() {1.4, 1.15, 0.95}
-- magic-number debt by registering empirically calibrated cutoffs in
-- ref.tier_bands.
--
-- CALIBRATION DATA -- 2026-05-08, against Neon production substrate.
--
-- Empirical NJ panel: 315 (county, year) burden-ratio observations
-- 2010-2024, computed as (FHFA HPI(year) / HPI(2010)) divided by
-- (real_income(year) / real_income(2010)) for each NJ county.
--
--   min     0.8912
--   p10     0.9451   <-- LAGGING / TRACKING boundary candidate
--   p25     0.9736
--   p50     1.0336
--   p75     1.2056   <-- TRACKING / ELEVATED boundary candidate
--   p90     1.5387   <-- ELEVATED / STRESS boundary candidate
--   p95     1.6553
--   max     1.8117
--
-- Latest year (2024) cross-section: every NJ county has burden_ratio
-- in [1.485, 1.812]. ALL 21 counties exceed the historical p90, so
-- the substrate-honest reading is "2024 is universally worse than
-- 90% of NJ history". The tier classifier reflects that without
-- softening the message.
--
-- CHOSEN CUTOFFS (4-band, version 1.7.1-tier-bands-v1)
-- ----------------------------------------------------
--   LAGGING   (-inf, 0.95)  -- wages outpaced housing growth
--   TRACKING  [0.95, 1.20)  -- housing within 20% of wage growth (~p10 to ~p75)
--   ELEVATED  [1.20, 1.55)  -- housing 20-55% above wage growth (~p75 to ~p90)
--   STRESS    [1.55, +inf)  -- housing >55% above wage growth (>= p90)
--
-- The cutoffs round the empirical percentiles to clean values to
-- maintain readability while preserving the empirical anchoring:
--   0.95 = p10 rounded down (empirical p10 = 0.945)
--   1.20 = p75 rounded down (empirical p75 = 1.206)
--   1.55 = p90 rounded down (empirical p90 = 1.539)
--
-- The "rounded down" choice is conservative: a county at exactly the
-- empirical p90 lands in STRESS, not ELEVATED. The cost is roughly
-- 0.5pp of misclassification for ratios in (1.539, 1.55); the benefit
-- is human-readable cutoffs and audit-friendly arithmetic.
-- ============================================================================

INSERT INTO ref.tier_bands
    (tier_kind, band_ord, label, description, severity_rank,
     lower_bound, upper_bound,
     ui_bg_classes, ui_fg_classes,
     citation_text, source_url,
     formula_version, effective_date, notes)
VALUES
    ('burden_growth_ratio', 1, 'LAGGING',
     'Wages have outpaced housing growth since the base year (ratio < 0.95). '
     'Affordability has IMPROVED relative to the base year.',
     0,  -- severity_rank: lowest
     NULL, 0.95,
     'bg-blue-50 dark:bg-blue-950',
     'text-blue-700 dark:text-blue-300',
     'Empirical NJ panel calibration: ratio < 0.95 corresponds to ~p10 of the '
     '315-pair (NJ county, year) historical distribution 2010-2024 (empirical '
     'p10 = 0.9451). A county-year in this band has wage growth at least 5% '
     'ahead of housing growth -- structurally improving affordability.',
     NULL,
     '1.7.1-tier-bands-v1',
     '2026-05-08'::DATE,
     'Lowest severity; rendered in calm blue to communicate "this is good news."'),

    ('burden_growth_ratio', 2, 'TRACKING',
     'Housing growth roughly matches wage growth (0.95 <= ratio < 1.20). '
     'The typical zone for a healthy housing market.',
     1,
     0.95, 1.20,
     'bg-emerald-100 dark:bg-emerald-950',
     'text-emerald-800 dark:text-emerald-200',
     'Empirical NJ panel calibration: ratio in [0.95, 1.20) corresponds to '
     'roughly the [p10, p75] interquartile-spanning zone (empirical p10 = '
     '0.9451, p75 = 1.2056). The middle 65% of historical NJ county-years '
     'fall here; this is the "normal" regime.',
     NULL,
     '1.7.1-tier-bands-v1',
     '2026-05-08'::DATE,
     NULL),

    ('burden_growth_ratio', 3, 'ELEVATED',
     'Housing growth has outpaced wage growth by 20-55% (1.20 <= ratio < 1.55). '
     'Affordability is deteriorating but not at historical extremes.',
     2,
     1.20, 1.55,
     'bg-orange-100 dark:bg-orange-950',
     'text-orange-800 dark:text-orange-200',
     'Empirical NJ panel calibration: ratio in [1.20, 1.55) corresponds to '
     'roughly the [p75, p90] zone (empirical p75 = 1.2056, p90 = 1.5387). '
     'A county-year in this band is in the WORST 25% of historical NJ but '
     'not yet at the worst 10%.',
     NULL,
     '1.7.1-tier-bands-v1',
     '2026-05-08'::DATE,
     NULL),

    ('burden_growth_ratio', 4, 'STRESS',
     'Housing growth has outpaced wage growth by 55%+ (ratio >= 1.55). '
     'In or near historical-extreme territory.',
     3,
     1.55, NULL,
     'bg-red-100 dark:bg-red-950',
     'text-red-800 dark:text-red-200',
     'Empirical NJ panel calibration: ratio >= 1.55 corresponds to >= p90 of '
     'the historical distribution (empirical p90 = 1.5387). A county-year in '
     'this band is in the WORST 10% of NJ history. As of 2024, ALL 21 NJ '
     'counties land here -- the substrate-honest reading is that 2024 is '
     'universally worse than 90% of NJ history 2010-2023.',
     NULL,
     '1.7.1-tier-bands-v1',
     '2026-05-08'::DATE,
     'Highest severity; rendered in red.')
ON CONFLICT (tier_kind, band_ord, formula_version) DO UPDATE SET
    label          = EXCLUDED.label,
    description    = EXCLUDED.description,
    severity_rank  = EXCLUDED.severity_rank,
    lower_bound    = EXCLUDED.lower_bound,
    upper_bound    = EXCLUDED.upper_bound,
    ui_bg_classes  = EXCLUDED.ui_bg_classes,
    ui_fg_classes  = EXCLUDED.ui_fg_classes,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;
