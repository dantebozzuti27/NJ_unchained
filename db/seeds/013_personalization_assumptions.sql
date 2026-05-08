-- ============================================================================
-- Seed: 013_personalization_assumptions
--
-- Phase-4 personalization-engine constants. Adds to the tier-A registry
-- established in seed 012; rolled as a new seed (rather than editing 012)
-- so the drift-detector can verify 012 has not changed since deployment.
--
-- Idempotent under re-run via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.affordability_assumptions
    (constant_id, effective_year, value_numeric, unit,
     source_url, source_citation, note)
VALUES

    -- ------------------------------------------------------------------
    -- Per-town verdict band (Phase 4: derived.f_user_town_verdict).
    --
    -- HUD's affordability-outreach materials and Fannie Mae's
    -- borrower-education materials both describe homes priced
    -- 0%-25% over the borrower's max-affordable as "stretch"
    -- (achievable with budget tradeoffs / higher down / longer term)
    -- and homes >25% over as "out of reach" (the budget gap is too
    -- large to bridge without a fundamental change in income or
    -- target). 1.25x is the canonical stretch multiplier.
    -- ------------------------------------------------------------------
    ('affordability_stretch_multiplier', 0, 1.25000000, 'fraction',
     'https://www.huduser.gov/portal/sites/default/files/pdf/HUD-Housing-Affordability-Glossary.pdf',
     'HUD outreach materials: "stretch home" = up to 25% over budget; "out of reach" beyond that',
     'Per-town verdict bands in derived.f_user_town_verdict: median_home <= max_affordable = affordable; max < median <= 1.25 * max = stretch; > 1.25 * max = out_of_reach. The /personalize page accepts a user override so the user can compress or relax the band per their risk appetite.')

ON CONFLICT (constant_id, effective_year) DO UPDATE SET
    value_numeric   = EXCLUDED.value_numeric,
    unit            = EXCLUDED.unit,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation,
    note            = EXCLUDED.note;
