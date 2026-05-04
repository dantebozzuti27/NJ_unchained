-- ============================================================================
-- Migration: 050_fraud_signal_observation
--
-- TIER 4 v3 STEP 1: long-format signal observation store + entity feature
-- pivot. The contract that L3 (scoring), L4 (evidence panel), and L5 (triage
-- queue) build on. Purely additive: every v2.A view in migration 040 stays
-- intact; an adapter in a later commit re-emits those views as rows in
-- this table without changing them.
--
-- WHY THIS LAYER EXISTS
-- ---------------------
-- v2.A is a CATALOG of eight per-signal flat lists. There is no entity-
-- level synthesis: a committee that fires three signals at the 99th
-- percentile is buried in three separate tabs. The user reviewed v2.A
-- and asked for an engine that "surfaces potential fraud, gives a %
-- prediction, and backs it up with data." The honest version of that
-- ask is:
--
--   (1) Pivot the analytical unit from per-signal -> per-entity. The
--       feature vector for one (cycle, entity) is the unit that scoring
--       and triage operate on.
--   (2) Score = composite of fired signals, weighted by analyst-curated
--       severity and tail-only quantile contribution. NOT a probability
--       of fraud; we have no labels at scale.
--   (3) Every score is backed by row-level evidence (which signals
--       fired, the underlying raw rows, the peer-bucket context).
--
-- This migration ships the storage + view contract for (1) and (3).
-- The scoring function (2) and the API/UI/CLI surfaces follow in
-- migrations 051+ and the matching Python work, gated on the open
-- design decisions still pinned at the top of work_left.txt.
--
-- DESIGN CHOICES (substrate-honesty)
-- ----------------------------------
-- 1. LONG FORMAT, NOT WIDE. One row per (cycle, entity, signal) instead
--    of per-entity with fixed signal columns. Adding a Tier-B signal is
--    insert-only -- no DDL migration, no schema rev, no client refactor.
--    The wide pivot for L3 consumption is a VIEW on top, recomputed
--    cheaply (the input is bounded by entity * signal cardinality, not
--    by the underlying raw.fec_* row counts).
--
-- 2. peer_percentile, NOT raw_value, IS WHAT L3 CONSUMES. Senators have
--    larger committees than House challengers; their address clusters
--    are larger; their treasurer rosters span more states. Without
--    peer-group normalization a senator looks "fraudulent" because
--    they are big. peer_bucket encodes the bucket the percentile is
--    computed within (e.g., 'office=H|state=NJ|incumbent=challenger').
--    L1 emits the percentile already; L3 never re-bins.
--
-- 3. severity IS ANALYST-CURATED ([1, 5] ORDINAL), NOT LEARNED. A self-
--    treasurer flag is qualitatively weaker evidence than a transfer
--    cycle. Until the L5 triage queue produces labels, encoding domain
--    priors as a fixed ordinal beats fitting weights to noise. When
--    labels exist (migration 053+) these become Bayesian priors that
--    get updated, not magic numbers.
--
-- 4. entity_kind IS A WHITELIST. Five values: committee, candidate,
--    treasurer, address, donor_cluster. Treasurer + address are
--    pre-declared even though they are not first-class entities yet
--    (no canonical treasurer table; address canonicalization lives
--    inside migration 040 views). Pre-declaring them in the CHECK
--    avoids a future ALTER TABLE when those promotions land. The
--    v2.A adapter only emits 'committee' and 'candidate' rows
--    initially; treasurer/address rows arrive when their canonical
--    tables do.
--
-- 5. evidence_url IS A REQUIRED COLUMN, NOT AN OPTIONAL ANNOTATION.
--    Every observation row points at the API path that will render
--    the evidence ("back it up with data"). Missing evidence is a
--    bug, not a soft-warn. NOT NULL + NOT empty.
--
-- 6. NO FOREIGN KEYS TO raw.fec_*. entity_id is opaque text: it can
--    be a cmte_id, a cand_id, a treasurer canonical-name hash, an
--    address canonical-form hash. Pinning to one foreign key would
--    force the table to be polymorphic-typed and either lose
--    referential integrity or fragment per kind. Schema-internal
--    consistency is enforced by the materializer in Python, not by
--    Postgres. (Same convention used by governance.dataset_health.)
--
-- 7. PARTITION BY (cycle) is DEFERRED. With v2.A's 8 signals over a
--    single 2024 cycle the maximum row count is bounded by entity
--    cardinality (~30K committees + candidates), so the table is
--    O(100K) rows max. Partitioning helps once cross-cycle data
--    lands; revisit when 2020 + 2022 are loaded.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- L1: derived.fraud_signal_observation
-- ----------------------------------------------------------------------------
-- One row per (cycle, entity, signal). A signal that does not fire on an
-- entity is ABSENT from this table -- not present with severity=0. This
-- preserves "n_signals_fired" semantics on the L2 pivot below without
-- needing to filter on a magic value.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS derived.fraud_signal_observation (
    cycle           CHAR(4)        NOT NULL
        CHECK (cycle ~ '^[0-9]{4}$'),

    entity_kind     TEXT           NOT NULL
        CHECK (entity_kind IN (
            'committee', 'candidate', 'treasurer', 'address', 'donor_cluster'
        )),

    entity_id       TEXT           NOT NULL
        CHECK (entity_id <> ''),

    signal_id       TEXT           NOT NULL
        CHECK (signal_id <> ''),

    raw_value       NUMERIC,

    severity        SMALLINT       NOT NULL
        CHECK (severity BETWEEN 1 AND 5),

    peer_bucket     TEXT           NOT NULL
        CHECK (peer_bucket <> ''),

    peer_percentile NUMERIC        NOT NULL
        CHECK (peer_percentile >= 0 AND peer_percentile <= 1),

    evidence_url    TEXT           NOT NULL
        CHECK (evidence_url <> ''),

    materialized_at TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (cycle, entity_kind, entity_id, signal_id)
);

COMMENT ON TABLE derived.fraud_signal_observation IS
'TIER 4 v3 L1: long-format signal-observation store. One row per '
'(cycle, entity, signal) where the signal fired for that entity. '
'Absent rows mean the signal did not fire (NOT severity=0). The L2 '
'pivot view + L3 scoring engine read this table; the L4 evidence '
'panel deep-links via evidence_url. peer_percentile is computed in '
'the materializer at L1 write-time and never re-binned downstream.';

COMMENT ON COLUMN derived.fraud_signal_observation.peer_bucket IS
'Pipe-delimited bucket key (e.g. office=H|state=NJ|incumbent=challenger) '
'within which peer_percentile was computed. Bucket granularity is a '
'materializer choice and may differ across signals; the L2 pivot '
'preserves the per-signal bucket so the evidence panel can show '
'analysts what comparison group each percentile is against.';

COMMENT ON COLUMN derived.fraud_signal_observation.peer_percentile IS
'Empirical CDF of raw_value within peer_bucket, in [0, 1]. 0.99 means '
'this entitys raw_value is at or above 99% of peers in the bucket. '
'L3a uses tail-only contribution: contributions below 0.95 do not '
'affect the score.';

COMMENT ON COLUMN derived.fraud_signal_observation.severity IS
'Analyst-curated ordinal in [1, 5]. 1 = weak / many false positives '
'(e.g. self-treasurer), 5 = strong evidence in isolation (e.g. '
'transfer-cycle in receipts graph). NOT learned. Becomes a Bayesian '
'prior under the L3c calibrated classifier once L5 labels exist.';

COMMENT ON COLUMN derived.fraud_signal_observation.evidence_url IS
'Path (relative to the API base) that renders the underlying evidence '
'rows. Required and non-empty. The evidence panel deep-links here so '
'every score component traces back to raw rows.';


-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------
-- Three access patterns this table must serve cheaply:
--
--   A. Per-entity drill (evidence panel):
--      "give me every signal that fired for this (entity_kind, entity_id)"
--      -> covered by the PK (cycle, entity_kind, entity_id, *).
--
--   B. Per-signal top-N within a cycle (the existing v2.A flat lists,
--      reframed):
--      "give me the top 100 entities for signal_id = X in cycle = Y
--       sorted by peer_percentile DESC"
--      -> needs an index on (cycle, signal_id, peer_percentile DESC).
--
--   C. Per-bucket population (peer-group context for the panel):
--      "how many entities are in this peer_bucket for this cycle, and
--       what is their score distribution"
--      -> needs an index on (cycle, peer_bucket).
--
-- We create B and C as separate btree indexes; A is free from the PK.
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS fraud_signal_observation_signal_pct_idx
    ON derived.fraud_signal_observation (cycle, signal_id, peer_percentile DESC);

CREATE INDEX IF NOT EXISTS fraud_signal_observation_bucket_idx
    ON derived.fraud_signal_observation (cycle, peer_bucket);


-- ----------------------------------------------------------------------------
-- L2: derived.v_entity_fraud_features
-- ----------------------------------------------------------------------------
-- Wide pivot keyed on (cycle, entity_kind, entity_id). One row per entity;
-- arrays preserve per-signal detail in stable signal_id order so the
-- evidence panel can render the score decomposition without a second
-- query.
--
-- Critical property: the score function is a deterministic function of
-- (severities, peer_percentiles), so scores can be recomputed entirely
-- from this view -- L1 re-aggregation never has to be replayed when only
-- the scoring formula changes. That is why L1 is materialized as a
-- table and L2 is a view, not the other way around.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_entity_fraud_features AS
SELECT
    cycle,
    entity_kind,
    entity_id,

    COUNT(*)::INT                                          AS n_signals_fired,
    MAX(severity)::SMALLINT                                AS max_severity,
    MAX(peer_percentile)                                   AS max_peer_percentile,
    AVG(peer_percentile)                                   AS avg_peer_percentile,

    -- The "primary" peer_bucket is a UI convenience: when an entity
    -- fires multiple signals with different bucket dimensions we display
    -- the bucket of the highest-severity, then highest-percentile,
    -- signal as the headline comparison group. Per-signal buckets are
    -- still preserved in the peer_buckets array for full transparency.
    (ARRAY_AGG(peer_bucket
               ORDER BY severity DESC, peer_percentile DESC, signal_id))[1]
        AS primary_peer_bucket,

    -- Parallel arrays in stable signal_id order. The L3 scoring engine
    -- consumes (severities, peer_percentiles); the evidence panel
    -- consumes the rest for the score-decomposition strip.
    ARRAY_AGG(signal_id        ORDER BY signal_id)         AS signals_fired,
    ARRAY_AGG(severity         ORDER BY signal_id)         AS severities,
    ARRAY_AGG(peer_percentile  ORDER BY signal_id)         AS peer_percentiles,
    ARRAY_AGG(peer_bucket      ORDER BY signal_id)         AS peer_buckets,
    ARRAY_AGG(raw_value        ORDER BY signal_id)         AS raw_values,
    ARRAY_AGG(evidence_url     ORDER BY signal_id)         AS evidence_urls,

    MAX(materialized_at)                                   AS last_observation_at
FROM   derived.fraud_signal_observation
GROUP  BY cycle, entity_kind, entity_id;

COMMENT ON VIEW derived.v_entity_fraud_features IS
'TIER 4 v3 L2: per-entity wide pivot of L1 observations. One row per '
'(cycle, entity_kind, entity_id) with the entitys fired-signal feature '
'vector in parallel arrays sorted by signal_id. The L3 scoring function '
'is a deterministic transform of (severities, peer_percentiles) on this '
'view, so changing the scoring formula does not require L1 re-aggregation.';
