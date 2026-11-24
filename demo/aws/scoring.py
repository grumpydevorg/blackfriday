"""The Growth round's options and score, shared by the workflow and the evaluator.

Profit = Launch profit + shop profit + Maya's sales - migration cost, in cents.
Every number here is a modelled price or a verified sale, never an AWS bill.

Each team picks one option. The option is really migrated and checked once, in its own
MicroVMs, and every team that picked it inherits that result. Each team's migration may
then break: a risk percentage is rolled once per team at Close. A broken migration loses
the Black Friday wave (the shop's verified sales count as 0, so the shop earns minus its
modelled cost); the Launch sale, Maya's sales and the migration cost really happened and
are unchanged.
"""

import json
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

MARGIN = 1000  # Each verified sale contributes $10.
LAUNCH_COST = 100
LAUNCH_PROFIT = MARGIN - LAUNCH_COST
RETURN_DRAW = 42  # One controlled draw, shared by the same customer in every release.
MAX_AUDIENCE_TEAMS = 200  # A safety cap: the DynamoDB item and the Durable callback stay far under their limits.
MAX_TEAMS = MAX_AUDIENCE_TEAMS + 2  # Plus the two house teams.
MIN_TEAMS = 6  # Fewer than this at Close and bots fill the board.
KIND = 'preserve'  # Every option really migrates every table.
MIGRATION_COST = 200
# One table for the round: the pages, the web function and the scoring all read it.
_TABLE = json.loads((Path(__file__).parent / 'options.json').read_text())
OPTIONS: dict[str, dict[str, Any]] = _TABLE['options']
BOTS: list[str] = _TABLE['bots']
STORAGE = ('local', 'small', 'large')  # The tiers of virtual_rds.TIERS.
REPLICAS = (1, 2)
TEAM_ID = re.compile(r'[a-z0-9]{3,16}')
NAME_LIMIT = 20


def valid_team(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {'id', 'name', 'house', 'option'}
        and isinstance(value['id'], str) and TEAM_ID.fullmatch(value['id']) is not None
        and isinstance(value['name'], str) and 1 <= len(value['name']) <= NAME_LIMIT
        and type(value['house']) is bool and value['option'] in OPTIONS
    )


def validate_teams(value: Any) -> list[dict]:
    if (
        not isinstance(value, list) or not 1 <= len(value) <= MAX_TEAMS
        or not all(valid_team(team) for team in value)
        or len({team['id'] for team in value}) != len(value)
    ):
        raise ValueError('Invalid team list')
    return value


def draw_risks(teams: list[dict], randbelow: Callable[[int], int] = secrets.randbelow) -> dict[str, dict]:
    """Roll once per team, in team order. broke = roll < risk. The only randomness in the round."""
    rolls = {}
    for team in teams:
        risk = OPTIONS[team['option']]['risk']
        roll = randbelow(100)
        rolls[team['id']] = {'option': team['option'], 'risk': risk, 'roll': roll, 'broke': roll < risk}
    return rolls


def bot_id(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


def plan_round(teams: list[dict], randbelow: Callable[[int], int] = secrets.randbelow) -> dict[str, Any]:
    """Everything the round decides by chance, drawn once: bots for a thin room, then one roll per team.

    Below MIN_TEAMS the board is padded with bots (house teams too, with a random option each) so
    the demo always has a field. Bots take names from a short fixed list, in order.
    """
    ids = list(OPTIONS)
    everyone = list(teams)
    for name in BOTS[:max(0, MIN_TEAMS - len(everyone))]:
        everyone.append({'id': bot_id(name), 'name': name, 'house': True, 'bot': True, 'option': ids[randbelow(len(ids))]})
    return {'teams': everyone, 'risks': draw_risks(everyone, randbelow)}


def architecture_profit(capacity: dict, option: str, broke: bool) -> int:
    """The shop's profit on the Black Friday wave, after the option's extra cost."""
    extra = OPTIONS[option]['extra_cost_cents']
    if broke:
        return -(capacity['cost_cents'] + extra)
    return capacity['profit_cents'] - extra


def return_probability(checkout_ms: int | None) -> int:
    """Chance Maya shops again next week: 75% after a fast checkout, 50% after a slow one."""
    return 75 if checkout_ms is not None and checkout_ms <= 2000 else 50


def shop_result(capacity: dict, option: str, broke: bool) -> dict:
    """What the Black Friday wave did to this team's shop, as the scoreboard shows it."""
    extra = OPTIONS[option]['extra_cost_cents']
    return {
        **capacity, 'modelled_verified_sales': capacity['verified_sales'],
        'verified_sales': 0 if broke else capacity['verified_sales'],
        'extra_cost_cents': extra, 'cost_cents': capacity['cost_cents'] + extra,
        'profit_cents': architecture_profit(capacity, option, broke),
    }


def maya_sales(sold: int, probability: int) -> int:
    """Her checkout now, plus one more sale if the shared draw says she comes back."""
    return sold + (1 if RETURN_DRAW < probability else 0)


def growth_profit(shop_profit: int, maya: int) -> int:
    return shop_profit + MARGIN * maya - MIGRATION_COST


def total_profit(growth: int) -> int:
    return LAUNCH_PROFIT + growth


def team_row(team: dict, risk: dict, shared: dict) -> dict:
    """One scoreboard row: the option's real result, plus this team's own roll applied on top."""
    wave = shop_result(shared['architecture'], team['option'], risk['broke'])
    profit = growth_profit(wave['profit_cents'], shared['maya_sales'])
    return {
        'id': team['id'], 'name': team['name'], 'house': team['house'], 'bot': bool(team.get('bot')),
        'option': team['option'], 'risk': risk['risk'], 'roll': risk['roll'], 'broke': risk['broke'],
        'cart_status': shared['cart_status'], 'maya_sales': shared['maya_sales'],
        'returns_next_week': shared['returns_next_week'], 'return_probability': shared['return_probability'],
        'migration_cost_cents': MIGRATION_COST,
        'architecture': {key: wave[key] for key in ('verified_sales', 'cost_cents', 'extra_cost_cents', 'profit_cents')},
        'profit_cents': profit, 'cumulative_cents': total_profit(profit),
    }


def rank(rows: list[dict]) -> list[dict]:
    """Highest profit first; equal profit shares a rank, then names sort."""
    ordered = sorted(rows, key=lambda row: (-row['cumulative_cents'], row['name'].casefold(), row['id']))
    ranked, previous = [], None
    for index, row in enumerate(ordered, 1):
        position = ranked[-1]['rank'] if previous == row['cumulative_cents'] else index
        previous = row['cumulative_cents']
        ranked.append({**row, 'rank': position})
    return ranked
