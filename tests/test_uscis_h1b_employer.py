"""Unit tests for the USCIS H-1B Employer Data Hub parser (POP-3)."""

from __future__ import annotations

from pathlib import Path

from ingestion._base import canonical_employer_name
from ingestion.uscis_h1b_employer import (
    build_uscis_h1b_url,
    parse_uscis_h1b_file,
    stage_dataframe,
)


def test_build_uscis_h1b_url() -> None:
    assert build_uscis_h1b_url(2025).endswith("h1b_datahubexport-2025.csv")


def test_parse_and_stage_minimal_csv(tmp_path: Path) -> None:
    csv = tmp_path / "h1b_datahubexport-2025.csv"
    csv.write_text(
        "Fiscal Year,Employer (Petitioner) Name,Tax ID,NAICS,City,State,ZIP,"
        "Initial Approval,Initial Denial,Continuing Approval,Continuing Denial\n"
        "2025,ACME SOFTWARE INC,1234,541511,Hoboken,NJ,07030,8,2,4,1\n"
        '2025,"Acme Software, Inc.",1234,541511,Hoboken,NJ,07030,1,0,0,0\n'
        "2025,OTHER CORP,9999,541512,Austin,TX,78701,20,0,10,0\n",
        encoding="utf-8",
    )
    parsed = parse_uscis_h1b_file(csv)
    assert parsed.fiscal_year == 2025
    assert parsed.n_output_rows == 3
    staged = stage_dataframe(parsed)
    # Two ACME rows canonicalize together and collapse on PK.
    acme = canonical_employer_name("ACME SOFTWARE INC")
    acme_rows = staged.filter(staged["employer_canonical_name"] == acme)
    assert acme_rows.height == 1
    assert int(acme_rows["initial_approval"][0]) == 9
    nj = staged.filter(staged["petitioner_state"] == "NJ")
    assert nj.height == 1
