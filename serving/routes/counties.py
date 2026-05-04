"""GET /counties: NJ counties for UI dropdowns.

Trivial reference endpoint. Pulled live from ref.county so that if the
seed ever expands or a county is renamed, the UI auto-discovers the
change without a redeploy.
"""

from __future__ import annotations

from fastapi import APIRouter

from serving.db import borrow_connection
from serving.models import CountyRef
from serving.queries import list_counties

router = APIRouter(tags=["counties"])


@router.get(
    "/counties",
    response_model=list[CountyRef],
    summary="List NJ counties (for UI dropdowns)",
    description=(
        "Returns the 21 NJ counties from ref.county, ordered "
        "alphabetically by name. Stable shape: only county_fips and "
        "name are exposed."
    ),
)
def get_counties() -> list[CountyRef]:
    """Return all NJ counties for UI dropdown population."""
    with borrow_connection() as conn:
        rows = list_counties(conn, state_code="NJ")
    return [CountyRef.model_validate(r) for r in rows]
