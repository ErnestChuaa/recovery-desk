"""CP-SAT model of the same constraints the validator checks.

Phase 1 maximises the number of flights placed. If CP-SAT reports OPTIMAL, the
result is a proof (for this model: 30 s grid, workbook limits, stated assumptions) of the most
flights that can be accommodated. Phase 2 fixes that count and optimises several
role-flavoured objectives to give the agents a diverse menu with real trade-offs.

Settings are deterministic (single worker, fixed seed, deterministic time limit),
so the same scenario yields the same menu on any machine. Solver output is never
trusted directly: every plan is passed through engine.validate().
"""
from ortools.sat.python import cp_model
from .models import Assignment, Scenario
from .engine import GRID, RATE_WINDOW, finalize, rate_windows, validate
from . import constraints as C

DET_TIME = 0.4   # deterministic time units per solve (~0.5-1 s wall each)


def _build(s: Scenario, objective: str, resolved_at_least=None, limits=None):
    m = cp_model.CpModel(); G = GRID; H = s.horizon
    V = {}
    moves = []  # (time var, kind, wake, presence, flight index, lower bound, upper bound)
    gate_iv = {g: [m.NewFixedSizeIntervalVar(w.start, w.end - w.start, '') for w in ws] for g, ws in s.gates.items()}
    crew_iv = {c: [] for c in range(s.crew_count)}
    pad_iv = []
    terms = {k: [] for k in ('air', 'fuelw', 'absorbed', 'wait', 'dd', 'chg', 'ot', 'pax', 'arr')}
    mk = m.NewIntVar(0, H, 'makespan')
    flight_air = {}
    notice_at = s.decision_time + s.gate_change_notice
    for fi, f in enumerate(s.flights):
        x = m.NewBoolVar(f'x_{f.id}')
        kl = m.NewIntVar(0, max(0, (min(f.land_by, H) - f.ready) // G), '')
        kw = m.NewIntVar(0, s.max_ground_wait // G if s.hold_pad_capacity > 0 else 0, '')
        kj = m.NewIntVar(0, H // G, '')
        for v in (kl, kw, kj): m.Add(v == 0).OnlyEnforceIf(x.Not())
        L = m.NewIntVar(0, 4 * H, ''); GI = m.NewIntVar(0, 4 * H, ''); GO = m.NewIntVar(0, 4 * H, ''); D = m.NewIntVar(0, 4 * H, '')
        m.Add(L == f.ready + G * kl)
        m.Add(GI == L + f.taxi + G * kw)
        m.Add(GO == GI + f.turnaround + G * kj)
        m.Add(D == GO + f.tout)
        m.Add(D <= H).OnlyEnforceIf(x)
        m.Add(mk >= D).OnlyEnforceIf(x)
        dmin = f.ready + f.taxi + f.turnaround + f.tout
        moves += [(L, 'A', f.wake, x, fi, f.ready, min(f.land_by, H)), (D, 'D', f.wake, x, fi, dmin, H)]
        own = s.gap('A', f.wake, 'D', f.wake)
        if own is not None: m.Add(D >= L + own).OnlyEnforceIf(x)
        for w in s.runway_closed:
            for t in (L, D):
                b = m.NewBoolVar('')
                m.Add(t < w.start).OnlyEnforceIf([b, x]); m.Add(t >= w.end).OnlyEnforceIf([b.Not(), x])
        for w in s.taxi_closed:
            for a, z in ((L, L + f.taxi), (GO, D)):
                b = m.NewBoolVar('')
                m.Add(z <= w.start).OnlyEnforceIf([b, x]); m.Add(a >= w.end).OnlyEnforceIf([b.Not(), x])
        gb = {}
        for g in f.gates:
            gb[g] = m.NewBoolVar('')
            gate_iv[g].append(m.NewOptionalIntervalVar(GI, f.turnaround + G * kj, GO, gb[g], ''))
            if g != f.preferred_gate: m.Add(L >= notice_at).OnlyEnforceIf(gb[g])   # gate-change notice
        m.Add(sum(gb.values()) == x)
        cb = []
        for c in range(s.crew_count):
            b = m.NewBoolVar(''); cb.append(b)
            crew_iv[c].append(m.NewOptionalFixedSizeIntervalVar(GI, f.turnaround, b, ''))
            if s.crew_shift_end:
                # Exact work after shift end: clamp(GI + turnaround - shift_end, 0, turnaround).
                past = m.NewIntVar(-8 * H, 8 * H, '')
                m.Add(past == GI + f.turnaround - s.crew_shift_end[c])
                capped = m.NewIntVar(-8 * H, f.turnaround, '')
                m.AddMinEquality(capped, [past, f.turnaround])
                job_ot = m.NewIntVar(0, f.turnaround, '')
                m.AddMaxEquality(job_ot, [capped, 0])
                ot = m.NewIntVar(0, f.turnaround, '')
                m.Add(ot >= job_ot).OnlyEnforceIf(b)
                terms['ot'].append(ot)
        m.Add(sum(cb) == x)
        if s.hold_pad_capacity > 0:
            ps = m.NewIntVar(0, 4 * H, ''); m.Add(ps == L + f.taxi)
            pad_iv.append(m.NewOptionalIntervalVar(ps, G * kw, GI, x, ''))
        # objective pieces (all zero for unplaced flights)
        air = m.NewIntVar(0, H, '')
        m.Add(air >= G * kl - f.enroute_absorb)
        flight_air[f.id] = (air, x)
        terms['air'].append(air); terms['absorbed'].append(G * kl - air); terms['arr'].append(G * kl)
        terms['fuelw'].append(air * round(C.burn_kg_per_h(f.wake) / 100))       # holding fuel, kg-weighted
        terms['wait'].append(G * kw)
        dd = m.NewIntVar(0, H, ''); m.Add(dd >= D - f.scheduled_departure).OnlyEnforceIf(x); terms['dd'].append(dd)
        terms['chg'].append(x - gb[f.preferred_gate])
        miss = m.NewBoolVar('')
        m.Add(GI <= f.connection_deadline).OnlyEnforceIf([miss.Not(), x]); terms['pax'].append(f.connections * miss)
        V[f.id] = (x, L, GI, GO, D, gb, cb, kl, kw, kj)
    # Wake separation within each runway stream; background traffic is fixed.
    bg = s.moves(); maxgap = s.max_gap()
    for i in range(len(moves)):
        ti, ki, wi, xi, fi, lo_i, hi_i = moves[i]
        for j in range(i + 1, len(moves)):
            tj, kj_, wj, xj, fj, lo_j, hi_j = moves[j]
            if fi == fj: continue                       # own landing/departure handled above
            g_ij = s.gap(ki, wi, kj_, wj); g_ji = s.gap(kj_, wj, ki, wi)
            if g_ij is None: continue                   # different streams
            if hi_i + maxgap <= lo_j or hi_j + maxgap <= lo_i: continue   # can never be close
            b = m.NewBoolVar('')
            m.Add(tj >= ti + g_ij).OnlyEnforceIf([b, xi, xj])
            m.Add(ti >= tj + g_ji).OnlyEnforceIf([b.Not(), xi, xj])
        for u in bg:
            after = s.gap(u.kind, u.wake, ki, wi); before = s.gap(ki, wi, u.kind, u.wake)
            if after is None: continue
            if u.time + after <= lo_i or u.time - before >= hi_i: continue  # outside this movement's reach
            b = m.NewBoolVar('')
            m.Add(ti >= u.time + after).OnlyEnforceIf([b, xi])
            m.Add(ti <= u.time - before).OnlyEnforceIf([b.Not(), xi])
    # Hourly runway-movement cap (workbook), on the same 15-minute windows as the validator.
    for a0, allowed, _ in rate_windows(s):
        cand = [mv for mv in moves if mv[5] < a0 + RATE_WINDOW and mv[6] >= a0]
        if len(cand) <= allowed: continue
        inside = []
        for t, _, _, x, _, _, _ in cand:
            ge = m.NewBoolVar(''); lt = m.NewBoolVar(''); z = m.NewBoolVar('')
            m.Add(t >= a0).OnlyEnforceIf(ge); m.Add(t < a0).OnlyEnforceIf(ge.Not())
            m.Add(t < a0 + RATE_WINDOW).OnlyEnforceIf(lt); m.Add(t >= a0 + RATE_WINDOW).OnlyEnforceIf(lt.Not())
            m.AddBoolOr([ge.Not(), lt.Not(), x.Not(), z])          # in window and placed -> counted
            inside.append(z)
        m.Add(sum(inside) <= allowed)
    for g in gate_iv: m.AddNoOverlap(gate_iv[g])
    for c in crew_iv: m.AddNoOverlap(crew_iv[c])
    if pad_iv: m.AddCumulative(pad_iv, [1] * len(pad_iv), s.hold_pad_capacity)
    X = [V[f.id][0] for f in s.flights]
    if resolved_at_least is not None: m.Add(sum(X) >= resolved_at_least)
    T = {k: sum(v) if v else 0 for k, v in terms.items()}
    if limits:
        # Mediation: every role's stated limit becomes a hard constraint (holding_stack is post-checked).
        fx = {f.id: f for f in s.flights}
        for metric, lim in limits.items():
            lim = int(lim)
            if metric == 'max_flight_holding':
                for fid, (air, x) in flight_air.items(): m.Add(air <= lim)
            elif metric in ('emergency_holding', 'turbulence_holding'):
                attr = 'emergency' if metric == 'emergency_holding' else 'motion_event'
                for fid, (air, x) in flight_air.items():
                    if getattr(fx[fid], attr): m.Add(air <= lim)
            elif metric == 'min_fuel_margin':
                for fid in V: m.Add(V[fid][1] <= fx[fid].latest - lim).OnlyEnforceIf(V[fid][0])
            elif metric == 'contingency_used':
                for fid in V: m.Add(V[fid][1] <= fx[fid].latest - fx[fid].contingency + lim).OnlyEnforceIf(V[fid][0])
            elif metric == 'duty_overrun':
                for fid in V:
                    f_ = fx[fid]
                    if f_.duty_end is not None: m.Add(V[fid][1] <= max(f_.duty_end, f_.ready) + lim).OnlyEnforceIf(V[fid][0])
            elif metric == 'makespan': m.Add(mk <= lim)
            else:
                key = {'holding': 'air', 'missed_connections': 'pax', 'crew_overtime': 'ot', 'gate_changes': 'chg', 'departure_delay': 'dd'}.get(metric)
                if key: m.Add(T[key] <= lim)
    if objective == 'max_resolved':
        m.Maximize(sum(X))
    else:
        weights = {
            # role-flavoured, integer weights; these only shape the menu, agents judge it
            'pilot':    dict(fuelw=1, absorbed=5, wait=3, pax=1200),
            'atc':      dict(arr=10, air=10, mk=5),
            'ground':   dict(dd=10, chg=1800, ot=30, wait=5),      # overtime at 1.5x
            'balanced': dict(air=20, dd=10, chg=1800, ot=15, pax=600, wait=3),
        }[objective]
        m.Minimize(sum(w * (mk if k == 'mk' else T[k]) for k, w in weights.items()))
    return m, V


def _hint(s, m, V, plan):
    """Warm-start from a validated greedy plan (same grid), so the solver never does worse than it."""
    if plan is None: return
    asg = {a.flight_id: a for a in plan.assignments}
    for f in s.flights:
        x, L, GI, GO, D, gb, cb, kl, kw, kj = V[f.id]
        a = asg.get(f.id)
        m.AddHint(x, 1 if a else 0)
        if not a: continue
        m.AddHint(kl, (a.landing - f.ready) // GRID); m.AddHint(kw, (a.gate_in - a.landing - f.taxi) // GRID)
        m.AddHint(kj, (a.gate_out - a.gate_in - f.turnaround) // GRID)
        for g, b in gb.items(): m.AddHint(b, 1 if g == a.gate else 0)
        for i, b in enumerate(cb): m.AddHint(b, 1 if i == a.crew else 0)


def _solve(m):
    sol = cp_model.CpSolver()
    sol.parameters.num_workers = 1
    sol.parameters.random_seed = 0
    sol.parameters.max_deterministic_time = DET_TIME
    st = sol.Solve(m)
    return sol, st


def _extract(s, sol, V, source):
    assigned, unresolved = [], []
    for f in s.flights:
        x, L, GI, GO, D, gb, cb = V[f.id][:7]
        if not sol.Value(x): unresolved.append(f.id); continue
        g = next(g for g, b in gb.items() if sol.Value(b))
        c = next(i for i, b in enumerate(cb) if sol.Value(b))
        assigned.append(Assignment(flight_id=f.id, landing=sol.Value(L), gate=g, gate_in=sol.Value(GI),
                                   gate_out=sol.Value(GO), departure=sol.Value(D), crew=c))
    return finalize(s, assigned, unresolved, source)


def solve_menu(s: Scenario, warm=None):
    plans = []
    m, V = _build(s, 'max_resolved')
    _hint(s, m, V, warm)
    sol, st = _solve(m)
    n = len(s.flights)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], {'status': sol.StatusName(st), 'proven': False, 'max_resolved': None, 'n': n}
    best = int(round(sol.ObjectiveValue()))
    proof = {'status': sol.StatusName(st), 'proven': st == cp_model.OPTIMAL, 'max_resolved': best, 'n': n,
             'model': f'{GRID}s grid; constraints identical to engine.validate(); synthetic rules'}
    rejected = 0
    for obj in ('balanced', 'pilot', 'atc', 'ground'):
        m, V = _build(s, obj, resolved_at_least=best)
        if warm is not None and warm.metrics['resolved'] >= best: _hint(s, m, V, warm)
        sol, st = _solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE): continue
        p = _extract(s, sol, V, f'solver:{obj}')
        if all(c['passed'] for c in validate(s, p)): plans.append(p)
        else: rejected += 1
    proof['solver_plans_rejected_by_validator'] = rejected
    return plans, proof


def mediate(s: Scenario, resolved: int, limits: dict, label: str, warm=None):
    """Search for a plan meeting all roles' stated limits. Returns (plan or None, status)."""
    m, V = _build(s, 'balanced', resolved_at_least=resolved, limits=limits)
    sol, st = _solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE): return None, sol.StatusName(st)
    p = _extract(s, sol, V, f'solver:mediated-{label}')
    return (p if all(c['passed'] for c in validate(s, p)) else None), sol.StatusName(st)
