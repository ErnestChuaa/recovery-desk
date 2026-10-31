"""Deterministic scheduling, metrics and independent complete-plan validation.

Times are integer seconds; intervals are half-open. All operating limits come from the organisers'
workbook via recovery/constraints.py and are snapshotted into each Scenario: FAA wake separation by
arrival/departure stream, the hourly runway-movement cap, spare gates, minimum turnaround, final fuel
reserve, flight-crew duty, ground-crew shifts and gate-change notice. Every plan, from any generator,
must pass validate() before it is offered, voted on or approved. The heuristic and the CP-SAT model
use exactly the same rules, so a solver proof covers every plan this validator accepts.
"""
import hashlib
import random
from collections import Counter
from .models import Assignment, Plan, Scenario
from . import constraints as C

GRID = 30            # scheduling grid (seconds), shared by the heuristic and the CP-SAT model
RATE_STEP = 900      # hourly-cap windows start every 15 minutes
RATE_WINDOW = 3600


def overlap(a, b, c, d): return a < d and c < b


def peak(intervals):
    """Maximum number of simultaneously open half-open intervals."""
    ev = sorted([(a, 1) for a, b in intervals if b > a] + [(b, -1) for a, b in intervals if b > a])
    cur = best = 0
    for _, d in ev:
        cur += d; best = max(best, cur)
    return best


def rate_windows(s: Scenario):
    """[(window start, movements our flights may still add, background movements)] for the hourly cap."""
    if not s.max_ops_per_hour: return []
    bg = [m.time for m in s.moves()]
    out = []
    for a in range(0, s.horizon + 1, RATE_STEP):
        n = sum(a <= t < a + RATE_WINDOW for t in bg)
        out.append((a, max(0, s.max_ops_per_hour - n), n))
    return out


def gate_change_ok(s, f, gate, landing):
    return gate == f.preferred_gate or landing >= s.decision_time + s.gate_change_notice


# --------------------------------------------------------------------------- validation
def validate(s: Scenario, plan: Plan):
    checks = []
    def check(role, rule, ok, **evidence):
        checks.append(dict(role=role, rule=rule, passed=bool(ok), evidence=evidence))
    fs = {f.id: f for f in s.flights}
    ids = [a.flight_id for a in plan.assignments]
    check('ATC', 'ACCOUNTING', len(ids) == len(set(ids)) and len(plan.unresolved) == len(set(plan.unresolved))
          and not set(ids) & set(plan.unresolved) and set(ids) | set(plan.unresolved) == set(fs))
    movements = [(m.time, 'BACKGROUND', m.kind, m.wake) for m in s.moves()]
    ours = []
    waits = []
    for a in plan.assignments:
        f = fs.get(a.flight_id)
        if not f:
            check('ATC', 'KNOWN_FLIGHT', False, flight=a.flight_id); continue
        gmin = a.landing + f.taxi
        check('ATC', 'ARRIVAL_WINDOW', a.landing >= f.ready, flight=f.id, actual=a.landing, earliest=f.ready)
        check('PILOT', 'FINAL_FUEL_RESERVE', a.landing <= f.latest, flight=f.id, actual=a.landing, latest=f.latest, margin=f.latest - a.landing)
        check('GROUND', 'GATE_COMPATIBLE', a.gate in f.gates and a.gate in s.gates, flight=f.id, gate=a.gate)
        check('GROUND', 'GATE_CHANGE_NOTICE', gate_change_ok(s, f, a.gate, a.landing), flight=f.id, gate=a.gate,
              planned_gate=f.preferred_gate, landing=a.landing, earliest_change=s.decision_time + s.gate_change_notice)
        # Taxi-out goes straight to take-off: an aircraft waiting for a departure slot keeps its gate.
        check('GROUND', 'TASK_ORDER', a.gate_in >= gmin and a.gate_out >= a.gate_in + f.turnaround
              and a.departure == a.gate_out + f.tout and a.departure <= s.horizon, flight=f.id)
        check('ATC', 'GRID_ALIGNED', (a.landing - f.ready) % GRID == 0 and (a.gate_in - gmin) % GRID == 0
              and (a.gate_out - a.gate_in - f.turnaround) % GRID == 0, flight=f.id, grid=GRID)
        check('GROUND', 'GROUND_WAIT_LIMIT', a.gate_in - gmin <= s.max_ground_wait, flight=f.id,
              actual=a.gate_in - gmin, limit=s.max_ground_wait)
        if a.gate_in > gmin: waits.append((gmin, a.gate_in))
        check('GROUND', 'CREW_EXISTS', 0 <= a.crew < s.crew_count, flight=f.id, crew=a.crew)
        for w in s.gates.get(a.gate, []):
            check('GROUND', 'GATE_RESERVATION', not overlap(a.gate_in, a.gate_out, w.start, w.end), flight=f.id, window=w.model_dump())
        for w in s.taxi_closed:
            check('ATC', 'TAXI_OPEN', not overlap(a.landing, gmin, w.start, w.end) and not overlap(a.gate_out, a.departure, w.start, w.end), flight=f.id)
        for t, kind in ((a.landing, 'A'), (a.departure, 'D')):
            movements.append((t, f.id, kind, f.wake)); ours.append(t)
            check('ATC', 'RUNWAY_OPEN', all(not w.start <= t < w.end for w in s.runway_closed), flight=f.id, time=t)
    check('GROUND', 'HOLD_PAD_CAPACITY', peak(waits) <= s.hold_pad_capacity, peak=peak(waits), capacity=s.hold_pad_capacity)
    for i, a in enumerate(plan.assignments):
        for b in plan.assignments[i + 1:]:
            check('GROUND', 'GATE_NO_OVERLAP', a.gate != b.gate or not overlap(a.gate_in, a.gate_out, b.gate_in, b.gate_out), flights=[a.flight_id, b.flight_id])
            fa, fb = fs.get(a.flight_id), fs.get(b.flight_id)
            if fa and fb:
                check('GROUND', 'CREW_NO_OVERLAP', a.crew != b.crew or not overlap(a.gate_in, a.gate_in + fa.turnaround, b.gate_in, b.gate_in + fb.turnaround), flights=[a.flight_id, b.flight_id])
    # Wake separation within each runway stream (FAA table from the workbook).
    movements.sort(key=lambda m: (m[0], m[1], m[2]))
    maxgap = s.max_gap()
    for i in range(len(movements)):
        t, a, ka, wa = movements[i]
        seen_follower = False
        for j in range(i + 1, len(movements)):
            u, b, kb, wb = movements[j]
            if u - t >= maxgap: break
            if a == b == 'BACKGROUND': continue
            need = s.gap(ka, wa, kb, wb)
            if need is None: continue
            ok = u - t >= need
            if not seen_follower or not ok:
                check('ATC', 'RUNWAY_WAKE_SEPARATION', ok, flights=[a, b], kinds=[ka, kb], actual=u - t, required=need, leader_wake=wa, follower_wake=wb)
            seen_follower = True
    # Hourly runway-movement cap from the workbook (background traffic counts towards it).
    windows = rate_windows(s)
    worst = None
    for a0, allowed, bg in windows:
        n = sum(a0 <= t < a0 + RATE_WINDOW for t in ours)
        if n > allowed:
            check('ATC', 'RUNWAY_HOURLY_CAPACITY', False, window_start=a0, our_movements=n, background=bg, cap=s.max_ops_per_hour)
        if worst is None or n + bg > worst[0]: worst = (n + bg, a0)
    if windows:
        check('ATC', 'RUNWAY_HOURLY_CAPACITY', True, peak_movements=worst[0], peak_window_start=worst[1], cap=s.max_ops_per_hour)
    return checks


# --------------------------------------------------------------------------- metrics
def details(s: Scenario, p: Plan):
    fs = {f.id: f for f in s.flights}
    mult = C.load()['fuel']['overtime_multiplier']
    out = []
    for a in p.assignments:
        f = fs[a.flight_id]
        delay = a.landing - f.ready
        absorb = min(delay, f.enroute_absorb)
        airborne = delay - absorb
        # Overtime = the part of this job worked after the crew's shift end.
        ot = (max(0, a.gate_in + f.turnaround - max(a.gate_in, s.crew_shift_end[a.crew])) if s.crew_shift_end else 0)
        margin = f.latest - a.landing
        out.append(dict(flight_id=f.id, revised_eta=a.landing, arrival_delay=delay, speed_absorb=absorb,
            airborne_hold=airborne, holding_fuel_kg=round(airborne / 3600 * C.burn_kg_per_h(f.wake)),
            ground_wait=a.gate_in - (a.landing + f.taxi), gate=a.gate,
            gate_changed=a.gate != f.preferred_gate, crew=a.crew, crew_overtime=ot, crew_overtime_cost=ot * mult,
            departure_delay=max(0, a.departure - f.scheduled_departure), fuel_margin=margin,
            contingency_used=max(0, f.contingency - margin),
            # Avoidable landing time past the crew's 13 h duty (if the ETA is already past it, only added delay counts).
            duty_overrun=max(0, a.landing - max(f.duty_end, f.ready)) if f.duty_end is not None else 0,
            missed_connection_pax=f.connections if a.gate_in > f.connection_deadline else 0,
            emergency=f.emergency, turbulence=f.motion_event, wake=f.wake,
            flight_plan_adjustment=(f'Reduce speed en route to absorb {absorb}s; revised runway ETA {a.landing}s' if absorb else None)))
    return out


def metrics(s, p, det=None):
    det = det if det is not None else details(s, p)
    fs = {f.id: f for f in s.flights}
    hold_iv = [(fs[d['flight_id']].ready + d['speed_absorb'], d['revised_eta']) for d in det if d['airborne_hold'] > 0]
    return {
        'departure_delay': sum(d['departure_delay'] for d in det),
        'holding': sum(d['airborne_hold'] for d in det),
        'holding_fuel_kg': sum(d['holding_fuel_kg'] for d in det),
        'speed_absorb': sum(d['speed_absorb'] for d in det),
        'ground_wait': sum(d['ground_wait'] for d in det),
        'missed_connections': sum(d['missed_connection_pax'] for d in det),
        'gate_changes': sum(d['gate_changed'] for d in det),
        'crew_overtime': sum(d['crew_overtime'] for d in det),
        'crew_overtime_cost': sum(d['crew_overtime_cost'] for d in det),
        'max_flight_holding': max([d['airborne_hold'] for d in det], default=0),
        'min_fuel_margin': min([d['fuel_margin'] for d in det], default=0),
        'contingency_used': max([d['contingency_used'] for d in det], default=0),
        'duty_overrun': max([d['duty_overrun'] for d in det], default=0),
        'emergency_holding': max([d['airborne_hold'] for d in det if d['emergency']], default=0),
        'turbulence_holding': max([d['airborne_hold'] for d in det if d['turbulence']], default=0),
        'holding_stack': peak(hold_iv),
        'makespan': max([a.departure for a in p.assignments], default=0),
        'resolved': len(p.assignments),
        'unresolved': len(p.unresolved),
    }


def neutral(m):
    """Declared deterministic fallback score (lower is better). Not any agent's private weights."""
    return (2 * m['holding'] + m['departure_delay'] + 0.5 * m['ground_wait'] + 0.5 * m['speed_absorb']
            + 180 * m['gate_changes'] + m['crew_overtime_cost'] + 120 * m['missed_connections'])


def finalize(s, assignments, unresolved, source):
    raw = '|'.join(a.model_dump_json() for a in sorted(assignments, key=lambda a: a.flight_id)) + '|' + ','.join(sorted(unresolved))
    p = Plan(id=hashlib.sha256(raw.encode()).hexdigest()[:12], assignments=assignments, unresolved=unresolved, source=source)
    p.details = details(s, p); p.metrics = metrics(s, p, p.details)
    return p


# --------------------------------------------------------------------------- greedy heuristic
def schedule(s, order, gate_shift=0, source='greedy'):
    assigned = []; unresolved = []
    fixed = [(m.time, m.kind, m.wake) for m in s.moves()]
    placed = []                                   # our committed movements (time, kind, wake)
    gates = {g: [(w.start, w.end) for w in ws] for g, ws in s.gates.items()}
    crews = [[] for _ in range(s.crew_count)]
    waits = []
    shift = s.crew_shift_end
    allowed = {a0: al for a0, al, _ in rate_windows(s)}
    used = Counter()

    def windows_of(t): return [a0 for a0 in allowed if a0 <= t < a0 + RATE_WINDOW]

    def rate_ok(times):
        c = Counter(a0 for t in times for a0 in windows_of(t))
        return all(used[a0] + n <= allowed[a0] for a0, n in c.items())

    def runway_ok(t, kind, wk, extra=()):
        if any(w.start <= t < w.end for w in s.runway_closed): return False
        for u, k2, w2 in fixed + placed + list(extra):
            need = s.gap(k2, w2, kind, wk) if u <= t else s.gap(kind, wk, k2, w2)
            if need is not None and abs(t - u) < need: return False
        return rate_ok([t] + [e[0] for e in extra])

    def pad_ok(a, b):
        return peak(waits + [(a, b)]) <= s.hold_pad_capacity

    def overtime(c, gi, end):
        return max(0, end - max(gi, shift[c])) if shift else 0

    for f in order:
        found = None
        gs = sorted(f.gates, key=lambda g: (g != f.preferred_gate, g))
        gs = gs[gate_shift % len(gs):] + gs[:gate_shift % len(gs)]
        for land in range(f.ready, min(f.land_by, s.horizon) + 1, GRID):
            if not runway_ok(land, 'A', f.wake): continue
            gmin = land + f.taxi
            if any(overlap(land, gmin, w.start, w.end) for w in s.taxi_closed): continue
            for g in gs:
                if not gate_change_ok(s, f, g, land): continue
                for gi in range(gmin, gmin + s.max_ground_wait + 1, GRID):
                    if gi > gmin and not pad_ok(gmin, gi): break
                    end = gi + f.turnaround
                    if any(overlap(gi, end, x, y) for x, y in gates[g]): continue
                    free = [c for c in range(s.crew_count) if not any(overlap(gi, end, x, y) for x, y in crews[c])]
                    if not free: continue
                    crew = min(free, key=lambda c: (overtime(c, gi, end), c))
                    for go in range(end, s.horizon - f.tout + 1, GRID):
                        dep = go + f.tout
                        if any(overlap(gi, go, x, y) for x, y in gates[g]): break
                        if not runway_ok(dep, 'D', f.wake, [(land, 'A', f.wake)]): continue
                        if any(overlap(go, dep, w.start, w.end) for w in s.taxi_closed): continue
                        found = Assignment(flight_id=f.id, landing=land, gate=g, gate_in=gi, gate_out=go, departure=dep, crew=crew)
                        break
                    if found: break
                if found: break
            if found: break
        if found:
            assigned.append(found)
            placed += [(found.landing, 'A', f.wake), (found.departure, 'D', f.wake)]
            for t in (found.landing, found.departure):
                for a0 in windows_of(t): used[a0] += 1
            gates[found.gate].append((found.gate_in, found.gate_out))
            crews[found.crew].append((found.gate_in, found.gate_in + f.turnaround))
            if found.gate_in > found.landing + f.taxi: waits.append((found.landing + f.taxi, found.gate_in))
        else:
            unresolved.append(f.id)
    p = finalize(s, assigned, unresolved, source)
    bad = [c for c in validate(s, p) if not c['passed']]
    if bad: raise RuntimeError(f'scheduler produced invalid plan: {bad[:2]}')
    return p


POLICIES = {
    'fcfs': lambda f: (not f.emergency, f.ready, f.id),
    'edf': lambda f: (not f.emergency, f.latest, f.id),
    'connections': lambda f: (not f.emergency, -f.connections, f.latest),
    'short_turn': lambda f: (not f.emergency, f.turnaround, f.ready),
}

MENU_CAP = 20


def candidate_menu(s, use_solver=True):
    """Bounded common menu: greedy policies x gate rotations, seeded random orders,
    and CP-SAT plans optimised for different objectives. Returns (plans, proof)."""
    pool = {}
    def add(p):
        if p.id in pool:
            if p.source not in pool[p.id].source: pool[p.id].source += ',' + p.source
        else: pool[p.id] = p
    for name, key in POLICIES.items():
        for shift in range(3):
            add(schedule(s, sorted(s.flights, key=key), shift, f'greedy:{name}:g{shift}'))
    rnd = random.Random(0)
    for k in range(6):
        keys = {f.id: rnd.random() for f in s.flights}
        add(schedule(s, sorted(s.flights, key=lambda f: (not f.emergency, keys[f.id])), k % 3, f'greedy:random{k}'))
    proof = {'status': 'NOT_RUN', 'proven': False, 'max_resolved': None, 'n': len(s.flights)}
    if use_solver:
        try:
            from .solver import solve_menu
            greedy = list(pool.values())
            warm = min(greedy, key=lambda p: (-p.metrics['resolved'], neutral(p.metrics), p.id))
            plans, proof = solve_menu(s, warm)
            for p in plans: add(p)
        except ImportError:
            proof = {'status': 'SOLVER_UNAVAILABLE', 'proven': False, 'max_resolved': None, 'n': len(s.flights)}
    plans = list(pool.values())
    solver_ids = {p.id for p in plans if 'solver:' in p.source}
    plans.sort(key=lambda p: (p.id not in solver_ids, -p.metrics['resolved'], neutral(p.metrics), p.id))
    return plans[:MENU_CAP], proof


def candidates(s, use_solver=True):
    return candidate_menu(s, use_solver)[0]


def fallback(s, pool=None):
    """Deterministic non-AI fallback: most flights resolved, then lowest declared neutral score, then ID."""
    pool = pool if pool is not None else candidates(s)
    if not pool: return schedule(s, sorted(s.flights, key=POLICIES['edf']), source='greedy:edf')
    return min(pool, key=lambda p: (-p.metrics['resolved'], neutral(p.metrics), p.id))


def fcfs(s):
    """Status-quo baseline: constraint-respecting first-come-first-served (emergencies first)."""
    return schedule(s, sorted(s.flights, key=POLICIES['fcfs']), source='baseline:fcfs')


def diagnostics(s, p, proof=None):
    result = []
    for f in s.flights:
        if f.id not in p.unresolved: continue
        earliest = f.ready
        for w in sorted(s.runway_closed, key=lambda w: w.start):
            if w.start <= earliest < w.end: earliest = w.end
        if earliest > f.land_by:
            rule, why = 'RUNWAY_WINDOW_CONFLICT', 'No open runway time before this flight would have to use its final fuel reserve. Operator decision required (e.g. diversion).'
        elif proof and proof.get('proven'):
            rule, why = 'PROVEN_JOINT_INFEASIBILITY', (f"CP-SAT proved at most {proof['max_resolved']} of {proof['n']} flights fit together under this model "
                        '(30 s grid, stated constraints). This flight is not in the best plan; operator decision required.')
        else:
            rule, why = 'BOUNDED_SEARCH_EXHAUSTED', 'No assignment found by the bounded search. This does not prove infeasibility.'
        result.append({'flight_id': f.id, 'rule': rule, 'earliest_open': earliest, 'latest_policy_arrival': f.land_by, 'explanation': why})
    return result
