# ESP32 boards (Flight A and Flight B)

The backend implements the board contract exactly: `POST /api/event` and
`GET /api/status/{FLIGHT_A|FLIGHT_B}`. Nothing needs to change in the board firmware except the
server address.

## Setup on the day

1. The laptop and both boards join the same 2.4 GHz hotspot.
2. Start the server so it listens on the hotspot, not just this laptop:
   - double-click `START_WITH_BOARDS.cmd`, or
   - in your existing launcher, change `--host 127.0.0.1` to `--host 0.0.0.0`.
3. Allow Python through Windows Firewall when asked, on **Private and Public** networks. Phone hotspots
   are often classed as Public, and the boards will time out if Public is blocked.
4. Read the board address from the server window
   (`ESP32 boards: set server to http://<ip>:8000`) or from the dashboard ("Boards connect to …").
   Put that address in the firmware. It changes if the hotspot gives the laptop a new IP.
5. Check from a phone on the same hotspot: open `http://<ip>:8000/api/status/FLIGHT_A`.
6. Rehearse without the boards: `python scripts/simulate_esp32.py --url http://<ip>:8000 --press "FLIGHT A" EMERGENCY`.

## Which flight each board represents

By default Board A is the scenario's trigger (focal) flight and Board B is the next arriving flight.
Change it from the dropdowns on the dashboard, or with
`PUT /api/hardware/bindings {"flight_a": "MQ3280", "flight_b": "OO2922"}`. Loading a new scenario
resets the defaults.

## What the events do

| Board sends | Backend does |
|---|---|
| `EMERGENCY` | Marks that flight as an emergency (scheduled first, never held), then starts a negotiation |
| `DISRUPTION` | Marks turbulence on that flight (the Pilot agent's holding limit tightens), then starts a negotiation |
| `CLEAR` | Removes both flags. Any approved plan becomes stale. Negotiates again only if the other board is still in an emergency |

Repeated presses or shakes while a flag is already set change nothing and start no new negotiation.
Agent mode for board-started runs is `HARDWARE_AGENT_MODE` (default `fixed`; set `claude` for Claude agents).

## Status the boards receive

Derived from the backend's real state on every poll, so the boards and dashboard always agree.

| status | led | emergency_active | When |
|---|---|---|---|
| WAITING | YELLOW | false | No emergency on this board's flight and no approved plan |
| NEGOTIATING | YELLOW | true | Event received; agents running, or the plan was made stale by a change |
| AWAITING_APPROVAL | YELLOW | true | Agents produced a complete plan; operator has not approved yet |
| PLAN_APPROVED | GREEN | false | Operator approved; `landing_time` and `gate` are this flight's slot (both boards go green) |
| UNRESOLVED | RED | true | No complete plan (e.g. a flight cannot land within its fuel limit) or the run failed |

`NEGOTIATING` is returned in the same request that receives the event, so the next poll never shows
`WAITING` (no flicker). `landing_time` is `T+HH:MM` from scenario start (`HARDWARE_TIME_FORMAT=clock`
gives local time such as `10:47` for historical scenarios). `gate` is one of the airport's spare gates from the
organisers' workbook (`G1`, `G2`, `G3`). When there is no slot, both are `--`.

The older `/telemetry` protocol in `firmware/node/` still works but is not needed for these boards.
