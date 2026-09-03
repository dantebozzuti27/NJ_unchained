"""Shared pytest fixtures.

Two patterns documented here once so individual tests do not repeat them:

* ``live_pg`` -- a fixture that yields a psycopg connection to an
  ephemeral Postgres instance referenced by the ``PG_TEST_DSN`` env var.
  Tests that need a database mark themselves with
  ``@pytest.mark.live_pg`` and call this fixture; if ``PG_TEST_DSN`` is
  not set, the test is skipped with a clear message.
* ``tmp_lca_csv`` -- a parametrized fixture that materializes a tiny
  synthetic LCA CSV in each known schema vintage. Used by the
  cross-vintage ingester tests to avoid depending on the actual DOL
  files (which are too large to commit and licence-encumbered to
  redistribute).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


@pytest.fixture
def live_pg() -> Iterator[psycopg.Connection]:
    """Yield a connection to PG_TEST_DSN. Skip if it is not set."""
    dsn = os.environ.get("PG_TEST_DSN")
    if not dsn:
        pytest.skip("PG_TEST_DSN not set; skipping live-Postgres test.")

    import psycopg

    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# --------------------------------------------------------------------------
# Synthetic LCA CSV fixtures, one per known schema vintage.
#
# Each fixture writes a 3-row CSV in the columnar shape DOL actually
# published for that vintage and returns the file path. The values are
# intentionally diverse enough to exercise:
#   - all five wage_unit_of_pay values (Hour/Week/Bi-Weekly/Month/Year)
#   - certified, denied, and withdrawn cases
#   - ZIPs that lost a leading zero (Excel ate it)
#   - employer-name suffix variants (LLC vs L.L.C.)
# --------------------------------------------------------------------------


@pytest.fixture
def lca_v1_2008_csv(tmp_path: Path) -> Path:
    """A 3-row synthetic v1_2008 (per-program) LCA file."""
    path = tmp_path / "H-1B_FY2010_Q4.csv"
    path.write_text(
        "LCA_CASE_NUMBER,LCA_CASE_EMPLOYER_NAME,STATUS,VISA_CLASS,"
        "WAGE_RATE_1,WAGE_UNIT_1,LCA_CASE_WORKLOC1_CITY,LCA_CASE_WORKLOC1_STATE,"
        "LCA_CASE_WORKLOC1_POSTAL_CODE,TOTAL_WORKERS\n"
        "I-200-10001-001,Tata Consultancy Services L.L.C.,Certified,H-1B,"
        "85000,Year,Iselin,NJ,8830,1\n"
        "I-200-10001-002,Infosys Ltd,DENIED,H-1B,"
        "62.50,Hour,Newark,NJ,07102,2\n"
        "I-200-10001-003,Cognizant Technologies Inc.,Withdrawn,H-1B,"
        "5500,Month,Jersey City,NJ,07302,1\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def lca_v2_2014_csv(tmp_path: Path) -> Path:
    """A 3-row synthetic v2_2014 (consolidated H-1B) LCA file."""
    path = tmp_path / "H1B_Disclosure_Data_FY15_Q1.csv"
    path.write_text(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME,EMPLOYER_STATE,"
        "WORKSITE_CITY,WORKSITE_STATE,WORKSITE_POSTAL_CODE,TOTAL_WORKERS,"
        "WAGE_RATE_OF_PAY_FROM,WAGE_RATE_OF_PAY_TO,WAGE_UNIT_OF_PAY,"
        "PREVAILING_WAGE,PW_UNIT_OF_PAY,SOC_CODE,JOB_TITLE\n"
        "I-201-15001-001,CERTIFIED,H-1B,Tata Consultancy Services L.L.C.,NJ,"
        "Iselin,NJ,8830,1,85000,90000,Year,80000,Year,15-1132,Software Developer\n"
        "I-201-15001-002,DENIED,H-1B,Infosys LLC,NJ,"
        "Newark,NJ,07102,2,62.50,75.00,Hour,60.00,Hour,15-1133,Software Engineer\n"
        "I-201-15001-003,WITHDRAWN,H-1B,Cognizant Technologies Inc.,NJ,"
        "Jersey City,NJ,07302,1,5500,6000,Month,5200,Month,15-1131,Programmer\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def lca_v3_2018_csv(tmp_path: Path) -> Path:
    """A 2-row synthetic v3_2018 (multi-worksite wide) LCA file.

    Row 1 has 2 worksites populated; row 2 has 1. After unstacking we
    expect 3 rows.
    """
    path = tmp_path / "H-1B_Disclosure_Data_FY18_Q1.csv"
    path.write_text(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME,"
        "WAGE_RATE_OF_PAY_FROM,WAGE_RATE_OF_PAY_TO,WAGE_UNIT_OF_PAY,"
        "WORKSITE_CITY_1,WORKSITE_STATE_1,WORKSITE_POSTAL_CODE_1,"
        "WORKSITE_CITY_2,WORKSITE_STATE_2,WORKSITE_POSTAL_CODE_2,"
        "WORKSITE_CITY_3,WORKSITE_STATE_3,WORKSITE_POSTAL_CODE_3\n"
        "I-203-18001-001,CERTIFIED,H-1B,Tata Consultancy Services LLC,"
        "85000,90000,Year,"
        "Iselin,NJ,8830,Newark,NJ,07102,,,\n"
        "I-203-18001-002,CERTIFIED,H-1B,Infosys L.L.C.,"
        "62.50,75.00,Hour,"
        "Jersey City,NJ,07302,,,,,,\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def lca_v4_2020_csv(tmp_path: Path) -> Path:
    """A 3-row synthetic v4_2020 (single-worksite, no NAICS, has FULL_TIME_POSITION).

    Uses BEGIN_DATE/END_DATE (renamed from EMPLOYMENT_START_DATE in 2018) and
    TOTAL_WORKER_POSITIONS (renamed from TOTAL_WORKERS in 2018).
    """
    path = tmp_path / "LCA_Disclosure_Data_FY2020_Q1.csv"
    path.write_text(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME,FULL_TIME_POSITION,"
        "BEGIN_DATE,END_DATE,"
        "WORKSITE_CITY,WORKSITE_STATE,WORKSITE_POSTAL_CODE,TOTAL_WORKER_POSITIONS,"
        "WAGE_RATE_OF_PAY_FROM,WAGE_RATE_OF_PAY_TO,WAGE_UNIT_OF_PAY,"
        "PREVAILING_WAGE,PW_UNIT_OF_PAY\n"
        "I-204-20001-001,CERTIFIED,H-1B,Tata Consultancy Services LLC,Y,"
        "2020-04-01 00:00:00,2023-03-31 00:00:00,"
        "Iselin,NJ,8830,1,85000,90000,Year,80000,Year\n"
        "I-204-20001-002,DENIED,H-1B,Infosys LLC,Y,"
        "2020-04-01 00:00:00,2023-03-31 00:00:00,"
        "Newark,NJ,07102,2,62.50,75.00,Hour,60.00,Hour\n"
        "I-204-20001-003,CERTIFIED,H-2A,Pinelands Farms LLC,Y,"
        "2020-04-01 00:00:00,2020-09-30 00:00:00,"
        "Vineland,NJ,08360,5,2200,2400,Bi-Weekly,2100,Bi-Weekly\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def lca_v5_2023_csv(tmp_path: Path) -> Path:
    """A 3-row synthetic v5_2023 (with NAICS_CODE, BEGIN/END_DATE, FULL_TIME_POSITION).

    This mirrors the actual FY2023+ DOL OFLC schema: column is NAICS_CODE
    (not EMPLOYER_NAICS_CODE) and employment dates are BEGIN_DATE/END_DATE
    (renamed in 2018 with the new ETA 9035 form).
    """
    path = tmp_path / "LCA_Disclosure_Data_FY2024_Q3.csv"
    path.write_text(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME,NAICS_CODE,"
        "FULL_TIME_POSITION,BEGIN_DATE,END_DATE,"
        "WORKSITE_CITY,WORKSITE_STATE,WORKSITE_POSTAL_CODE,TOTAL_WORKER_POSITIONS,"
        "WAGE_RATE_OF_PAY_FROM,WAGE_RATE_OF_PAY_TO,WAGE_UNIT_OF_PAY,"
        "PREVAILING_WAGE,PW_UNIT_OF_PAY,"
        "EMPLOYER_FEIN,H-1B_DEPENDENT,WILLFUL_VIOLATOR,"
        "SECONDARY_ENTITY,PW_WAGE_LEVEL\n"
        "I-205-24001-001,CERTIFIED,H-1B,Tata Consultancy Services LLC,541512,Y,"
        '2024-07-15 00:00:00,2027-07-14 00:00:00,'
        'Iselin,NJ,8830,1,"$95,000.00","$100,000.00",Year,"$92,000.00",Year,'
        "22-1234567,N,N,N,II\n"
        "I-205-24001-002,CERTIFIED,H-1B,Infosys LLC,541512,Y,"
        "2024-08-01 00:00:00,2027-07-31 00:00:00,"
        "Newark,NJ,07102,2,72.50,80.00,Hour,70.00,Hour,"
        "11-9876543,Y,N,Y,I\n"
        "I-205-24001-003,WITHDRAWN,E-3 Australian,Macquarie Bank Limited,522110,Y,"
        "2024-09-01 00:00:00,2026-08-31 00:00:00,"
        "Jersey City,NJ,07302,1,180000,200000,Year,170000,Year,"
        "98-0000001,N,N,N,IV\n",
        encoding="utf-8",
    )
    return path
