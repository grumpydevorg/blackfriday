"""The Growth round's picks and score, shared by the workflow and the evaluator.

Profit = Launch profit + shop profit + Maya's sales - migration cost, in cents.
Every number here is a modelled price or a verified sale, never an AWS bill.
"""

import re
from typing import Any

MARGIN = 1000  # Each verified sale contributes $10.
LAUNCH_COST = 100
LAUNCH_PROFIT = MARGIN - LAUNCH_COST
RETURN_DRAW = 42  # One controlled draw, shared by the same customer in every release.
MAX_AUDIENCE_TEAMS = 8
MAX_TEAMS = MAX_AUDIENCE_TEAMS + 2  # Plus the two house teams.
# Release A migrates every table; release B skips carts. Modelled cents per migration.
RELEASES = {'A': ('preserve', 200), 'B': ('omit', 100)}
KINDS = {kind: cost for kind, cost in RELEASES.values()}
STORAGE = ('local', 'small', 'large')  # The tiers of virtual_rds.TIERS.
REPLICAS = (1, 2)
TEAM_ID = re.compile(r'[a-z0-9]{3,16}')
NAME_LIMIT = 20


def valid_architecture(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {'storage', 'replicas', 'cache'}
        and value['storage'] in STORAGE and value['replicas'] in REPLICAS
        and type(value['replicas']) is int and type(value['cache']) is bool
    )


def valid_team(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {'id', 'name', 'house', 'release', 'kind', 'architecture'}
        and isinstance(value['id'], str) and TEAM_ID.fullmatch(value['id']) is not None
        and isinstance(value['name'], str) and 1 <= len(value['name']) <= NAME_LIMIT
        and type(value['house']) is bool and value['release'] in RELEASES
        and value['kind'] == RELEASES[value['release']][0] and valid_architecture(value['architecture'])
    )


def validate_teams(value: Any) -> list[dict]:
    if (
        not isinstance(value, list) or not 1 <= len(value) <= MAX_TEAMS
        or not all(valid_team(team) for team in value)
        or len({team['id'] for team in value}) != len(value)
    ):
        raise ValueError('Invalid team list')
    return value


def return_probability(kind: str, checkout_ms: int | None) -> int:
    """Chance Maya shops again next week: 75% after a fast checkout, 50% after a slow one, 5% without her cart."""
    if kind != 'preserve':
        return 5
    return 75 if checkout_ms is not None and checkout_ms <= 2000 else 50


def maya_sales(sold: int, probability: int) -> int:
    """Her checkout now, plus one more sale if the shared draw says she comes back."""
    return sold + (1 if RETURN_DRAW < probability else 0)


def growth_profit(architecture_profit: int, maya: int, kind: str) -> int:
    return architecture_profit + MARGIN * maya - KINDS[kind]


def total_profit(growth: int) -> int:
    return LAUNCH_PROFIT + growth


def rank(rows: list[dict]) -> list[dict]:
    """Highest profit first; equal profit shares a rank, then names sort."""
    ordered = sorted(rows, key=lambda row: (-row['cumulative_cents'], row['name'].casefold(), row['id']))
    ranked, previous = [], None
    for index, row in enumerate(ordered, 1):
        position = ranked[-1]['rank'] if previous == row['cumulative_cents'] else index
        previous = row['cumulative_cents']
        ranked.append({**row, 'rank': position})
    return ranked
