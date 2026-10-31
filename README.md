# Recovery Desk

Airbus Challenge 2 (Autonomous SG 2026). Three independent agents (ATC, Pilot, Ground) negotiate an
airport recovery plan in two rounds. Code generates and checks every plan, a CP-SAT solver supplies
candidates and proofs, and an operations coordinator approves the result on a timeline.

**User:** the airport operations coordinator (duty manager) who today rings ATC, the airline and the ground
handler one by one when a weather hold or late inbound aircraft disrupts a bank of arrivals.

## Run (Python 3.11+)

**Judges must supply their own copy of the organisers' `flights.csv`; it is not included in this repository.** The synthetic demo and bundled example scenarios work without it.

```sh
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
export FLIGHTS_CSV=/path/to/flights.csv                 # Windows: set FLIGHTS_CSV=C:\path\flights.csv
python -m uvicorn recovery.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://localhost:8000/ui**. Pick *169609 (MIA weather demo)* from *Suggested rows*, choose *Rule agents*
or *Claude agents*, then *Run negotiation*. The dashboard shows:

- the disruption, built from the real row, with every synthetic value labelled;
- the outcome against first-come-first-served;
- a timeline of runway, flights, gates and crews (hover for details, click a flight to trace it);
- the two negotiation rounds streaming live, with rejections, mediation and concessions;
- tabs for per-flight changes, every plan option (sortable, click to preview), code checks, the
  operating limits in force, data sources and the benchmark evaluation;
- a *Consensus log* download for each run (every turn's messages, the decision and why);
- *Add disruption* (closure, delay, emergency, turbulence, gate outage) and *Approve plan* at the bottom.

Fonts are bundled, so it works without internet; only Claude mode needs the network. Without the CSV, *Demo scenario* loads a synthetic fixture, or `PUT /scenario` with
`examples/dot-169609-MIA-demo.json`. API docs: http://localhost:8000/docs. Local demo service, no auth; single worker.

## Operating limits (organisers' workbook)

`data/challenge2_operational_constraints.xlsx` is read at start-up (`recovery/constraints.py`); edit it
and restart to change a limit. Every scenario snapshots the values it uses, and the validator, the
heuristic and the CP-SAT solver apply exactly the same rules.

| Workbook item | How it is applied |
|---|---|
| Airport row (322 airports) | Active runways, movements per hour, spare gates (`G1`…), minimum turnaround for the arrival airport |
| Wake separation (FAA, Super/Heavy/Large/Small) | Arrivals: nm converted at 140 kt; departures: workbook seconds; checked within each runway stream |
| Final reserve 30 min / spare fuel 45 min | Hard: land by ETA + 45 min, so the final reserve is never used |
| Contingency 5% of trip | Pilot's preferred buffer: using it is a recorded concession |
| Holding burn 2,400 / 6,500 kg/h | Holding fuel reported per flight and plan; Pilot ranking and solver objective weight it |
| Flight crew duty 13 h | Pilot limit: plans adding holding past duty are rejected; a forced breach goes to the operator |
| Ground crew 8 h shift, 1.5x overtime | Overtime = work after shift end; Ground ranks it at 1.5x |
| Gate change notice 20 min | Hard: a flight can only change gate if it lands 20+ min after the decision |
| Rules of play: 3 turns | 2 turns used (offer, accept); fallback after that, with the reason logged |

Stated changes and assumptions are listed on the dashboard's *Limits* tab and at `GET /constraints`.

## ESP32 boards

Two boards (Flight A, Flight B) can trigger emergencies and show the agreed landing slot. See
`HARDWARE.md`; start with `START_WITH_BOARDS.cmd` so the server listens on the hotspot.

## How a run works

1. **Scenario from a real row.** `recovery/data.py` takes a DOT row with WEATHER_DELAY >15 or
   LATE_AIRCRAFT_DELAY >20 and turns its delay causes into the disruption:
   - LATE_AIRCRAFT_DELAY moves that flight's runway ETA (known before arrival, so no hindsight);
   - WEATHER_DELAY becomes a *constructed* runway closure of that length at the focal ETA (DOT records
     minutes of delay, not closures, so this is labelled as constructed).
   Up to 8 arrivals within ±90 min negotiate (focal airline first). Every other arrival or departure at
   that airport becomes fixed background runway traffic. Tail numbers link to real outbound rotations.
   Recorded actual arrival times are never given to the scheduler. Every field is labelled
   historical, derived or synthetic in `provenance`.
2. **Menu.** Greedy policies, seeded random orders and four CP-SAT objectives (balanced, pilot, ATC,
   ground) produce up to 20 plans. CP-SAT first maximises flights placed; OPTIMAL means it has
   *proved* that number is the most achievable under this model. Every plan passes `engine.validate()`
   before anyone sees it.
3. **Round 1: offers.** Each agent judges plans from its own view against its own limits
   (`agents.POLICY`):

   | Role | Limits (strict = preferred, relaxed = most it will concede) |
   |---|---|
   | ATC | aircraft holding at once, sector throughput (time to last movement), total airborne holding |
   | Pilot | fuel margin (absolute), longest single hold, emergency hold (never relaxed), turbulence hold, missed connections |
   | Ground | crew overtime past shift end, gate changes, turnaround (departure) delay |

   An agent offers every plan within its relaxed limits, marks the ones within its strict limits as
   preferred, and rejects the rest with reason codes. Only plan IDs are shared, never cost weights.
4. **Mediation.** If no plan is preferred by all three, the coordinator asks CP-SAT for a plan that meets
   every stated limit: strict limits first, then relaxed.
5. **Round 2: acceptance.** Each agent sees who offered what and accepts plans, conceding within its
   relaxed band if needed. Concessions are recorded with values and limits. The chosen plan is the one
   accepted by all three with the lowest rank sum.
6. **Fallback.** If agents don't agree, or if an LLM returns errors, malformed JSON or unknown IDs, a
   deterministic rule picks the plan: most flights placed, then fewest outright role rejections, then
   lowest neutral score. This is reported as `FALLBACK`, not as consensus.
7. **Review.** The operator sees the timeline, the trace, concessions and any unresolved flights, then
   approves. Only a complete, current, validated plan can be approved.

**Outcomes:** `CONSENSUS` · `FALLBACK_PLAN` · `PROVEN_INFEASIBLE` (solver proved not every flight fits;
the unresolved flights need an operator decision such as diversion) · `NO_PLAN_FOUND` (search
ended without a proof). The `decision` field records whether a partial plan was agreed or fell back.

## What each brief requirement maps to

| Brief | Implementation |
|---|---|
| ATC: wake separation | Leader/follower wake buffer matrix H/M/L (`models.DEFAULT_WAKE`); validator checks every pair |
| ATC: taxiway blockage | Taxi closures checked on taxi-in and taxi-out |
| ATC: sector throughput | THROUGHPUT_LOSS and HOLDING_STACK limits |
| Pilot: fuel reserves | Hard fuel/holding deadline per flight; FUEL_MARGIN_LOW buffer limit |
| Pilot: safety / emergency | Emergencies scheduled first; EMERGENCY_HELD never relaxed |
| Pilot: turbulence | `motion_event` (button, sensor or `turbulence` disruption) tightens that flight's holding limit |
| Pilot: connections | Missed-connection passengers (synthetic counts) |
| Ground: gates / turnaround / overtime | Gate and crew no-overlap, ground-hold pad capacity, crew shift ends → overtime |
| Revised arrival time / gate / flight plan | Per-flight revised ETA, gate, and speed-reduction (en-route absorption) adjustment |
| Structured messages, constraint checks, consensus log | `agent_message` events (OFFER/ACCEPT, rejections, concessions), SQLite event log, SSE |
| Deterministic non-AI fallback | Fixed-rule agents, CP-SAT and the fallback rule; no LLM needed |
| Under 3 turns, no endless loops | Exactly 2 rounds; 6 model calls in Claude mode; no retries |

## LLM / non-LLM split

Code does the arithmetic, scheduling, constraint checks and proofs. In Claude mode
(`ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, for example `claude-sonnet-5-5`, then `{"mode":"claude"}`), each role is a separate call with its own
system prompt, limits and notes. The model decides which admissible plans to offer or accept, which
concessions to make, and writes the explanation. It can only choose from plans that already pass hard
checks and its own relaxed limits; anything else becomes `AGENT_ERROR` and falls back. Model text is
labelled `summary_source: model` and is never treated as a check.

## Evaluation (actual runs; see `results/`)

```sh
python -m recovery.benchmark --csv flights.csv --count 30 --output results/historical-30.json
python -m recovery.benchmark --output results/synthetic-smoke.json
```

These compare FCFS (status quo), a central planner (same menu, one neutral score), negotiation with a
heuristic-only menu (ablation) and the full system, all on the same validator and workbook limits. Read
`results/*.md` and `TEST_REPORT.md`. Claude-agent numbers are not included; run with `--claude` to add them.

## Limits (say these out loud)

- Simulation only. Limits come from the organisers' workbook, which is itself synthetic ("not real airport
  data"); crews, preferred gates, connections and pad capacity are ours. Passing these checks is not a claim of
  operational safety or certification.
- The supplied DOT file contains only delayed flights, so background traffic is an undercount.
- Wake category is a proxy (stage length ≥2000 mi → HEAVY, else LARGE); DOT has no aircraft type.
- Runways are modelled as arrival and departure streams plus the hourly cap, not individual runways.
  No diversion planning: unresolved flights go to the operator.
- CP-SAT proofs hold for this model (30 s grid) only.

## Tests

```sh
python -m pytest -q                        # 55 tests (+2 that need FLIGHTS_CSV)
FLIGHTS_CSV=flights.csv python -m pytest -q
```

## Files

`recovery/` engine, solver, agents, data importer, API, benchmark · `dashboard/index.html` reference UI
(served at `/ui`) · `dashboard/client.ts` adapter · `examples/` scenarios · `results/` benchmark output ·
`INTEGRATION.md` API contract · `SLIDES_NOTES.md` 3-minute pitch · `firmware/` optional bench hardware (not in the pitch).
