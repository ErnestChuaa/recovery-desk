# Benchmark results

**Synthetic stress profiles (6 distinct, workbook Medium-hub limits); NOT historical evaluation** — 6 scenarios, 36 flights. Simulation results; not real-world savings.

CP-SAT upper bound (proven maximum placeable flights): 32/36 (6/6 scenarios proven optimal).

Solver plans rejected by the validator: 0 (must be 0; otherwise solver and validator disagree).

| Metric | fcfs | central | fixed_greedy | fixed |
|---|---|---|---|---|
| Flights placed | 31/36 | 32/36 | 31/36 | 32/36 |
| Scenarios with every flight placed | 2/6 | 3/6 | 2/6 | 3/6 |
| Consensus (unanimous, complete or partial) | n/a | n/a | 6/6 | 6/6 |
| Fallback invoked | n/a | n/a | 0/6 | 0/6 |
| Mediation plan used in round 2 | n/a | n/a | 0/6 | 6/6 |
| Validator violations in chosen plans | 0 | 0 | 0 | 0 |
| Airborne holding (min) | 500 | 387 | 472 | 378 |
| Holding fuel burned (kg, workbook burn rates) | 28247 | 26823 | 27099 | 25315 |
| Delay absorbed by speed reduction (min) | 85 | 114 | 86 | 104 |
| Ground wait for gate (min) | 703 | 678 | 699 | 698 |
| Outbound departure delay (min) | 539 | 362 | 504 | 345 |
| Ground-crew overtime (min) | 564 | 567 | 564 | 567 |
| Missed-connection pax (synthetic) | 352 | 321 | 358 | 346 |
| Gate changes | 2 | 3 | 8 | 6 |
| Runtime median / p95 (s) | 0.01 / 0.16 | 2.07 / 3.19 | 0.12 / 1.56 | 2.63 / 3.86 |

Totals are summed over flights that were placed; always read them beside "Flights placed". Missed-connection counts are synthetic.

## Paired comparison: full system (fixed) vs FCFS, per scenario

| Metric | better | same | worse |
|---|---|---|---|
| resolved | 1 | 5 | 0 |
| holding | 5 | 0 | 0 |
| holding_fuel_kg | 4 | 0 | 1 |
| crew_overtime | 1 | 3 | 1 |
| missed_connections | 3 | 1 | 1 |
| departure_delay | 5 | 0 | 0 |
| ground_wait | 4 | 1 | 0 |
| gate_changes | 0 | 3 | 2 |

(Non-"resolved" rows compare only scenarios where both methods placed the same number of flights.)

## Failure and fallback cases (full system)

- `demo-1`: outcome PROVEN_INFEASIBLE, decision CONSENSUS; unresolved ['F1']; proof OPTIMAL max 5/6
- `demo-3`: outcome PROVEN_INFEASIBLE, decision CONSENSUS; unresolved ['F1', 'F2']; proof OPTIMAL max 4/6
- `demo-4`: outcome PROVEN_INFEASIBLE, decision CONSENSUS; unresolved ['F1']; proof OPTIMAL max 5/6
