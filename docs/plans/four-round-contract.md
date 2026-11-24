# BLACK FRIDAY four-round MVP — build contract (25 Nov 2026, talk tonight)

Repo `~/Git/personal/blackfriday` (on rick-dev; branch `four-round`), code in `demo/aws/`. Background: `docs/plans/four-round-proposal.md` (original plan; this contract overrides it). Build brief: `docs/plans/four-round-build.md`.
The deployed one-round game is tagged `round-fallback-2026-11-25` and stays deployed as `round`. The new game deploys as a third deployment `fourround`. NOTHING deploys to AWS during the build; the integrator deploys.

## Game
- One phone = one player = one shop (internally still "team": DynamoDB attr `teams_data`, header `X-Team-Id`/`X-Team-Token`; everything players see says "player").
- Four rounds, three options each, from `demo/aws/rounds.json` (single source for web, workflow, model, pages). Round ids: `launch`, `growth`, `blackfriday`, `christmas`. Option ids: launch `small|medium|large`; growth `stay|careful|fast`; blackfriday `ride|autoscale|queue`; christmas `keep|down|commit`. The FIRST option of each round is the default (no pick, late join, bad row).
- `requires`: `autoscale` needs growth pick `careful` or `fast`. The server refuses such a pick (409 with the `reason`), phones grey it out with the reason, bots never draw it without the requirement, and a default never hits it.
- One house player (`rounds.json.house`, id `house`) always plays every default. Bots (names from `rounds.json.bots`, ids = lowercase alnum of the name, flagged `bot: true, house: true`) fill the field up to `min_players` (6) counting audience + house, decided once in the launch plan; bots then draw a random allowed option each round.
- Player cap `player_cap` (100) audience players.
- Names: POST /api/join with an optional name. Blank → server generates `"<Adjective> <Noun>"` from `rounds.json.names`, unique case-folded (NFKC) against players and bot names; on collision append ` 2`, ` 3`… Existing name validation rules stay (1–20 printable chars, no invisible/zalgo). Rename allowed until the launch round closes.
- Joining allowed in phases `ready`, `starting`, and `open` while round is `launch` or `growth`. Late joiners start from the model's `baseline` (an all-default shop).
- Randomness, all drawn inside one checkpointed Durable step per round (`plan-<round>`), via an injected `randbelow`:
  - market: one shared integer per round, uniform in `[market.min, market.max]`.
  - per player: `roll` 0–99 (hit when `roll < option.risk`), `noise` −10…+10 (% demand noise so ties are rare).
  - launch plan only: bots.
- Real vs modelled: Launch (one real MicroVM sale) and the growth migrations (careful = `preserve`, fast = `omit`; each migrated then checked in its own MicroVM) are REAL and all run after Start, before Round 1 opens. Zero MicroVMs after that. If any real step fails, the workflow uses `rounds.json.recorded_evidence` (source `recorded`) and continues. Everything else is modelled. Rows carry `evidence`: `real|recorded|modelled`.

## model.py (pure Python, stdlib only, reads rounds.json beside it)
```python
ROUNDS: list[dict]            # rounds.json['rounds']
ROUND_IDS = ['launch', 'growth', 'blackfriday', 'christmas']
PLAYER_CAP: int; MIN_PLAYERS: int; BOTS: list[str]; HOUSE: dict
def default_option(round_id: str) -> str
def allowed(round_id: str, option: str, picks: dict[str, str]) -> bool   # honours `requires`
def validate_players(value) -> list[dict]
    # TOTAL: never raises for a bad row. Input rows {'id','name','house','option', optional 'bot', optional 'picks'}.
    # Drops rows whose id is not a str matching [a-z0-9]{3,16} or is duplicate; a bad name -> 'Player <id[-4:]>'; a bad/disallowed option -> default.
    # Raises ValueError only if value is not a list.
def plan(round_id: str, players: list[dict], previous: dict | None, randbelow) -> dict
    # players: validated rows for this round (option already defaulted); previous: the previous score() output or None.
    # Launch only: appends the house if missing and bots up to MIN_PLAYERS. Later rounds: carries the bot list from previous.
    # Bots get a random allowed option each round. Returns a JSON-serialisable dict:
    # {'round', 'market': int, 'players': [rows incl. house/bots with final option], 'draws': {id: {'roll': int, 'noise': int}}}
def score(round_id: str, plan: dict, previous: dict | None, evidence: dict) -> dict
    # evidence: {'careful': {...,'cart_kept': bool,'source'}, 'fast': {...}} (used in growth and later rounds' consequences).
    # Players absent from previous['states'] start from previous['baseline'] (or the initial state in launch).
    # Returns JSON-serialisable {'round', 'market', 'states': {id: state}, 'baseline': state, 'bots': [...], 'rows': ranked rows, 'summary': {...}}
```
Row (one per player per round, compact; < 300 bytes JSON each):
```json
{"id":"t1a2b3c","name":"Spiky Lambda","house":false,"bot":false,"rank":1,
 "option":"fast","roll":12,"risk":30,"hit":true,"event":"Saved carts were lost in the migration",
 "sales":120,"slow":10,"failed":5,"cost_cents":3000,"refund_cents":0,
 "profit_cents":120000,"cumulative_cents":250000,"returning":80,"evidence":"real"}
```
`rank`: by cumulative desc; equal cumulative shares a rank; then name casefold, then id.
`summary`: `{"market": int, "options": {option_id: {"players": n, "mean_profit_cents": int, "best_profit_cents": int}}}`.

## Durable workflow (workflow.py)
Event `{'id': run_id}`. Sequence (every operation name unique and round-scoped):
1. step `register-execution` (as today).
2. invoke evaluator `{'id','action':'launch'}` as `launch-shop`; on exception → recorded launch evidence `{'source':'recorded'}`.
3. parallel `migrate-every-option`: invoke `{'id','action':'migrate','option':'careful'}` and `'fast'`; then parallel `check-every-option` with action `compare`. Any failure → that option uses `recorded_evidence[option]`.
4. for each round r: `wait_for_action` publishes phase `open`, `round` = r (plus on r=launch: `launch_data`, `evidence_data`; on r>launch: `results_round` = previous round) and its `callback_id`; callback name `await-close-<r>`. The callback value is `{'action':'close','round': r,'players':[...]}`; reject a different round.
   step `plan-<r>` (model.plan with `secrets.randbelow`), step `score-<r>`, step `publish-results-<r>` (UpdateItem a separate item `pk = results#<run_id>#<r>` with `run_id`, `round`, `data` = JSON of {'round','market','summary','rows'}).
5. After christmas: step `publish-final` sets phase `final`, `results_round` = `christmas`.
Expired callback → phase `expired` (as today). States/baseline/bots live only in checkpointed step results (≤ 256 KB per operation; fine at 101 players).

## DynamoDB arena item (`pk = arena`)
`run_id`, `phase` ∈ `ready|starting|open|closing|final|expired|failed`, `round` (current open round id), `results_round`, `teams_data` (JSON list `{id,name,house,picked_rounds?, token_hash, picks:{round:option}}`), `frozen_data`, `callback_id`, `lease_until`, `execution_arn`, `workflow_arn`, `start_deadline`, `launch_data`, `evidence_data`.
Close is ONE conditional UpdateItem: `phase open → closing`, condition `run_id = :id AND callback_id = :cb AND phase = :open AND round = :round`, copies `teams_data` into `frozen_data`. Picks write the whole roster with condition `run_id = :id AND phase IN (...) AND teams_data = :before AND (round = :round when open)`.

## Web API (web.mjs)
Public:
- `GET /api/state` (headers `X-Team-Id`, `X-Team-Token` optional) → state JSON below.
- `POST /api/join {"name"?: string}` → `{id, token, run_id, name}`.
- `POST /api/rename {id, token, name}` → `{name}` (until launch closes).
- `POST /api/pick {id, token, round, option}` → `{saved: true}`; 409 if `round` ≠ the open round or the option is not allowed.
Presenter (all under `/api/host/`, so host_auth's existing `/api/host/` rule protects them; delete the old `/api/(launch|migrate|compare|reset)` regex): `GET /api/host/state`, `GET /api/host/qr.svg`, `POST /api/host/start`, `POST /api/host/close`, `POST /api/host/reset`, `POST /api/host/remove {id}`. Presenter POSTs need `X-Rehearsal-Action: 1` and `X-Rehearsal-Run: <action_token>`; close also needs `X-Rehearsal-Round: <round>`. Start/close/reset have no body. Reset works in ANY phase (force) when the token matches.
State JSON:
```json
{"mode":"aws","id":"<run>","phase":"open","round":"growth","round_number":2,
 "rounds":[{"id","title","weeks","forecast","market":{"text","unit","min","max"},"options":[{"id","label","blurb","cost_text","risk","dice_text","requires","evidence"}]}],
 "joinable":true,"renamable":false,"player_count":42,"player_cap":100,
 "me":{"id","name","picks":{"launch":"medium","growth":"fast"}} ,
 "results":{"round":"launch","market":112,"summary":{...},"board":{"rows":[...top 10 + own],"total":n,"more":n},"mine":{row}|null} ,
 "evidence":{"launch":{...},"careful":{...},"fast":{...}},
 "message":"..."}
```
Host view adds: `players` ([{id,name,house,bot,picked}] — picked = has a pick for the open round), `public_url`, `auth_mode`, `execution_arn`, `action_token`, `retry_action` ('close' when a close callback lease expired). Phones never get `players`, tokens, `execution_arn`, `action_token`.
Results items are immutable per (run, round): cache them in-process forever by key. Phones poll every 3 s + 0–1 s jitter; after `final`/`expired`/`failed`: host 15 s, phones 30 s.

## Evaluator (evaluator.py, vm_runtime.py)
Events: `{'id','action':'launch'}` (unchanged) and `{'id','action':'migrate'|'compare','option':'careful'|'fast'}`; kind = careful→`preserve`, fast→`omit`. Compare returns `{'option','kind','cart_status','cart_kept','checkout_status','checkout_ms','charges','verified_sales','health_status','history_status','vm_id','vm_state'}`; a missing cart under `omit` is the expected result, not an error (restore the omit handling removed in commit 4d3a27d; see 75a5cbd). No virtual_rds/capacity/scoring imports.

## Files and owners (one owner per file)
- Model: `rounds.json` (fill `model`, may adjust cost_text/risk/blurb text but NOT ids/structure), `model.py`, `test_model.py`.
- Workflow: `workflow.py`, `test_workflow.py`.
- Web: `game.mjs`, `roster.mjs`, `web.mjs`, `host_auth.mjs`, `test_roster.py`, `test_host_auth.py`, new `test_game.py`.
- Pages: `round.js`, `player.html`, `host.html`, `round.css`.
- Deploy+evaluator: `operate.py`, `evaluator.py`, `vm_runtime.py`, `artifacts.yaml`, `rehearsal.yaml`, new `test_package.py`.
- Integrator only: delete `scoring.py`, `options.json`, `test_growth.py`; docs.
Do not edit files you don't own. No deploys, no AWS calls. Run only your own tests: `cd demo && uv run --no-project --with pytest --with boto3==1.44.9 --with pydantic==2.13.4 pytest -q aws/<your tests>` (PYTHONPATH=. if imports fail).
