"""The four-round BLACK FRIDAY model: pure Python, stdlib only, no I/O beyond reading rounds.json.

One player = one shop. Every round is one pure function of (shop state, pick, market, dice):
customers arrive, the shop serves them fast up to its capacity, slowly in a band above it and
not at all beyond that, and each verified sale (fast or slow) earns the margin. Everything is a
modelled price or a count of customers, never an AWS bill; only the growth migrations' cart
check is real evidence (see `score`).

`python model.py balance` is the balance harness: it plays every legal path through the four
rounds in thousands of random worlds and checks that no option is pointless.
"""

import json
import re
import secrets
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

_TABLE = json.loads((Path(__file__).parent / 'rounds.json').read_text())
ROUNDS: list[dict] = _TABLE['rounds']
ROUND_IDS: list[str] = [rnd['id'] for rnd in ROUNDS]
PLAYER_CAP: int = _TABLE['player_cap']
MIN_PLAYERS: int = _TABLE['min_players']
BOTS: list[str] = _TABLE['bots']
HOUSE: dict = _TABLE['house']
RECORDED: dict = _TABLE['recorded_evidence']
PARAMS: dict = _TABLE['model']

_BY_ID = {rnd['id']: rnd for rnd in ROUNDS}
_OPTIONS = {rnd['id']: {opt['id']: opt for opt in rnd['options']} for rnd in ROUNDS}
PLAYER_ID = re.compile(r'[a-z0-9]{3,16}')
NAME_LIMIT = 20
ALL_OPTION_IDS = {option for options in _OPTIONS.values() for option in options}
# Options whose evidence can come from a real MicroVM: option id -> round id. rounds.json is the only list.
REAL_OPTIONS: dict[str, str] = {opt['id']: rnd['id'] for rnd in ROUNDS for opt in rnd['options']
                                if opt['evidence'] == 'real'}
EVIDENCE_SOURCES = ('real', 'recorded', 'modelled')
if set(REAL_OPTIONS) != set(RECORDED):
    raise ValueError('rounds.json: recorded_evidence must name exactly the options whose evidence is real')


def bot_id(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


# ---------------------------------------------------------------------------------------------
# Options and validation
# ---------------------------------------------------------------------------------------------


def default_option(round_id: str) -> str:
    return _BY_ID[round_id]['options'][0]['id']


def option_label(option: str) -> str:
    """The rounds.json label of an option id (option ids are unique across rounds)."""
    return next(opt['label'] for options in _OPTIONS.values() for key, opt in options.items() if key == option)


def round_title(round_id: str) -> str:
    return _BY_ID[round_id]['title']


def allowed(round_id: str, option: str, picks: dict[str, str]) -> bool:
    """Is `option` a legal pick in `round_id` given the earlier picks? Honours `requires`."""
    entry = _OPTIONS.get(round_id, {}).get(option) if isinstance(option, str) else None
    if entry is None:
        return False
    need = entry['requires']
    return need is None or picks.get(need['round']) in need['options']


def _good_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not 1 <= len(name) <= NAME_LIMIT:
        return None
    run = 0
    for char in name:
        category = unicodedata.category(char)
        if category[0] == 'C' or category in ('Zl', 'Zp') or char in '\u3164\u115f\u1160\uffa0':
            return None
        run = run + 1 if category[0] == 'M' else 0
        if run >= 3:
            return None
    return name


def _clean_picks(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    picks: dict[str, str] = {}
    for round_id in ROUND_IDS:  # Round order, so a later pick is checked against the earlier ones.
        option = value.get(round_id)
        if isinstance(option, str) and option in _OPTIONS[round_id] and allowed(round_id, option, picks):
            picks[round_id] = option
    return picks


def validate_players(value: Any, round_id: str | None = None) -> list[dict]:
    """Total: sanitise the roster rows. Raises ValueError only when `value` is not a list.

    Drops rows whose id is not [a-z0-9]{3,16} or repeats an earlier id; a bad name becomes
    'Player <last 4 of id>'; a bad `option` is defaulted. `option` is this round's pick. When
    `round_id` is given it is checked with `allowed` and defaulted to that round's default;
    without it the option can only be checked as a known option id, and a bad one becomes None
    (`plan` applies the round's default).
    """
    if not isinstance(value, list):
        raise ValueError('Players must be a list')
    rows: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        pid = item.get('id')
        if not isinstance(pid, str) or PLAYER_ID.fullmatch(pid) is None or pid in seen:
            continue
        seen.add(pid)
        picks = _clean_picks(item.get('picks'))
        option = item.get('option')
        if round_id in _OPTIONS:
            rest = {k: v for k, v in picks.items() if k != round_id}
            option = option if allowed(round_id, option, rest) else default_option(round_id)
        elif not (isinstance(option, str) and option in ALL_OPTION_IDS):
            option = None
        rows.append({
            'id': pid, 'name': _good_name(item.get('name')) or f'Player {pid[-4:]}',
            'house': item.get('house') is True, 'bot': item.get('bot') is True,
            'option': option, 'picks': picks,
        })
    return rows


# ---------------------------------------------------------------------------------------------
# The shop: one pure step per round
# ---------------------------------------------------------------------------------------------

INITIAL: dict[str, Any] = {
    'server': None, 'data': 'disk', 'cap': 0, 'run': 0, 'hist_lost': False,
    'fast': 0, 'slow': 0, 'failed': 0, 'bad': 0, 'cum': 0, 'picks': {},
}


def _market_factor(rnd: dict, market: int) -> tuple[int, int]:
    """(numerator, denominator): '%' markets are market/100, '×' spikes are market/1."""
    return (market, 100) if rnd['market']['unit'] == '%' else (market, 1)


def returning(state: dict, bad: int) -> tuple[int, int]:
    """Customers that come back after the round in `state`: 75% fast + 50% slow + 20% failed, 5% of `bad`.

    Returns (returning, of_which_bad). `bad` customers lost their carts/history or were double-charged.
    """
    rate = PARAMS['return_pct']
    fast, slow, failed = state['fast'], state['slow'], state['failed']
    total = fast + slow + failed
    if total == 0:
        return 0, 0
    bad = min(bad, total)
    back = ((fast * rate['fast'] + slow * rate['slow'] + failed * rate['failed']) * (total - bad)
            + total * rate['lost'] * bad) // (total * 100)
    return back, bad * rate['lost'] // 100


def _serve(demand: int, cap: int) -> tuple[int, int, int]:
    """Fast sales up to `cap`, a slow band of slow_pct% of `cap` above it, failures beyond that."""
    fast = min(demand, cap)
    slow = min(demand - fast, cap * PARAMS['slow_pct'] // 100)
    return fast, slow, demand - fast - slow


def _cap_run(state: dict, server: str | None, data: str) -> tuple[int, int]:
    """(capacity per week, running cost per week) of a server size on a database.

    On the server's own disk the database is the limit from the growth round on. A shared database
    lifts that ceiling: the shop runs `shared_pct`% of the servers (capacity and cost), plus the database.
    """
    box = PARAMS['servers'].get(server)
    cap, run = (box['cap'], box['cost']) if box else (0, 0)
    if data == 'disk':
        if 'growth' in state['picks']:
            cap = min(cap, PARAMS['disk_ceiling'])
    else:
        cap, run = cap * PARAMS['shared_pct'] // 100, run * PARAMS['shared_pct'] // 100
        run += PARAMS['database'][state['picks']['growth']]['week']
    return cap, run


def step(state: dict, round_id: str, option: str, market: int, roll: int, noise: int,
         evidence: dict) -> tuple[dict, dict]:
    """Play one round for one shop: (state, pick, market, dice) -> (state', result). Pure."""
    rnd = _BY_ID[round_id]
    entry = _OPTIONS[round_id][option]
    weeks = PARAMS['weeks'][round_id]
    margin = PARAMS['margin_cents']
    hit = roll < entry['risk']
    new = dict(state, picks={**state['picks'], round_id: option})
    cost = refund = dc = 0
    lost = False  # saved carts were lost in this round's migration
    bad_in = new['bad']
    event = ''

    num, den = _market_factor(rnd, market)
    fresh = PARAMS['arrivals'][round_id] * weeks * num * (100 + noise) // (den * 100)

    if round_id == 'launch':
        new['server'] = option
        new['cap'], new['run'] = _cap_run(new, option, 'disk')
        event = f"Launched on a {option} server"
    elif round_id == 'growth':
        db = PARAMS['database'][option]
        new['data'] = 'disk' if option == 'stay' else 'shared'
        new['cap'], new['run'] = _cap_run(new, new['server'], new['data'])
        cost += db['migration']
        if option == 'stay':
            event = 'Stayed on the disk database'
        elif option == 'careful':
            lost = evidence.get('careful', {}).get('cart_kept', True) is False
            event = 'Carts lost in the migration' if lost else 'Migrated, every cart kept'
        else:
            lost = hit and evidence.get('fast', {}).get('cart_kept', False) is False
            if lost:
                event = 'Fumbled migration: saved carts lost'
            else:
                event = 'Dice hit, carts survived' if hit else 'Migrated fast, no trouble'
        if lost:
            new['hist_lost'] = True
            bad_in = state['fast'] + state['slow'] + state['failed']
    elif round_id == 'christmas' and state['hist_lost']:
        total = state['fast'] + state['slow'] + state['failed']
        bad_in = max(bad_in, total * PARAMS['christmas']['history_lost_pct'] // 100)

    back_in, bad_back = returning(state, bad_in) if state['server'] else (0, 0)
    demand = fresh + back_in
    cap_week, run_week = new['cap'], new['run']
    # The autoscale ramp below is billed twice: once as `cost`, once again through `run_week`. Billing it once makes
    # `python model.py balance` FAIL (autoscale on the best path in 93% of worlds), so rounds.json is tuned to this.

    if round_id == 'blackfriday':
        conf = PARAMS['blackfriday']
        cap = cap_week * weeks
        if option == 'ride':
            fast, slow, failed = _serve(demand, cap)
            event = 'Rode the spike'
        elif option == 'queue':
            fast = min(demand, cap)
            slow, failed = demand - fast, 0
            # Queue workers cost per queued checkout, and the cost per checkout grows with the queue's depth.
            cost += conf['queue_week'] * weeks + slow * conf['queue_unit'] * (100 + slow * 100 // max(cap, 1)) // 100
            event = 'Queued, nobody failed'
            if hit:
                dc = slow * conf['double_pct'] // 100
                refund = dc * conf['refund_cents']
                event = 'Retries double-charged'
        else:  # autoscale: capacity follows demand, cost follows the servers added.
            need = -(-demand // weeks)
            added = max(0, need - cap_week)
            live = weeks * 100
            if hit:
                late = conf['late_pct']
                early = demand * late // 100
                f1, s1, x1 = _serve(early, cap_week * weeks * late // 100)
                f2, s2, x2 = _serve(demand - early, need * weeks)
                fast, slow, failed = f1 + f2, s1 + s2, x1 + x2
                live = weeks * (100 - late)
                event = 'Scaled too late'
            else:
                fast, slow, failed = _serve(demand, need * weeks)
                event = 'Scaled with the traffic'
            cost += added * conf['scale_unit_week'] * live // 100
            cap_week = max(cap_week, need)
            run_week += added * conf['scale_unit_week']
        new['cap'], new['run'] = cap_week, run_week
    elif round_id == 'christmas':
        conf = PARAMS['christmas']
        if option == 'keep':
            event = 'Kept Black Friday capacity'
        elif option == 'down':
            small = min(cap_week, conf['down_cap'])
            run_week = run_week * small // cap_week if cap_week else 0
            cap_week = small
            event = 'Scaled down to base'
        else:
            cap_week = conf['commit_cap']  # A managed commitment: its capacity and its price do not depend on the old disk.
            run_week = PARAMS['database'][new['picks'].get('growth', 'stay')]['week']
            cost += conf['commit_cap'] * conf['commit_unit_week'] * weeks
            event = 'Committed to capacity'
        fast, slow, failed = _serve(demand, cap_week * weeks)
    else:
        fast, slow, failed = _serve(demand, cap_week * weeks)

    cost += run_week * weeks
    sold = fast + slow
    profit = margin * sold - cost - refund
    flagged = min(bad_back + dc, sold + failed)
    new.update(fast=fast, slow=slow, failed=failed, bad=flagged, cum=state['cum'] + profit)
    next_back = returning(new, flagged)[0]
    return new, {
        'hit': hit, 'outcome': 'lost_carts' if lost else 'hit' if hit else 'held', 'event': event, 'sales': fast,
        'slow': slow, 'failed': failed, 'cost_cents': cost, 'refund_cents': refund, 'profit_cents': profit,
        'returning': next_back, 'demand': demand,
    }


# ---------------------------------------------------------------------------------------------
# plan and score
# ---------------------------------------------------------------------------------------------


def _random_option(round_id: str, picks: dict[str, str], randbelow: Callable[[int], int]) -> str:
    legal = [opt['id'] for opt in _BY_ID[round_id]['options'] if allowed(round_id, opt['id'], picks)]
    return legal[randbelow(len(legal))]


def plan(round_id: str, players: list[dict], previous: dict | None, randbelow: Callable[[int], int]) -> dict:
    """Everything chance decides for the round, drawn in a fixed order: market, bots' picks, then dice.

    A missing/bad option is defaulted here too, so `players` may come straight from validate_players.
    The house always plays the default; bots draw a random allowed option.
    """
    rnd = _BY_ID[round_id]
    low, high = rnd['market']['min'], rnd['market']['max']
    market = low + randbelow(high - low + 1)
    states = (previous or {}).get('states', {})

    rows: list[dict] = []
    seen: set[str] = set()
    for player in players:
        if player['id'] in seen:
            continue
        seen.add(player['id'])
        picks = {k: v for k, v in (states.get(player['id'], {}).get('picks') or player.get('picks') or {}).items()
                 if k != round_id and k in _OPTIONS}
        option = player.get('option')
        if player.get('house') and not player.get('bot') or not allowed(round_id, option, picks):
            option = default_option(round_id)
        rows.append({**player, 'option': option, 'picks': {**picks, round_id: option},
                     'bot': player.get('bot') is True})

    if HOUSE['id'] not in seen:  # The house plays every round, whether or not the roster carries it.
        seen.add(HOUSE['id'])
        picks = {rid: default_option(rid) for rid in ROUND_IDS[:ROUND_IDS.index(round_id) + 1]}
        rows.append({'id': HOUSE['id'], 'name': HOUSE['name'], 'house': True, 'bot': False,
                     'option': picks[round_id], 'picks': picks})
    if round_id == ROUND_IDS[0]:
        bots = [{'id': bot_id(name), 'name': name} for name in BOTS if bot_id(name) not in seen]
        bots = bots[:max(0, MIN_PLAYERS - len(rows))]
    else:
        bots = [bot for bot in (previous or {}).get('bots', [])]
    for bot in bots:
        if bot['id'] in seen:
            continue
        seen.add(bot['id'])
        picks = {k: v for k, v in states.get(bot['id'], {}).get('picks', {}).items() if k != round_id}
        option = _random_option(round_id, picks, randbelow)
        rows.append({'id': bot['id'], 'name': bot['name'], 'house': True, 'bot': True,
                     'option': option, 'picks': {**picks, round_id: option}})

    draws = {}
    for row in rows:
        roll = randbelow(100)
        draws[row['id']] = {'roll': roll, 'noise': randbelow(21) - 10}
    return {'round': round_id, 'market': market, 'players': rows, 'draws': draws}


def _evidence_source(round_id: str, option: str, evidence: dict) -> str:
    declared = _OPTIONS[round_id][option]['evidence']
    if declared != 'real':
        return declared
    return 'real' if evidence.get(option, {}).get('source') == 'real' else 'recorded'


def _ranked(rows: list[dict]) -> list[dict]:
    """Cumulative profit descending; equal cumulative shares a rank; then name casefold, then id."""
    ordered = sorted(rows, key=lambda row: (-row['cumulative_cents'], row['name'].casefold(), row['id']))
    last_cum = last_rank = None
    out = []
    for index, row in enumerate(ordered, 1):
        rank = last_rank if row['cumulative_cents'] == last_cum else index
        last_cum, last_rank = row['cumulative_cents'], rank
        out.append({'id': row['id'], 'name': row['name'], 'house': row['house'], 'bot': row['bot'],
                    'rank': rank, **{k: row[k] for k in _ROW_KEYS}})
    return out


_ROW_KEYS = ('option', 'roll', 'risk', 'hit', 'outcome', 'event', 'sales', 'slow', 'failed', 'cost_cents',
             'refund_cents', 'profit_cents', 'cumulative_cents', 'returning', 'evidence')


def _evidence(evidence: dict) -> dict:
    """The evidence per real option; a missing or malformed option falls back to its recorded evidence.
    Real evidence must say whether the cart survived: a real result without `cart_kept` is rejected."""
    out = {}
    for option, recorded in RECORDED.items():
        got = evidence.get(option) if isinstance(evidence, dict) else None
        if isinstance(got, dict) and got.get('source') == 'real' and not isinstance(got.get('cart_kept'), bool):
            raise ValueError(f'real evidence for {option} must say whether the cart survived (cart_kept)')
        out[option] = got if isinstance(got, dict) else recorded
    return out


def score(round_id: str, plan: dict, previous: dict | None, evidence: dict) -> dict:
    """Play the planned round for every shop and rank them. Absent shops start from the baseline."""
    evidence = _evidence(evidence)
    previous = previous or {}
    states = previous.get('states', {})
    baseline = previous.get('baseline') or INITIAL
    market = plan['market']
    default = default_option(round_id)

    new_states: dict[str, dict] = {}
    rows: list[dict] = []
    for player in plan['players']:
        pid, option = player['id'], player['option']
        draw = plan['draws'][pid]
        start = states.get(pid) or baseline
        if not allowed(round_id, option, start['picks']):
            option = default
        state, out = step(start, round_id, option, market, draw['roll'], draw['noise'], evidence)
        new_states[pid] = state
        rows.append({
            'id': pid, 'name': player['name'], 'house': player['house'], 'bot': player.get('bot') is True,
            'option': option, 'roll': draw['roll'], 'risk': _OPTIONS[round_id][option]['risk'],
            'hit': out['hit'], 'outcome': out['outcome'], 'event': out['event'], 'sales': out['sales'],
            'slow': out['slow'],
            'failed': out['failed'], 'cost_cents': out['cost_cents'], 'refund_cents': out['refund_cents'],
            'profit_cents': out['profit_cents'], 'cumulative_cents': state['cum'], 'returning': out['returning'],
            'evidence': _evidence_source(round_id, option, evidence),
        })
    new_baseline, _ = step(baseline, round_id, default, market, 99, 0, evidence)

    summary: dict[str, Any] = {opt['id']: {'players': 0, 'mean_profit_cents': 0, 'best_profit_cents': 0}
                               for opt in _BY_ID[round_id]['options']}
    profits: dict[str, list[int]] = {opt: [] for opt in summary}
    for row in rows:
        profits[row['option']].append(row['profit_cents'])
    for opt, values in profits.items():
        if values:
            summary[opt] = {'players': len(values), 'mean_profit_cents': sum(values) // len(values),
                            'best_profit_cents': max(values)}
    return {
        'round': round_id, 'market': market, 'states': new_states, 'baseline': new_baseline,
        'bots': [{'id': p['id'], 'name': p['name']} for p in plan['players'] if p.get('bot')],
        'rows': _ranked(rows), 'summary': {'market': market, 'options': summary},
    }


# ---------------------------------------------------------------------------------------------
# Balance harness
# ---------------------------------------------------------------------------------------------


def _worlds(count: int, rng: Any) -> list[list[tuple[int, int, int]]]:
    worlds = []
    for _ in range(count):
        world = []
        for round_id in ROUND_IDS:
            market = _BY_ID[round_id]['market']
            world.append((rng.randint(market['min'], market['max']), rng.randrange(100), rng.randint(-10, 10)))
        worlds.append(world)
    return worlds


def path_totals(worlds: int, seed: int, cart_kept: bool) -> tuple[list[tuple[str, ...]], list[list[int]]]:
    """Cumulative profit of every legal path in `worlds` random worlds: (paths, totals[world][path]).

    Paths share their prefixes (all legal picks of round 1, then of round 2 ...), so each step runs once.
    """
    import random

    rng = random.Random(seed)
    evidence = _evidence({'fast': {'cart_kept': cart_kept}, 'careful': {'cart_kept': True}})
    totals: list[list[int]] = []
    for world in _worlds(worlds, rng):
        states: dict[tuple[str, ...], dict] = {(): INITIAL}
        for round_id, (market, roll, noise) in zip(ROUND_IDS, world):
            states = {path + (opt['id'],): step(state, round_id, opt['id'], market, roll, noise, evidence)[0]
                      for path, state in states.items() for opt in _BY_ID[round_id]['options']
                      if allowed(round_id, opt['id'], state['picks'])}
        totals.append([state['cum'] for state in states.values()])
    return list(states), totals


def measure(worlds: int = 4000, seed: int = 20261125, cart_kept: bool = False) -> dict[str, Any]:
    """Play every legal path through `worlds` random worlds and summarise how the options compare."""
    from statistics import mean, pvariance

    paths, totals = path_totals(worlds, seed, cart_kept)

    # Share of worlds in which each option is on the best path.
    wins = {rid: {opt['id']: 0 for opt in _BY_ID[rid]['options']} for rid in ROUND_IDS}
    for row in totals:
        best = row.index(max(row))
        for rid, option in zip(ROUND_IDS, paths[best]):
            wins[rid][option] += 1
    shares = {rid: {opt: 100 * n / worlds for opt, n in counts.items()} for rid, counts in wins.items()}

    # Paths that another single path beats in every world; `beaten` is each path's worst share of worlds lost to one path.
    columns = [[row[p] for row in totals] for p in range(len(paths))]
    dominated, beaten = [], []
    for p, mine in enumerate(columns):
        worst, worst_q = 0, p
        for q, other in enumerate(columns):
            if q != p:
                lost = sum(a > b for a, b in zip(other, mine))
                if lost > worst:
                    worst, worst_q = lost, q
        beaten.append(worst / worlds)
        if worst == worlds:
            dominated.append((paths[p], paths[worst_q]))

    # Variance between paths explained by each round's option (main effects, averaged over worlds).
    effects = {}
    for index, rid in enumerate(ROUND_IDS):
        groups: dict[str, list[int]] = {}
        for p, path in enumerate(paths):
            groups.setdefault(path[index], []).append(p)
        effects[rid] = mean(pvariance([mean(row[p] for p in members) for members in groups.values()])
                            for row in totals)
    sums = sum(effects.values()) or 1
    default = tuple(default_option(rid) for rid in ROUND_IDS)
    index = paths.index(default)
    means = [mean(column) for column in columns]
    return {
        'worlds': worlds, 'seed': seed, 'cart_kept': cart_kept, 'paths': len(paths), 'shares': shares,
        'dominated': dominated, 'beaten': beaten, 'effects': {rid: 100 * v / sums for rid, v in effects.items()},
        'default': default, 'default_mean': means[index],
        'default_percentile': mean(100 * sum(1 for value in row if value < row[index]) / (len(paths) - 1)
                                   for row in totals),
        'best_mean': max(means), 'worst_mean': min(means),
    }


def balance(worlds: int = 4000, seed: int = 20261125, cart_kept: bool = False) -> tuple[bool, str]:
    got = measure(worlds, seed, cart_kept)
    lines = [f"balance: {worlds} worlds x {got['paths']} legal paths (autoscale needs a shared database), "
             f"seed {seed}, fast migration cart_kept={cart_kept}"]
    ok = True
    for rid in ROUND_IDS:
        shares = got['shares'][rid]
        good = all(share >= 10 for share in shares.values())
        ok &= good
        lines.append(f'  on the best path {rid:<11} ' + '  '.join(f'{o} {s:5.1f}%' for o, s in shares.items())
                     + ('' if good else '   FAIL (<10%)'))
    dominated = got['dominated']
    ok &= not dominated
    lines.append(f'  paths beaten by another path in every world: {len(dominated)}'
                 + ''.join(f'\n    {"/".join(p)} < {"/".join(q)}' for p, q in dominated))
    bf = got['effects']['blackfriday']
    ok &= bf <= 50
    lines.append('  share of between-path variance: ' + '  '.join(f'{r} {v:4.1f}%' for r, v in got['effects'].items())
                 + ('' if bf <= 50 else '   FAIL (blackfriday >50%)'))
    good = got['default_mean'] > 0 and 25 <= got['default_percentile'] <= 75
    ok &= good
    lines.append(f"  all-default {'/'.join(got['default'])}: mean ${got['default_mean'] / 100:,.0f}, "
                 f"mean path percentile {got['default_percentile']:.0f}" + ('' if good else '   FAIL (want >$0 and 25-75)'))
    lines.append(f"  mean total over paths: best ${got['best_mean'] / 100:,.0f}, worst ${got['worst_mean'] / 100:,.0f}")
    lines.append('BALANCE ' + ('PASS' if ok else 'FAIL'))
    return ok, '\n'.join(lines)


if __name__ == '__main__':
    if sys.argv[1:2] != ['balance']:
        sys.exit('usage: model.py balance [worlds] [seed] [cart_kept]')
    args = sys.argv[2:]
    passed, report = balance(int(args[0]) if args else 4000, int(args[1]) if len(args) > 1 else 20261125,
                             args[2] == 'kept' if len(args) > 2 else False)
    print(report)
    sys.exit(0 if passed else 1)
