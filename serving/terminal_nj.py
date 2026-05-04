"""BBG-LIKE-1: ``nj`` shorthand for :mod:`serving.terminal_cli`.

``nj Bergen`` runs ``nj-terminal burden bergen``. Unknown first tokens that are
not NJ counties should use the explicit ``nj-terminal`` entrypoint instead.

Relay mode: if the first argument is a known ``nj-terminal`` subcommand (for
example ``datasets``), arguments are forwarded unchanged so ``nj datasets``
matches ``nj-terminal datasets``.
"""

from __future__ import annotations

from typing import Final

import click

from serving.terminal_cli import CLI_VERSION
from serving.terminal_cli import main as terminal_main

_TERMINAL_SUBCOMMANDS: Final[frozenset[str]] = frozenset({
    "burden",
    "hpi",
    "income",
    "pums-series",
    "pums-burden",
    "pums-burden-county",
    "pums-burden-county-series",
    "acs-burden",
    "burden-latest",
    "counties",
    "datasets",
    "asset",
    "releases",
    "calendar",
    "health",
    "fec-summary",
    "fec-cycles",
    "fec-enums",
    "fec-metrics",
    "fec-metric",
    "fec-risk",
    "fec-risk-entity",
    "fec-money-nj",
    "fec-contributions",
    "fec-candidates",
    "fec-committees",
    "fec-candidate",
    "fec-committee",
    "fec-export-candidates",
    "fec-export-committees",
    "fec-export-contributions",
    "fec-export-money-nj",
})

_METRIC_TO_SUBCOMMAND: Final[dict[str, str]] = {
    "burden": "burden",
    "pums": "burden",
    "pums-burden": "burden",
    "acs-burden": "acs-burden",
    "acs": "acs-burden",
    "tabular": "acs-burden",
    # FHFA House Price Index, base-year normalized.
    "hpi": "hpi",
    "prices": "hpi",
    "fhfa": "hpi",
    # CPI-deflated ACS B19013 median household income.
    "income": "income",
    "mhi": "income",
    "acs-income": "income",
}


def parse_nj_relay_argv(argv: list[str]) -> list[str] | None:
    """Map ``nj`` argv to ``nj-terminal`` argv.

    Returns ``None`` when ``argv`` is empty (caller should print help).
    Raises ``ValueError`` for an unknown *metric* keyword (second token).
    """
    if not argv:
        return None
    if argv[0] in _TERMINAL_SUBCOMMANDS:
        return list(argv)
    county = argv[0]
    rest = argv[1:]
    metric_token: str | None = None
    if rest and not rest[0].startswith("-"):
        metric_token = rest[0]
        rest = rest[1:]
    key = (metric_token or "burden").lower().strip()
    sub = _METRIC_TO_SUBCOMMAND.get(key)
    if sub is None:
        raise ValueError(
            f"Unknown metric {metric_token!r}. "
            f"Try: {', '.join(sorted(set(_METRIC_TO_SUBCOMMAND.keys())))}.",
        )
    return [sub, county, *rest]


@click.command(
    context_settings={
        "help_option_names": ["-h", "--help"],
        "ignore_unknown_options": True,
    },
)
@click.version_option(version=CLI_VERSION, prog_name="nj")
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def main(ctx: click.Context, argv: tuple[str, ...]) -> None:
    """Bloomberg-style shortcut for county metrics (see ``nj-terminal``)."""
    args = list(argv)
    try:
        relay = parse_nj_relay_argv(args)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if relay is None:
        click.echo(ctx.get_help())
        return
    terminal_main.main(args=relay, prog_name="nj-terminal", standalone_mode=True)


if __name__ == "__main__":
    main()
