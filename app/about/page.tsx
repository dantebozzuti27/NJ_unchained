export const metadata = {
  title: "Methodology — NJ Unchained",
};

export default function AboutPage() {
  return (
    <article className="prose prose-zinc dark:prose-invert max-w-none">
      <h1>Methodology</h1>

      <p className="lead">
        NJ Unchained is a two-pillar public-interest screener for New
        Jersey: <strong>housing affordability</strong> (county-level
        burden divergence) and <strong>civic integrity</strong>{" "}
        (cross-source risk evidence on political and federal-procurement
        entities). Both pillars share one Postgres substrate; both
        publish their underlying data and computations.
      </p>

      <h2>Pillar 1 — Housing affordability</h2>
      <p>
        For each NJ county we compute a <strong>burden ratio</strong>:
        the FHFA House Price Index growth divided by ACS 5-year median
        household income growth, both deflated to constant dollars via
        CPI-U All Items and re-indexed so that 2010 = 100 (or 1.0 in
        ratio form).
      </p>
      <pre>
{`burden_ratio(county, year) =
    [HPI(county, year) / HPI(county, 2010)]
  ÷ [real_income(county, year) / real_income(county, 2010)]`}
      </pre>
      <p>
        A ratio of 1.40 means home-price growth has outpaced real wage
        growth by 40% since the base year — i.e. a household needs ~40%
        more inflation-adjusted income to afford the same house. Tier
        bands: <strong>STRESS</strong> (≥ 1.40), <strong>ELEVATED</strong>{" "}
        (≥ 1.15), <strong>TRACKING</strong> (≥ 0.95), <strong>LAGGING</strong>{" "}
        (&lt; 0.95).
      </p>
      <p>
        This metric deliberately ignores interest-rate effects on
        monthly mortgage payments and tenure-segmented (renter vs owner)
        cost-burden share. Those richer dimensions live in the backend{" "}
        <code>derived.housing_burden_ratio</code> view, materialized
        from ACS PUMS.
      </p>

      <h2>Pillar 2 — Civic integrity: how the risk score is computed</h2>
      <p>
        We surface a 0–100 <strong>risk score</strong> per entity —
        candidate, committee, treasurer, donor, contractor, or address
        cluster — within a federal election cycle. The number is a{" "}
        <em>percentile of anomalousness</em> within an entity&apos;s
        peer group. It is <strong>not</strong> a probability of fraud.
      </p>

      <h2>Five layers</h2>
      <ol>
        <li>
          <strong>L1 — signal observations</strong>:{" "}
          <code>derived.fraud_signal_observation</code>. One row per
          (cycle, entity_kind, entity_id, signal_id) where a signal
          fires. Severity 1–5; raw_value in dollars or count; per-bucket
          peer_percentile.
        </li>
        <li>
          <strong>L2 — entity feature pivot</strong>:{" "}
          <code>derived.v_entity_fraud_features</code>. One row per
          (cycle, entity). Aggregates L1 signals after applying each
          signal&apos;s <code>min_actionable_threshold</code> from{" "}
          <code>derived.fraud_signal_config</code>.
        </li>
        <li>
          <strong>L3a — entity risk score</strong>:{" "}
          <code>derived.v_entity_fraud_risk</code>. Composite 0–100 from{" "}
          <code>derived.fraud_risk_score(severities, percentiles, families)</code>.
          Tail-only: only signals with peer_percentile ≥ 0.95
          contribute. Multi-family diversity bonus rewards corroboration
          across distinct signal families.
        </li>
        <li>
          <strong>L4 — evidence panel</strong>: per-signal{" "}
          <code>evidence_url</code>, links back to source data.
        </li>
        <li>
          <strong>L5 — analyst feedback / labels</strong>: deferred
          until the platform has analyst-confirmed labels at scale.
        </li>
      </ol>

      <h2>17 signals across 5 families</h2>
      <ul>
        <li>
          <strong>leie_bearing</strong> (4 signals): canonical-name
          matches against the HHS-OIG List of Excluded Individuals and
          Entities. Severity 5 (CRITICAL) on every match.
        </li>
        <li>
          <strong>sam_bearing</strong> (3 signals): UEI-deterministic
          and canonical-name matches against the SAM.gov Exclusions
          extract. Broader than LEIE — covers DOJ, OFAC, GSA, NIH/NSF,
          DOE.
        </li>
        <li>
          <strong>workforce</strong> (2 signals): donor employer
          overlaps with NJ federal-contractor population, plus the
          candidate-side projection.
        </li>
        <li>
          <strong>address</strong> (1 signal): committee address
          clustering.
        </li>
        <li>
          <strong>structural</strong> (7 signals): committee /
          candidate / treasurer FEC-structural anomalies (no PCC,
          broken PCC, multiple PCCs, name collisions, namesakes,
          treasurer = candidate, treasurer concentration).
        </li>
      </ul>

      <h2>Risk score formula</h2>
      <p>
        For each entity, the L3a score is computed as:
      </p>
      <pre>
{`score = 100 * (
  Σ over signals s where p_s >= 0.95:
    sev_s * (p_s - 0.95)^2
  + 0.01 * max(0, n_contributing_families - 1)^2
)`}
      </pre>
      <p>
        The tail-only quadratic penalty means a signal at the 95th
        percentile contributes ~0; a signal at the 99th contributes
        sharply. The diversity bonus rewards multi-family corroboration
        — an entity flagged by both LEIE and SAM signals scores
        meaningfully higher than an equally-tailed single-family entity,
        because cross-source agreement is harder to fake than a single
        false positive.
      </p>

      <h2>Honest framing</h2>
      <p>
        A real <code>P(fraud | features)</code> field appears only after
        the L5 triage queue produces enough analyst-confirmed labels to
        fit and calibrate (isotonic) a held-out classifier. Until then
        the surface is <code>risk_score</code>, not <code>p_fraud</code>.
      </p>
      <p>
        Substrate-honesty is a hard rule: the L1 layer mirrors source
        data faithfully without any inference. All filtering, decay,
        and percentile-normalization happen at L2 / L3a so the analyst
        can always reconstruct the path from a flagged entity back to
        the underlying federal records.
      </p>

      <h2>Data sources</h2>
      <ul>
        <li>
          FEC bulk data (candidates, committees, contributions) —
          public, vintage tracked per cycle
        </li>
        <li>USAspending.gov NJ-recipient awards (federal contracts)</li>
        <li>HHS-OIG LEIE (federal exclusions, monthly full-replace)</li>
        <li>SAM.gov Exclusions extract</li>
        <li>FHFA HPI county series</li>
        <li>ACS income / housing-burden microdata (PUMS)</li>
        <li>
          DOL OFLC LCA disclosures (H-1B statutory wages), USCIS H-1B
          Employer Data Hub (first-decision approvals/denials), and DOL
          WHD H-1B debarment / willful-violator lists
        </li>
      </ul>
    </article>
  );
}
