# Validation report

31 October 2026. Python 3.12/3.13, OR-Tools 9.15. All numbers below come from runs whose raw output is
in `results/`. Operating limits come from the organisers' workbook
(`data/challenge2_operational_constraints.xlsx`).

## Organisers' workbook: what changed

- **Limits now come from the workbook:** airport capacity for all 322 airports, the FAA wake table, final
  reserve and spare fuel, contingency fuel, holding burn rates, flight-crew duty, ground-crew shifts with
  1.5x overtime, gate-change notice and the 3-turn rule. They replace our earlier synthetic values.
- **Stated changes:** the rules of play require any changed limit to be stated. Every interpretation is
  listed on the dashboard's *Limits* tab and at `GET /constraints`. The main ones:
  - runways are modelled as arrival and departure streams plus the hourly cap;
  - arrival nm are converted at 140 kt;
  - crew duty is a Pilot limit, not a hard rule;
  - we use 2 of the 3 allowed turns.
- **A bug found while integrating, and fixed:** a flight whose crew's duty had already ended before its
  ETA could be "placed" by the solver but was rejected by the validator. Proofs then overstated the
  achievable flights (187 "proven" vs 179 achieved).
  - Crew duty is now a Pilot-agent limit, matching the workbook's "Pilot agent" wording.
  - The tests and benchmark now fail if the solver ever produces a plan the validator rejects, and the
    benchmark prints that count (0).

## Earlier fixes from the external code review (still covered by tests)

- **Solver/validator agreement:** no taxiway departure queue; a 30 s grid.
- **Overtime:** counts only work done after the shift ends.
- **Background traffic:** rotation departures are no longer counted twice.
- **Dashboard:** refreshes live on hardware events; the event cursor bug is fixed.
- **No hindsight:** recorded outcomes are withheld from agents.
- **Telemetry test:** a heartbeat is sent before checking the light.

## Automated tests

`python -m pytest -q`: **55 passed, 2 skipped**. The 2 skipped tests need `FLIGHTS_CSV`; with it set,
**57 passed**. New tests for the workbook:
- the workbook loads (ORD row, wake values, fuel and crew rules, 3 turns);
- the hourly cap is respected and checked;
- mixed vs segregated runway streams;
- gate-change notice, the crew-duty Pilot limit and the final reserve;
- contingency fuel is a concession, never a rejection;
- the fallback logs why;
- the consensus-log and constraints endpoints;
- the importer applies the airport row;
- **every heuristic plan is feasible inside the solver model;**
- **solver and validator agree on real windows** (no rejected solver plans, and the menu reaches the proof).

## Benchmark: 30 historical DOT windows (`results/historical-30.md`)

**Selection rule:** random eligible rows with ≥4 arrivals within ±90 min, seed 0. **Demo rows 169609 and
280983 are excluded**: 169609 was chosen by looking at outcomes, so it is not evidence. 191 flights; all
30 proofs optimal; 0 solver plans rejected by the validator.

| | FCFS (status quo) | Central planner | Agents, no solver | **Agents (full system)** |
|---|---|---|---|---|
| Flights placed (proven max 188) | 188 | 188 | 188 | **188** |
| Agreed / fallback | n/a | n/a | 25 / 5 | **29 / 1** |
| Validator violations | 0 | 0 | 0 | **0** |
| Ground-crew overtime (min) | 984 | 588 | 836 | **650** |
| Airborne holding (min) | 88 | 88 | 88 | 88 |
| Holding fuel (kg) | 3,500 | 3,500 | 3,500 | 3,500 |
| Ground wait for a gate (min) | 436 | 466 | 484 | 468 |
| Outbound departure delay (min) | 11,826 | 11,922 | 11,874 | 11,896 |
| Gate changes | 0 | 2 | 16 | 2 |
| Runtime median / p95 (s) | <0.01 | 0.20 / 2.41 | 0.04 / 0.20 | 0.23 / 2.34 |

Paired against FCFS per window, the full system is:
- better on crew overtime in 13 windows, worse in none (−34% in total);
- better on departure delay in 1 window, worse in 6 (+0.6% in total);
- the same on holding and holding fuel in all 30.

**Honest reading.**
- At the workbook's hub capacities (for example 90 movements/hour and 4 runways at ORD), most random
  historical windows are not congested.
- Every method places the proven maximum and holding is identical. The measurable gain is ground-crew
  overtime.
- Departure delay is slightly worse: the agents trade a little turnaround delay for less overtime, inside
  Ground's own limit.
- The solver menu matters: agreement rises from 25/30 to 29/30, and gate changes fall from 16 to 2.

## Benchmark: synthetic stress profiles (`results/synthetic-smoke.md`)

There are 6 profiles at Medium-hub workbook limits: ordinary, long closure, gate outages, one crew,
little spare fuel, and emergency plus taxiway closure. All 6 proofs are optimal. Against FCFS:
- flights placed: 32/36 vs 31/36;
- airborne holding: −24%;
- holding fuel: −10% (28,247 → 25,315 kg);
- departure delay: −36%.

Agents agreed in 6/6, with mediation in 6/6. These are fixtures, not evidence about real operations.

## Demo cases

- **169609 (MIA, 9 Jun 2015, 36-min weather delay as a closure):** all three agree, with a Pilot
  concession. AA2447 holds about 1 min longer than the Pilot prefers and uses 12 s of contingency fuel;
  the final reserve is untouched. Against FCFS:
  - holding: 174 → 137 min;
  - holding fuel: 8,702 → 7,120 kg;
  - crew overtime: 38 → 3 min;
  - departure delay: 726 → 658 min.
- **3125 (MSP, 73-min closure, failure case):** CP-SAT proves at most 5 of 8 flights can land before using
  their final reserve. The Pilot rejects all 9 plans because the crews would pass 13 h duty, so the
  fallback decides. The log says why, approval is blocked, and the 3 unresolved flights go to the operator.

## Claude agents

Not re-run after the review fixes and the workbook change. To add the column:

```sh
ANTHROPIC_API_KEY=... CLAUDE_MODEL=claude-sonnet-5-5 python -m recovery.benchmark --csv flights.csv --count 30 --claude --output results/historical-30.json
```

## Not tested

- **Claude mode after these changes.**
- **Firmware:** the friend's ESP32 boards are tested only through the simulator.
- **Real-world validity:** the workbook itself is labelled "synthetic planning limits, not real airport data".
