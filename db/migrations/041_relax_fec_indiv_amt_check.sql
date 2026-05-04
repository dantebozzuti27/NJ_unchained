-- ============================================================================
-- Migration: 041_relax_fec_indiv_amt_check
--
-- Migration 038 set raw.fec_contribution.transaction_amt to a CHECK
-- of [-$10M, $10M]. That cap was conservative; the 2024 cycle has
-- multiple legitimate contributions exceeding it, including (but
-- not limited to) Miriam Adelson's $25M to Future Forward (8/13/24)
-- which is the largest individual political contribution recorded
-- by the FEC. Rejecting it at COPY time violates substrate honesty:
-- raw.* must mirror what the FEC published. Anomaly detection lives
-- in derived.*, not in CHECK constraints.
--
-- New cap: $1B per row. This is wide enough to absorb any legal
-- (or illegal-but-reported) single-row amount the FEC has ever
-- accepted, while still rejecting obviously-malformed values
-- (e.g., a stray "999999999999" digit overflow).
--
-- The width-14 NUMERIC type already constrains values to ten digits
-- of dollars (max ~9.999e9), so $1B is well within the type bounds.
--
-- This migration is idempotent: dropping a constraint that does not
-- exist is a no-op (we use IF EXISTS).
-- ============================================================================

ALTER TABLE raw.fec_contribution
    DROP CONSTRAINT IF EXISTS fec_contribution_transaction_amt_check;

ALTER TABLE raw.fec_contribution
    ADD  CONSTRAINT fec_contribution_transaction_amt_check
    CHECK (
        transaction_amt IS NULL
        OR transaction_amt BETWEEN -1000000000 AND 1000000000
    );

COMMENT ON CONSTRAINT fec_contribution_transaction_amt_check
    ON raw.fec_contribution IS
'Sanity bound on single-row contribution amount; intentionally wide '
'($1B) to faithfully accept all FEC-published values (incl. legitimate '
'mega-donations like the Adelson $25M of 2024). Anomaly detection '
'happens in derived.* views, not at the raw substrate.';
