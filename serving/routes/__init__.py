"""Route modules for the serving API.

Each module exposes an :class:`fastapi.APIRouter` named ``router``,
which the top-level :mod:`serving.app` registers. Splitting routes by
concern keeps the per-file surface area small and discoverable.
"""

from __future__ import annotations
