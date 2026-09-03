"""Unit tests for the DOL WHD H-1B debarment / willful-violator parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ingestion._base import canonical_employer_name
from ingestion.dol_whd_h1b_lists import parse_whd_h1b_file, stage_dataframe

_DEBARMENT_HTML = """<!DOCTYPE html>
<html><body>
<p>This list is effective as of September 1, 2026.</p>
<table>
<tr>
  <th>Employer Name</th><th>Employer Address</th>
  <th>Willful Violator</th><th>Debarment Period</th>
</tr>
<tr>
  <td>GowraTech, LLC</td><td></td>
  <td>Yes</td><td>5/12/2025 to 5/11/2027</td>
</tr>
<tr>
  <td>Seeloz, Inc.</td><td></td>
  <td>Yes</td><td>3/4/2026 to 3/3/2028</td>
</tr>
</table>
<p>Last Updated on August 28, 2026.</p>
</body></html>
"""

_WILLFUL_HTML = """<!DOCTYPE html>
<html><body>
<p>This list is effective as of September 1, 2026.</p>
<table>
<tr>
  <th>Employer Name</th><th>City</th><th>State</th>
  <th>Date of Willful Violation Determination</th>
  <th>Agency Making Determination</th>
</tr>
<tr>
  <td>Bonzer, LLC</td><td></td><td></td>
  <td>7/18/2023</td><td></td>
</tr>
<tr>
  <td>GowraTech, LLC</td><td></td><td></td>
  <td>4/11/2025</td><td></td>
</tr>
</table>
<p>Last Updated on August 28, 2026.</p>
</body></html>
"""


def test_parse_debarment_html(tmp_path: Path) -> None:
    path = tmp_path / "whd_h1b_debarment.html"
    path.write_text(_DEBARMENT_HTML, encoding="utf-8")
    parsed = parse_whd_h1b_file(
        path,
        list_kind="debarment",
        source_url="https://www.dol.gov/agencies/whd/immigration/h1b/debarment",
    )
    assert parsed.n_output_rows == 2
    staged = stage_dataframe(parsed)
    gowra = staged.filter(
        staged["employer_canonical_name"] == canonical_employer_name("GowraTech, LLC")
    )
    assert gowra.height == 1
    assert gowra["list_kind"][0] == "debarment"
    assert gowra["debarment_start"][0] == date(2025, 5, 12)
    assert gowra["debarment_end"][0] == date(2027, 5, 11)
    assert gowra["willful_violator"][0] is True
    assert gowra["source_page_updated"][0] == date(2026, 8, 28)
    assert gowra["list_effective_date"][0] == date(2026, 9, 1)


def test_parse_willful_html(tmp_path: Path) -> None:
    path = tmp_path / "whd_h1b_willful.html"
    path.write_text(_WILLFUL_HTML, encoding="utf-8")
    parsed = parse_whd_h1b_file(
        path,
        list_kind="willful",
        source_url="https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list",
    )
    assert parsed.n_output_rows == 2
    staged = stage_dataframe(parsed)
    bonzer = staged.filter(
        staged["employer_canonical_name"] == canonical_employer_name("Bonzer, LLC")
    )
    assert bonzer.height == 1
    assert bonzer["determination_date"][0] == date(2023, 7, 18)
    assert bonzer["willful_violator"][0] is True
