# 3-minute pitch

Run everything from **http://localhost:8000/ui**. Claude mode takes about 12 s per run (tested live);
pick "Claude agents" if the venue network is reliable, otherwise rule agents (under 2 s). Record a
labelled screen capture of a run as a backup.
Leave the Arduino hardware out of the pitch: it isn't in the brief and costs time.

| Time | Show | Say |
|---|---|---|
| 0:00–0:20 | Pick *169609 (MIA weather demo)*: scenario panel | "A duty manager at Miami, 9 June 2015. This American Airlines flight hit 36 minutes of weather delay; eight arrivals are caught behind it. Today she phones ATC, the airline and the ground handler one by one. We built the room where they agree, using the organisers' limits: Miami's runways, hourly cap, three spare gates, FAA wake separation, fuel and crew rules." |
| 0:20–1:05 | Run negotiation → Round 1 / mediation / Round 2 | "Three separate agents with their own limits. Ground rejects plans for crew overtime and gate changes; ATC for extra holding and lost throughput. No plan suits everyone, so the solver builds one inside every role's stated limits. The Pilot concedes: one flight holds about a minute longer than it would like and dips 12 seconds into contingency fuel. The final reserve is never touched; that's a hard rule. All three accept." |
| 1:05–1:30 | Outcome strip + timeline + Flights tab | "Against first-come-first-served: 37 minutes less holding, 1.6 tonnes less holding fuel, overtime from 38 minutes to 3, departure delay down 68 minutes. Revised arrival, gate and speed change for every flight, every check passed." |
| 1:30–1:50 | Pick *3125 (MSP failure case)* → Run | "Here no plan works. The solver proves only 5 of 8 flights can land before touching their final reserve, and the Pilot refuses every plan because crews would pass 13 hours of duty. After two turns the fallback decides, the log says exactly why, approval is blocked, and three flights go to the human for diversion." |
| 1:50–2:30 | Evaluation tab (30 historical windows) | "On 30 random held-out windows at the workbook's capacities: zero violations, every flight that can be placed was placed, agreement in 29 of 30, crew overtime down 34%. Most windows aren't congested at those capacities, so holding and fuel don't change, and departure delay is slightly worse. We report that." |
| 2:30–2:55 | Limits tab → Review card → Approve; Consensus log | "Every limit comes from your workbook, and every assumption is stated here. LLMs choose offers and explain; code does the arithmetic and the checks. Nothing is approved without the coordinator, and every turn is in the consensus log." |

## Numbers you may quote (from `results/`, simulation)

- 30 windows, 191 flights, workbook limits. Placed: 188 by every method (proven maximum 188). 0 violations.
- Crew overtime: 984 → 650 min (−34%). Better than FCFS in 13 windows, worse in none.
- Agreed 29/30, fallback 1/30 (the MSP case). Mediation used in 14/30.
- Solver menu vs heuristic-only menu: agreement 25 → 29, gate changes 16 → 2.
- Holding and fuel: no change on the 30 windows; departure delay +0.6% (better in 1, worse in 6). Say so.
- Synthetic stress profiles: holding −24%, holding fuel −10%, departure delay −36% (fixtures, not evidence).
- Demo MIA row was chosen because it shows a trade-off; it is excluded from the evaluation. Say so if asked.

## Don't say

- Any fuel or cost savings in dollars.
- "Certified", "safe", or "real ATC rules".
- That the LLM improves results. The earlier measurement showed it doesn't; re-run `--claude` before quoting.
- That the workbook is real airport data. It says it is synthetic.
- That fixed mode is Claude mode.

## If a judge asks "how do you know the solver's proof is right?"

The validator, the heuristic and the solver apply exactly the same workbook rules. Tests check both
directions: every heuristic plan is feasible inside the solver model, and every solver plan passes the
validator (the benchmark prints the count of rejected solver plans: 0). Three mismatches were found during
development (taxiway queue, overtime formula, crew duty) and each now has a regression test.
