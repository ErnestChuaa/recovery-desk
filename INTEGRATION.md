# Dashboard and hardware contract

## REST endpoints

| Endpoint | Purpose |
|---|---|
| POST /api/event | ESP32 board event `{flight_id, event}`; see HARDWARE.md |
| GET /api/status/{board} | ESP32 board status (FLIGHT_A / FLIGHT_B) |
| GET /api/hardware, PUT /api/hardware/bindings | Board statuses, LAN URLs, board-to-flight mapping |
| GET /runs/latest | Latest run for the current scenario version, or null |
| GET /runs/{id}/log | Consensus log: each turn's messages, decision, why, revised arrival/gate/flight plan |
| GET /constraints | Workbook limits in force, rules of play, stated changes |
| GET /ui | Reference dashboard: timeline, negotiation trace, candidate menu, review card |
| GET /health | Liveness; simulation_only=true |
| GET /events/latest | Highest `event_id` (start the SSE stream after it) |
| GET /results/{name} | Benchmark JSON: historical-30, synthetic-smoke |
| GET /state | Current scenario with version and provenance |
| GET /scenario/demo?seed=0 | Generate fixture without modifying state |
| PUT /scenario | Load validated Scenario JSON; invalidate old approval |
| POST /scenario/from-csv | `{"row": 280983}`: build a scenario from a DOT row (server needs FLIGHTS_CSV) |
| GET /scenario/suggest?n=10 | Eligible rows with >=4 arrivals within +/-90 min |
| GET /baseline | FCFS plan and snapshot version |
| POST /runs | `{ "mode": "fixed" }` or `claude`; returns ID immediately |
| GET /runs/{id} | RUNNING, REVIEW_REQUIRED, APPROVED, STALE, INTERRUPTED or ERROR |
| GET /runs/{id}/timeline | Ordered landing/gate/departure events for replay/animation |
| POST /runs/{id}/approve | Apply complete current plan; 409 on stale/incomplete |
| POST /disruptions | Change state; dashboard explicitly starts a new run |
| POST /telemetry | Update node; meaningful events automatically start a run |
| GET /devices | Node statuses, freshness and current assignment |
| GET /devices/{id}/status | Versioned LED command |
| GET /events?after=0 | Up to 500 ordered events; persist the highest `event_id` for pagination |
| GET /events/stream?after=0 | SSE stream; event data is JSON |

FastAPI `/docs` and `/openapi.json` contain the exact field types and constraints.
Set `DASHBOARD_ORIGINS` to comma-separated frontend origins for browser CORS.
Plan metrics in seconds: `holding` (airborne), `contingency_used`, `duty_overrun`, `speed_absorb`, `ground_wait`, `departure_delay`,
`crew_overtime`, `max_flight_holding`, `min_fuel_margin`, `emergency_holding`, `turbulence_holding`,
`makespan`. Kilograms: `holding_fuel_kg`. Overtime at 1.5x: `crew_overtime_cost` (seconds). Counts: `resolved`, `unresolved`, `gate_changes`, `missed_connections` (synthetic pax),
`holding_stack`. `plan.details[]` has per-flight revised ETA, speed absorption (flight-plan adjustment),
airborne hold, ground wait, gate, crew, overtime and fuel margin.

## Run result fields

- `outcome`: CONSENSUS | FALLBACK_PLAN | PROVEN_INFEASIBLE | NO_PLAN_FOUND
- `fallback_explanation`: why the fallback decided (rules of play); `turn_limit` from the workbook
- `decision`: CONSENSUS | FALLBACK (how the plan was chosen, also for partial plans)
- `complete`, `fallback_reason` (AGENT_ERROR | NO_AGREEMENT_IN_MENU), `concessions` {role: [findings]},
  `fallback_soft_limit_breaches`, `proof` {status, proven, max_resolved, n}
- `candidates[]`: plan, checks, `accepted_by` (round 2), `role_findings` {role: [limit findings]}
- Recorded outcomes of the historical row are in `provenance.recorded_outcome_withheld` (display only).
- `trace[]` events: `menu`, `agent_message`, `coordinator` (round-1 summary and `MEDIATION`), `agent_error`, `decision`

`agent_message` fields: negotiation_id, scenario_id, state_version, round, sender, message_type
(OFFER|ACCEPT), ranked_plan_ids, accepted_plan_ids, preferred_plan_ids, conditions (round 1 strict
breaches it would tolerate), rejections [{plan_id, reason_code, metric, value, limit, tier}], conceded,
concessions, summary, summary_source (code|model). Tiers: hard, relaxed (rejected), strict (concession),
coordination (not offered by all), preference.

## Disruption bodies

```json
{"kind":"gate_closure","gate":"B","start":300,"end":1800}
```

Other kinds: runway_closure, taxi_closure (start/end); latest (flight_id and end
as latest-arrival seconds); emergency, reset_emergency, turbulence, reset_turbulence (flight_id);
delay (flight_id, start = extra seconds; ETA and fuel deadline move together).
Disruptions are additive; reload a saved scenario to remove closures or reset motion.
Emergency is a priority preference in scheduling, never permission to violate checks.

## Telemetry body

```json
{"device_id":"node1","flight_id":"F1","boot_id":"boot-unique-123",
 "seq":1,"uptime_ms":500,"emergency_flag":false,"motion_score":0.0}
```

Nodes send at 2 Hz. `boot_id` must change on reboot; seq increases within a boot.
Backend uses server receipt time for freshness, so MCU clocks need not match laptop.
Duplicates/out-of-order same-boot messages return accepted=false. motion_score is
abs(acceleration magnitude in g - 1), a bench proxy. Three consecutive samples >=0.8
latch a simulated motion event, which tightens the Pilot agent's holding limit for that flight. The first true emergency flag latches urgency until
explicit reset. Repeated packets do not repeatedly invoke the LLM.

Response includes accepted, state_version, status, led, assignment and optional run_id.
Green means operator-approved simulation assignment; yellow means replan/review pending;
red means offline/unknown or unresolved review. Hardware must expire commands locally
after 2500 ms and reject older versions. Backend marks device offline after 3 seconds.
Status does not mean permission to land a real aircraft.

## UI event flow

1. Render /state and /baseline. Draw intervals in seconds, formatting labels locally.
2. Start run, subscribe to events, and show the two round numbers plus individual responses.
3. On run_completed fetch full run. Highlight failed checks from checks[].evidence.
4. Render recovery assignments and unresolved IDs separately. Show all provenance.
5. Enable approval only for REVIEW_REQUIRED, zero violations, no unresolved flights.
   Show `concessions` (or `fallback_soft_limit_breaches`) on the review card.
6. On state_changed clear old approval and show stale plans as history only.
7. Poll /devices for freshness even if no new events arrive. A lost connection is visible.

Plan checks are code-generated. Agent summary text is explanation only. Do not display
it as proof or hidden reasoning. Show which condition rejected a plan and each role's vote.
The run's `candidates` array resolves all IDs in the agent trace to assignments,
metrics, eligibility, checks and diagnostics. `diagnostics` on the run explains
unresolved flights: RUNWAY_WINDOW_CONFLICT (no open runway inside the fuel window),
PROVEN_JOINT_INFEASIBILITY (CP-SAT proved not all flights fit), or BOUNDED_SEARCH_EXHAUSTED.
An unoffered admissible candidate is not automatically a constraint violation.

## Hardware setup

Copy firmware/node/config.example.h to config.h. Set SSID/password, laptop IPv4,
node ID and flight ID (node1/F1 and node2/F2 for demo). Select the correct board in
Arduino IDE. ESP32 uses Adafruit MPU6050 over default I2C pins; UNO WiFi Rev2 uses
its onboard LSM6DS3. Install libraries listed at the top of node.ino.
Use a button between configured input pin and GND (internal pull-up), and resistors
for LEDs. Check sensor breakout voltage requirements and board pin mappings.
Standard UNO R3 needs a communication bridge and is not supported by this sketch.
UNO R4 WiFi is also a different board; do not select the Rev2 firmware for it.

Firmware HTTP status is piggybacked on telemetry responses, avoiding a second polling
request. The backend status endpoint is available to other clients. If the server
restarts and version falls, reboot nodes or reload the matching saved state.

## Operational limits of this handoff

Single process/worker. No distributed message bus or production access control.
SQLite event log grows until you archive/delete the local demo database. Device
bindings are in-memory and reset on scenario load or restart. API keys are never
stored in exported logs. Real Claude calls and physical board behaviour require
the team's own credentials/hardware validation.
