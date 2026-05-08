#!/usr/bin/env python3
# ruff: noqa: E501
"""One-shot generator for Phase-5 historical tax-table seeds.

This script is NOT part of the production migration pipeline. It exists
solely to emit canonical SQL seed files for tax years 2010-2015 (federal +
NJ) in the same format as hand-written seeds 020-027. Run once, commit
the emitted SQL, then archive this script.

All numeric constants are hand-transcribed from authoritative sources:
- Rev. Proc. 2009-50 (TY 2010) https://www.irs.gov/pub/irs-drop/rp-09-50.pdf
- Rev. Proc. 2011-12  (TY 2011) https://www.irs.gov/pub/irs-drop/rp-11-12.pdf
- Rev. Proc. 2011-52  (TY 2012) https://www.irs.gov/pub/irs-drop/rp-11-52.pdf
- Rev. Proc. 2013-15  (TY 2013) https://www.irs.gov/pub/irs-drop/rp-13-15.pdf
- Rev. Proc. 2013-35  (TY 2014) https://www.irs.gov/pub/irs-drop/rp-13-35.pdf
- SSA Contribution and Benefit Base for FICA wage bases.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

# ----------------------------------------------------------------------------
# Federal data per year, transcribed from Rev. Proc. of record.
# Each "brackets" dict maps filing_status -> list of (floor, rate) tuples.
# ----------------------------------------------------------------------------

FEDERAL_DATA: dict[int, dict[str, Any]] = {
    2014: {
        "rev_proc": "2013-35",
        "url": "https://www.irs.gov/pub/irs-drop/rp-13-35.pdf",
        "brackets": {
            "single": [(0, "0.10000"), (9075, "0.15000"), (36900, "0.25000"),
                       (89350, "0.28000"), (186350, "0.33000"), (405100, "0.35000"),
                       (406750, "0.39600")],
            "mfj":    [(0, "0.10000"), (18150, "0.15000"), (73800, "0.25000"),
                       (148850, "0.28000"), (226850, "0.33000"), (405100, "0.35000"),
                       (457600, "0.39600")],
            "qss":    [(0, "0.10000"), (18150, "0.15000"), (73800, "0.25000"),
                       (148850, "0.28000"), (226850, "0.33000"), (405100, "0.35000"),
                       (457600, "0.39600")],
            "mfs":    [(0, "0.10000"), (9075, "0.15000"), (36900, "0.25000"),
                       (74425, "0.28000"), (113425, "0.33000"), (202550, "0.35000"),
                       (228800, "0.39600")],
            "hoh":    [(0, "0.10000"), (12950, "0.15000"), (49400, "0.25000"),
                       (127550, "0.28000"), (206600, "0.33000"), (405100, "0.35000"),
                       (432200, "0.39600")],
        },
        "std_deduction": {"single": 6200, "mfj": 12400, "mfs": 6200,
                          "hoh": 9100, "qss": 12400},
        "aged_blind_unmarried": 1550,
        "aged_blind_married": 1200,
        "personal_exemption": 3950,
        "ss_wage_base": 117000,
        "ss_rate": "0.06200",
        "addl_medicare": True,
    },
    2013: {
        "rev_proc": "2013-15",
        "url": "https://www.irs.gov/pub/irs-drop/rp-13-15.pdf",
        "brackets": {
            "single": [(0, "0.10000"), (8925, "0.15000"), (36250, "0.25000"),
                       (87850, "0.28000"), (183250, "0.33000"), (398350, "0.35000"),
                       (400000, "0.39600")],
            "mfj":    [(0, "0.10000"), (17850, "0.15000"), (72500, "0.25000"),
                       (146400, "0.28000"), (223050, "0.33000"), (398350, "0.35000"),
                       (450000, "0.39600")],
            "qss":    [(0, "0.10000"), (17850, "0.15000"), (72500, "0.25000"),
                       (146400, "0.28000"), (223050, "0.33000"), (398350, "0.35000"),
                       (450000, "0.39600")],
            "mfs":    [(0, "0.10000"), (8925, "0.15000"), (36250, "0.25000"),
                       (73200, "0.28000"), (111525, "0.33000"), (199175, "0.35000"),
                       (225000, "0.39600")],
            "hoh":    [(0, "0.10000"), (12750, "0.15000"), (48600, "0.25000"),
                       (125450, "0.28000"), (203150, "0.33000"), (398350, "0.35000"),
                       (425000, "0.39600")],
        },
        "std_deduction": {"single": 6100, "mfj": 12200, "mfs": 6100,
                          "hoh": 8950, "qss": 12200},
        "aged_blind_unmarried": 1500,
        "aged_blind_married": 1200,
        "personal_exemption": 3900,
        "ss_wage_base": 113700,
        "ss_rate": "0.06200",
        "addl_medicare": True,
    },
    2012: {
        "rev_proc": "2011-52",
        "url": "https://www.irs.gov/pub/irs-drop/rp-11-52.pdf",
        "brackets": {
            # NOTE: 6 brackets only (10/15/25/28/33/35); ATRA's 39.6% added TY 2013+.
            "single": [(0, "0.10000"), (8700, "0.15000"), (35350, "0.25000"),
                       (85650, "0.28000"), (178650, "0.33000"), (388350, "0.35000")],
            "mfj":    [(0, "0.10000"), (17400, "0.15000"), (70700, "0.25000"),
                       (142700, "0.28000"), (217450, "0.33000"), (388350, "0.35000")],
            "qss":    [(0, "0.10000"), (17400, "0.15000"), (70700, "0.25000"),
                       (142700, "0.28000"), (217450, "0.33000"), (388350, "0.35000")],
            "mfs":    [(0, "0.10000"), (8700, "0.15000"), (35350, "0.25000"),
                       (71350, "0.28000"), (108725, "0.33000"), (194175, "0.35000")],
            "hoh":    [(0, "0.10000"), (12400, "0.15000"), (47350, "0.25000"),
                       (122300, "0.28000"), (198050, "0.33000"), (388350, "0.35000")],
        },
        "std_deduction": {"single": 5950, "mfj": 11900, "mfs": 5950,
                          "hoh": 8700, "qss": 11900},
        "aged_blind_unmarried": 1450,
        "aged_blind_married": 1150,
        "personal_exemption": 3800,
        "ss_wage_base": 110100,
        "ss_rate": "0.04200",
        "addl_medicare": False,
    },
    2011: {
        "rev_proc": "2011-12",
        "url": "https://www.irs.gov/pub/irs-drop/rp-11-12.pdf",
        "brackets": {
            "single": [(0, "0.10000"), (8500, "0.15000"), (34500, "0.25000"),
                       (83600, "0.28000"), (174400, "0.33000"), (379150, "0.35000")],
            "mfj":    [(0, "0.10000"), (17000, "0.15000"), (69000, "0.25000"),
                       (139350, "0.28000"), (212300, "0.33000"), (379150, "0.35000")],
            "qss":    [(0, "0.10000"), (17000, "0.15000"), (69000, "0.25000"),
                       (139350, "0.28000"), (212300, "0.33000"), (379150, "0.35000")],
            "mfs":    [(0, "0.10000"), (8500, "0.15000"), (34500, "0.25000"),
                       (69675, "0.28000"), (106150, "0.33000"), (189575, "0.35000")],
            "hoh":    [(0, "0.10000"), (12150, "0.15000"), (46250, "0.25000"),
                       (119400, "0.28000"), (193350, "0.33000"), (379150, "0.35000")],
        },
        "std_deduction": {"single": 5800, "mfj": 11600, "mfs": 5800,
                          "hoh": 8500, "qss": 11600},
        "aged_blind_unmarried": 1450,
        "aged_blind_married": 1150,
        "personal_exemption": 3700,
        "ss_wage_base": 106800,
        "ss_rate": "0.04200",
        "addl_medicare": False,
    },
    2010: {
        "rev_proc": "2009-50",
        "url": "https://www.irs.gov/pub/irs-drop/rp-09-50.pdf",
        "brackets": {
            "single": [(0, "0.10000"), (8375, "0.15000"), (34000, "0.25000"),
                       (82400, "0.28000"), (171850, "0.33000"), (373650, "0.35000")],
            "mfj":    [(0, "0.10000"), (16750, "0.15000"), (68000, "0.25000"),
                       (137300, "0.28000"), (209250, "0.33000"), (373650, "0.35000")],
            "qss":    [(0, "0.10000"), (16750, "0.15000"), (68000, "0.25000"),
                       (137300, "0.28000"), (209250, "0.33000"), (373650, "0.35000")],
            "mfs":    [(0, "0.10000"), (8375, "0.15000"), (34000, "0.25000"),
                       (68650, "0.28000"), (104625, "0.33000"), (186825, "0.35000")],
            "hoh":    [(0, "0.10000"), (11950, "0.15000"), (45550, "0.25000"),
                       (117650, "0.28000"), (190550, "0.33000"), (373650, "0.35000")],
        },
        "std_deduction": {"single": 5700, "mfj": 11400, "mfs": 5700,
                          "hoh": 8400, "qss": 11400},
        "aged_blind_unmarried": 1400,
        "aged_blind_married": 1100,
        "personal_exemption": 3650,
        "ss_wage_base": 106800,
        "ss_rate": "0.06200",
        "addl_medicare": False,
    },
}


def emit_federal_seed(year: int, seed_num: int) -> str:
    d = FEDERAL_DATA[year]
    url = d["url"]
    rp = d["rev_proc"]
    bracket_lines = []
    for status, brackets in d["brackets"].items():
        for ord_, (floor, rate) in enumerate(brackets, start=1):
            bracket_lines.append(
                f"    ({year}, '{status}', {ord_}, {floor:>10}.00, {rate}, "
                f"'{url}', 'Rev. Proc. {rp} s.3.01 ({status})'),"
            )
    bracket_sql = "\n".join(bracket_lines)
    if bracket_sql.endswith(","):
        bracket_sql = bracket_sql[:-1]

    sd = d["std_deduction"]
    abu = d["aged_blind_unmarried"]
    abm = d["aged_blind_married"]
    sd_lines = [
        f"    ({year}, 'single',  {sd['single']:>5}.00, {abu}.00, {abu}.00, '{url}', 'Rev. Proc. {rp} std deduction')",
        f"    ({year}, 'mfj',    {sd['mfj']:>5}.00, {abm}.00, {abm}.00, '{url}', 'Rev. Proc. {rp} std deduction')",
        f"    ({year}, 'mfs',    {sd['mfs']:>5}.00, {abm}.00, {abm}.00, '{url}', 'Rev. Proc. {rp} std deduction')",
        f"    ({year}, 'hoh',    {sd['hoh']:>5}.00, {abu}.00, {abu}.00, '{url}', 'Rev. Proc. {rp} std deduction')",
        f"    ({year}, 'qss',    {sd['qss']:>5}.00, {abm}.00, {abm}.00, '{url}', 'Rev. Proc. {rp} std deduction')",
    ]
    sd_sql = ",\n".join(sd_lines)

    pe = d["personal_exemption"]
    ssr = d["ss_rate"]
    ssb = d["ss_wage_base"]
    if d["addl_medicare"]:
        addl_rate = "0.00900"
        thr_single = "200000.00"
        thr_mfj = "250000.00"
        addl_note = "Add''l Medicare per IRC s.3101(b)(2) (ACA s.9015 effective TY 2013+)"
    else:
        addl_rate = "0.00000"
        thr_single = "NULL"
        thr_mfj = "NULL"
        addl_note = "ACA s.9015 Add''l Medicare 0.9% NOT yet effective (TY < 2013)"

    payroll_holiday_note = (
        " (PAYROLL TAX HOLIDAY: 4.2% per P.L. 111-312 (TY 2011) extended via P.L. 112-78 + P.L. 112-96 (TY 2012))"
        if ssr == "0.04200" else ""
    )

    return dedent(f"""\
        -- ============================================================================
        -- Seed: {seed_num:03d}_irs_federal_tax_{year}
        -- Source: Rev. Proc. {rp} ({url})
        -- {('6-bracket pre-ATRA ladder (10/15/25/28/33/35); ATRA 39.6% top added TY 2013.' if year < 2013 else '7-bracket ATRA-era ladder (10/15/25/28/33/35/39.6) per P.L. 112-240.')}
        -- SS rate {ssr}{payroll_holiday_note}; wage base ${ssb:,}.
        -- ============================================================================
        INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
        {bracket_sql}
        ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
        {sd_sql}
        ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
        VALUES ({year}, {pe}.00, '{url}', 'Rev. Proc. {rp} personal exemption')
        ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.irs_child_tax_credit (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child, phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate, source_url, source_citation)
        VALUES ({year}, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000, 'https://www.law.cornell.edu/uscode/text/26/24', 'IRC s.24 pre-TCJA: $1,000 base; phaseout $75K Single/$110K MFJ at 5%')
        ON CONFLICT (tax_year) DO UPDATE SET amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17, refundable_max_per_child = EXCLUDED.refundable_max_per_child, phaseout_threshold_single = EXCLUDED.phaseout_threshold_single, phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
        VALUES ({year}, {ssr}, {ssb}.00, 0.01450, {addl_rate}, {thr_single}, {thr_mfj}, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY{year} ${ssb:,}; {addl_note}')
        ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
        """)


# NJ data per year. Schedules I/II are IDENTICAL TY 2010-2017 (P.L. 2004 c.40
# baseline; P.L. 2009 c.69 retroactive 10.75% expired TY 2010; P.L. 2018 c.45
# new 10.75%-above-$5M not yet effective).
NJ_SCHEDULE_I = [(0, "0.01400"), (20000, "0.01750"), (35000, "0.03500"),
                 (40000, "0.05525"), (75000, "0.06370"), (500000, "0.08970")]
NJ_SCHEDULE_II = [(0, "0.01400"), (20000, "0.01750"), (50000, "0.02450"),
                  (70000, "0.03500"), (80000, "0.05525"), (150000, "0.06370"),
                  (500000, "0.08970")]

# NJ EITC phased rates per P.L. 2008 c.110, 2010 c.27, 2015 c.180, 2018 c.45,
# 2020 c.21. Verified at https://nj.gov/treasury/taxation/eitc/prioryear.shtml.
NJ_EITC = {2010: "0.20000", 2011: "0.20000", 2012: "0.25000",
           2013: "0.25000", 2014: "0.25000", 2015: "0.25000"}

# NJ property-tax cap. P.L. 2010 c.27 SUSPENDED the deduction for TY 2010 only;
# restored to $10K for TY 2011+.
NJ_PTD_CAP = {2010: 0, 2011: 10000, 2012: 10000, 2013: 10000,
              2014: 10000, 2015: 10000}


def emit_nj_seed(year: int, seed_num: int) -> str:
    eitc_rate = NJ_EITC[year]
    ptd_cap = NJ_PTD_CAP[year]
    cap_note = (
        "PTD SUSPENDED for TY 2010 ONLY by P.L. 2010 c.27 (cap=$0); restored to $10K TY 2011+; alt credit $50 always available"
        if year == 2010 else
        "$10K cap (P.L. 1996 c.60 baseline)"
    )

    bracket_lines = []
    for status, brackets in [
        ("single", NJ_SCHEDULE_I),
        ("mfs",    NJ_SCHEDULE_I),
        ("mfj",    NJ_SCHEDULE_II),
        ("hoh",    NJ_SCHEDULE_II),
        ("qss",    NJ_SCHEDULE_II),
    ]:
        sched_label = "Schedule I" if status in ("single", "mfs") else "Schedule II"
        for ord_, (floor, rate) in enumerate(brackets, start=1):
            bracket_lines.append(
                f"    ({year}, '{status}', {ord_}, {floor:>10}.00, {rate}, "
                f"'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', "
                f"'NJ-1040 TY{year} {sched_label}'),"
            )
    bracket_sql = "\n".join(bracket_lines)
    if bracket_sql.endswith(","):
        bracket_sql = bracket_sql[:-1]

    pe_lines = [
        f"    ({year}, 'taxpayer', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year} NJSA 54A:3-1.1'),",
        f"    ({year}, 'spouse', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}'),",
        f"    ({year}, 'dependent', 1500.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}'),",
        f"    ({year}, 'dependent_college_under_22', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}'),",
        f"    ({year}, 'taxpayer_age_65_plus', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}'),",
        f"    ({year}, 'spouse_age_65_plus', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}'),",
        f"    ({year}, 'taxpayer_blind_disabled', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}'),",
        f"    ({year}, 'spouse_blind_disabled', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year}')",
    ]
    pe_sql = "\n".join(pe_lines)

    return dedent(f"""\
        -- ============================================================================
        -- Seed: {seed_num:03d}_nj_state_tax_{year}
        -- NJ Schedule I/II UNCHANGED 2010-2017 (P.L. 2004 c.40 baseline 8.97% top).
        -- EITC {eitc_rate}; PTD cap ${ptd_cap:,}; no veteran exemption (P.L. 2017 c.36 not yet effective).
        -- {cap_note}
        -- ============================================================================
        INSERT INTO ref.nj_state_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
        {bracket_sql}
        ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.nj_state_personal_exemption (tax_year, exemption_kind, amount, source_url, source_citation) VALUES
        {pe_sql}
        ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.nj_state_property_tax_deduction (tax_year, deduction_cap, alternative_credit, rent_property_tax_share, source_url, source_citation)
        VALUES ({year}, {ptd_cap}.00, 50.00, 0.18, 'https://www.state.nj.us/treasury/taxation/pdf/{year}/1040i.pdf', 'NJ-1040 TY{year} PTD; {cap_note}; NJSA 54A:3A-17')
        ON CONFLICT (tax_year) DO UPDATE SET deduction_cap = EXCLUDED.deduction_cap, alternative_credit = EXCLUDED.alternative_credit, rent_property_tax_share = EXCLUDED.rent_property_tax_share, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

        INSERT INTO ref.nj_state_eitc_match (tax_year, match_rate, eligibility_note, source_url, source_citation)
        VALUES ({year}, {eitc_rate}, 'NJEITC for workers 25-64 with qualifying children. Pre-2020 expansions.', 'https://nj.gov/treasury/taxation/eitc/prioryear.shtml', 'NJSA 54A:4-7; phased rate per P.L. 2008 c.110 / 2010 c.27 / 2015 c.180')
        ON CONFLICT (tax_year) DO UPDATE SET match_rate = EXCLUDED.match_rate, eligibility_note = EXCLUDED.eligibility_note, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
        """)


# Federal seeds: 028 = TY 2014, 030 = TY 2013, 032 = TY 2012, 034 = TY 2011, 036 = TY 2010.
# NJ seeds:      029 = TY 2014, 031 = TY 2013, 033 = TY 2012, 035 = TY 2011, 037 = TY 2010.
SEED_NUMBERING = [
    (2014, 28, 29),
    (2013, 30, 31),
    (2012, 32, 33),
    (2011, 34, 35),
    (2010, 36, 37),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDS_DIR = REPO_ROOT / "db" / "seeds"


def main() -> None:
    for year, fed_num, nj_num in SEED_NUMBERING:
        fed_path = SEEDS_DIR / f"{fed_num:03d}_irs_federal_tax_{year}.sql"
        nj_path = SEEDS_DIR / f"{nj_num:03d}_nj_state_tax_{year}.sql"
        fed_path.write_text(emit_federal_seed(year, fed_num))
        nj_path.write_text(emit_nj_seed(year, nj_num))
        print(f"  emitted {fed_path.name} + {nj_path.name}")


if __name__ == "__main__":
    main()
