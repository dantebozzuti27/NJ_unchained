"""One-shot generator: extract the UNION of NJ municipality reference rows
across every on-disk DCA workbook (2016-2024) and emit
``db/seeds/040_nj_municipality.sql``.

Why this exists:
    NJ has 564 incorporated municipalities in 2024 (post-2013 Princeton
    merger), but the historical DCA workbooks back to 2016 reference
    additional muni_code values that have since been retired (mergers,
    consolidations) -- and the platform's raw.nj_property_tax_muni FK
    requires every code seen in the raw layer to exist in the ref
    dimension. Hand-transcribing 564+ triples across 9 vintages is
    error-prone; the canonical machine-readable sources are the
    workbooks themselves.

    This script reads EVERY workbook in
    ``data/manual/nj_dca_property_tax/*.xls`` (16-24 by default),
    extracts rows whose 4-digit MuniCode does NOT end in '00', UNIONs
    them by (muni_code, county_fips), and uses the most-recent
    observation of muni_name (since names sometimes change as part of
    incorporation events). Output: a single deterministic, idempotent
    seed file.

    The OUTPUT (seeds/040) is the source of record. This script is a
    one-shot helper, not a runtime dependency. Re-run only when a new
    DCA workbook lands.

Usage:
    python -m scripts._generate_phase8_muni_seed > db/seeds/040_nj_municipality.sql
"""
# ruff: noqa: E501

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
WORKBOOK_DIR = ROOT / "data" / "manual" / "nj_dca_property_tax"

DCA_TO_FIPS: dict[str, str] = {
    "01": "34001",  # Atlantic
    "02": "34003",  # Bergen
    "03": "34005",  # Burlington
    "04": "34007",  # Camden
    "05": "34009",  # Cape May
    "06": "34011",  # Cumberland
    "07": "34013",  # Essex
    "08": "34015",  # Gloucester
    "09": "34017",  # Hudson
    "10": "34019",  # Hunterdon
    "11": "34021",  # Mercer
    "12": "34023",  # Middlesex
    "13": "34025",  # Monmouth
    "14": "34027",  # Morris
    "15": "34029",  # Ocean
    "16": "34031",  # Passaic
    "17": "34033",  # Salem
    "18": "34035",  # Somerset
    "19": "34037",  # Sussex
    "20": "34039",  # Union
    "21": "34041",  # Warren
}


def _extract_munis_from_workbook(path: Path) -> list[tuple[str, str]]:
    """Return [(muni_code, muni_name)] from one DCA workbook's
    'Municipal Tax Summary' sheet; filtered to real munis only."""
    raw = pl.read_excel(
        path, engine="calamine",
        sheet_name="Municipal Tax Summary",
        infer_schema_length=0,
    )
    if raw.height == 0:
        return []

    headers_row = raw.row(0)
    body = raw.slice(1)

    muni_code_col: str | None = None
    muni_name_col: str | None = None
    for src_col, header in zip(raw.columns, headers_row, strict=True):
        if header is None:
            continue
        h = str(header).strip().lower()
        if h == "municode":
            muni_code_col = src_col
        elif h == "municipality":
            muni_name_col = src_col

    if muni_code_col is None or muni_name_col is None:
        return []

    munis = body.filter(
        pl.col(muni_code_col).is_not_null()
        & (pl.col(muni_code_col).str.len_chars() == 4)
        & ~pl.col(muni_code_col).str.ends_with("00")
    ).select(
        pl.col(muni_code_col).alias("muni_code"),
        pl.col(muni_name_col).alias("muni_name"),
    )

    out: list[tuple[str, str]] = []
    for r in munis.iter_rows(named=True):
        code = str(r["muni_code"]).strip()
        name = str(r["muni_name"]).strip() if r["muni_name"] is not None else ""
        if code and name:
            out.append((code, name))
    return out


def main() -> int:
    workbooks = sorted(WORKBOOK_DIR.glob("*taxes.xls"))
    # Filter: only "modern format" workbooks (the parser's earliest-year
    # gate is 2016 = '16taxes.xls'). 10taxes.xls predates the multi-sheet
    # workbook layout and would crash the parser; skip it.
    workbooks = [p for p in workbooks if p.name >= "16taxes.xls"]
    if not workbooks:
        print(f"ERROR: no workbooks found in {WORKBOOK_DIR}", file=sys.stderr)
        return 1

    # Newest first: when a (muni_code, county_fips) is observed in
    # multiple workbooks with different muni_name strings, the most
    # recent observation wins. NJ's DCA occasionally re-spells a muni
    # (e.g. capitalization, "Borough" vs "Boro") and we honor the latest
    # canonical form.
    workbooks_newest_first = list(reversed(workbooks))

    # (muni_code, county_fips) -> (muni_name, source_workbook_name)
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for wb in workbooks_newest_first:
        wb_rows = _extract_munis_from_workbook(wb)
        for code, name in wb_rows:
            dca_county = code[:2]
            fips = DCA_TO_FIPS.get(dca_county)
            if fips is None:
                print(
                    f"ERROR: workbook {wb.name} has muni_code {code} with "
                    f"unknown DCA county {dca_county}",
                    file=sys.stderr,
                )
                return 1
            key = (code, fips)
            if key not in seen:
                seen[key] = (name, wb.name)

    if not seen:
        print("ERROR: extracted 0 muni rows from all workbooks", file=sys.stderr)
        return 1

    rows: list[tuple[str, str, str, str]] = []  # (code, name, fips, src_wb)
    for (code, fips), (name, src) in sorted(seen.items()):
        rows.append((code, name, fips, src))

    out: list[str] = []
    out.append(
        "-- ============================================================================\n"
        f"-- Seed: 040_nj_municipality ({len(rows)} rows)\n"
        "--\n"
        f"-- The UNION of every NJ municipality observed in the 2016-2024 NJ DCA\n"
        f"-- workbooks ({len(rows)} unique (muni_code, county_fips) pairs).\n"
        "-- The 2024 workbook alone publishes 564 munis (post-Princeton-merger);\n"
        "-- earlier years reference a small number of additional muni codes that\n"
        "-- have since been retired through municipal consolidation. We seed the\n"
        "-- UNION so the FK from raw.nj_property_tax_muni to ref.nj_municipality\n"
        "-- holds across the full historical ingest.\n"
        "--\n"
        "-- Keying: 4-digit MuniCode. First 2 digits = DCA county code 01..21,\n"
        "-- last 2 digits = municipal index within the county; '00' suffix\n"
        "-- denotes the county-level summary and is excluded by CHECK constraint.\n"
        "--\n"
        "-- Source family: NJ Division of Local Government Services, annual\n"
        "-- '{YY}taxes.xls' workbooks (2016-2024).\n"
        "--   https://www.nj.gov/dca/dlgs/resources/Property_Tax_info.shtml\n"
        "--   Sheet: 'Municipal Tax Summary'. When a muni_code appears in\n"
        "--   multiple vintages with a different muni_name spelling, the\n"
        "--   most recent observation wins (NJ DCA occasionally re-spells\n"
        "--    'Borough' / 'Boro', etc.).\n"
        "--\n"
        "-- Generated by: scripts/_generate_phase8_muni_seed.py (one-shot).\n"
        "-- Idempotent under re-run via the muni_code conflict handler.\n"
        "-- ============================================================================\n\n"
    )
    out.append("INSERT INTO ref.nj_municipality (muni_code, muni_name, county_fips, source_url, source_citation) VALUES\n")
    body_lines: list[str] = []
    for code, name, fips, src_wb in rows:
        # The 2-digit year suffix in '24taxes.xls' -> 2024; we pin the
        # source_url to the canonical DCA path for that vintage.
        yy = src_wb.replace("taxes.xls", "")
        full_year = 2000 + int(yy)
        url = (
            f"https://www.nj.gov/dca/dlgs/resources/Property_Tax/{yy}_data/"
            f"{src_wb}"
        )
        cite = (
            f"NJ DCA Municipal Tax Summary, {full_year} workbook ({src_wb}), "
            f"Municipality column."
        )
        esc_name = name.replace("'", "''")
        body_lines.append(
            f"    ('{code}', '{esc_name}', '{fips}', '{url}', '{cite}')"
        )
    out.append(",\n".join(body_lines))
    out.append("\n")
    out.append(
        "ON CONFLICT (muni_code) DO UPDATE SET\n"
        "    muni_name       = EXCLUDED.muni_name,\n"
        "    county_fips     = EXCLUDED.county_fips,\n"
        "    source_url      = EXCLUDED.source_url,\n"
        "    source_citation = EXCLUDED.source_citation;\n"
    )
    sys.stdout.write("".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
