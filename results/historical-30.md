# Benchmark results

**Historical DOT windows (seed 0): random eligible rows with >=4 arrivals within +/-90 min; demo rows [169609, 280983] excluded. Limits from the organisers' workbook; other operations synthetic (see provenance).** — 30 scenarios, 191 flights. Simulation results; not real-world savings.

CP-SAT upper bound (proven maximum placeable flights): 188/191 (30/30 scenarios proven optimal).

Solver plans rejected by the validator: 0 (must be 0; otherwise solver and validator disagree).

| Metric | fcfs | central | fixed_greedy | fixed |
|---|---|---|---|---|
| Flights placed | 188/191 | 188/191 | 188/191 | 188/191 |
| Scenarios with every flight placed | 29/30 | 29/30 | 29/30 | 29/30 |
| Consensus (unanimous, complete or partial) | n/a | n/a | 25/30 | 29/30 |
| Fallback invoked | n/a | n/a | 5/30 | 1/30 |
| Mediation plan used in round 2 | n/a | n/a | 0/30 | 14/30 |
| Validator violations in chosen plans | 0 | 0 | 0 | 0 |
| Airborne holding (min) | 88 | 88 | 88 | 88 |
| Holding fuel burned (kg, workbook burn rates) | 3500 | 3500 | 3500 | 3500 |
| Delay absorbed by speed reduction (min) | 40 | 142 | 40 | 114 |
| Ground wait for gate (min) | 436 | 466 | 484 | 468 |
| Outbound departure delay (min) | 11826 | 11922 | 11874 | 11896 |
| Ground-crew overtime (min) | 984 | 588 | 836 | 650 |
| Missed-connection pax (synthetic) | 1879 | 1855 | 1869 | 1869 |
| Gate changes | 0 | 2 | 16 | 2 |
| Runtime median / p95 (s) | 0.00 / 0.01 | 0.20 / 2.41 | 0.04 / 0.20 | 0.23 / 2.34 |

Totals are summed over flights that were placed; always read them beside "Flights placed". Missed-connection counts are synthetic.

## Paired comparison: full system (fixed) vs FCFS, per scenario

| Metric | better | same | worse |
|---|---|---|---|
| resolved | 0 | 30 | 0 |
| holding | 0 | 30 | 0 |
| holding_fuel_kg | 0 | 30 | 0 |
| crew_overtime | 13 | 17 | 0 |
| missed_connections | 1 | 29 | 0 |
| departure_delay | 1 | 23 | 6 |
| ground_wait | 8 | 17 | 5 |
| gate_changes | 0 | 29 | 1 |

(Non-"resolved" rows compare only scenarios where both methods placed the same number of flights.)

## Failure and fallback cases (full system)

- `dot-3125`: outcome PROVEN_INFEASIBLE, decision FALLBACK, reason NO_AGREEMENT_IN_MENU; unresolved ['DL2549', 'DL2637', 'DL1770']; proof OPTIMAL max 5/8
