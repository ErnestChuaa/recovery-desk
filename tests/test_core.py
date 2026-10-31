import asyncio, csv, os, time
import pytest
from fastapi.testclient import TestClient
from recovery.models import demo, Window, Choice, Plan, Assignment, Flight, Scenario
from recovery.engine import candidates, candidate_menu, fallback, validate, schedule, fcfs, finalize
from recovery.agents import negotiate, assess, POLICY
from recovery.api import create_app
from recovery.data import load


from recovery.benchmark import smoke   # shared stress profiles


def failed(s, p): return [c for c in validate(s, p) if not c['passed']]


# ---------------------------------------------------------------- engine and solver
def test_generated_plans_independently_validate():
    for i in range(6):
        s = smoke(i)
        for p in candidates(s): assert not failed(s, p), (i, p.source)


def test_proven_infeasible_not_faked():
    s = demo(); s.runway_closed = [Window(start=0, end=s.horizon)]
    r = asyncio.run(negotiate(s))
    assert r['outcome'] == 'PROVEN_INFEASIBLE' and r['proof']['proven'] and r['proof']['max_resolved'] == 0
    assert len(r['plan']['unresolved']) == 6 and r['violations'] == 0
    assert all(d['rule'] == 'RUNWAY_WINDOW_CONFLICT' for d in r['diagnostics'])


def test_solver_places_flights_greedy_misses():
    s = smoke(5)                                   # emergency plus taxiway closure
    assert fcfs(s).metrics['resolved'] < 6
    plans, proof = candidate_menu(s)
    assert proof['proven'] and proof['max_resolved'] == 6
    assert any(p.metrics['resolved'] == 6 and 'solver:' in p.source for p in plans)


def test_partial_is_labelled_proven_when_solver_proves_it():
    r = asyncio.run(negotiate(smoke(3)))           # one crew: cannot turn all six
    assert r['outcome'] == 'PROVEN_INFEASIBLE' and r['proof']['max_resolved'] == r['plan']['metrics']['resolved'] < 6
    assert any(d['rule'] == 'PROVEN_JOINT_INFEASIBILITY' for d in r['diagnostics'])


def test_mutations_caught():
    s = demo(); p = fallback(s); a = p.assignments[0]
    a.landing = s.flights[0].latest + 1
    assert any(not x['passed'] for x in validate(s, p))
    p = fallback(s); p.assignments.append(p.assignments[0])
    assert any(c['rule'] == 'ACCOUNTING' and not c['passed'] for c in validate(s, p))


def test_boundary_and_gate_overlap():
    s = demo(); s.runway_closed = []; s.flights = s.flights[:1]; f = s.flights[0]; f.ready = f.latest = 360
    p = schedule(s, s.flights)
    assert not p.unresolved and p.assignments[0].landing == 360
    a = p.assignments[0]; s.gates[a.gate].append(Window(start=a.gate_in, end=a.gate_out))
    assert any(not c['passed'] and c['rule'] == 'GATE_RESERVATION' for c in validate(s, p))


def _two(wake1, wake2, gap):
    s = Scenario(id='w', focal_id='X', crew_count=2, gates={'A': [], 'B': []}, flights=[
        Flight(id='X', ready=0, latest=3000, scheduled_departure=5000, turnaround=1200, wake=wake1, gates=['A'], preferred_gate='A'),
        Flight(id='Y', ready=0, latest=3000, scheduled_departure=5000, turnaround=1200, wake=wake2, gates=['B'], preferred_gate='B')])
    p = finalize(s, [Assignment(flight_id='X', landing=990, gate='A', gate_in=1290, gate_out=2490, departure=2790, crew=0),
                     Assignment(flight_id='Y', landing=990 + gap, gate='B', gate_in=1290 + gap, gate_out=2490 + gap, departure=2790 + gap, crew=1)], [], 't')
    return s, p


def test_wake_separation_is_leader_follower_specific():
    s, p = _two('HEAVY', 'LARGE', 120)   # Large landing 120 s behind Heavy: workbook 5 nm = 129 s at 140 kt
    assert any(c['rule'] == 'RUNWAY_WAKE_SEPARATION' and not c['passed'] for c in validate(s, p))
    s, p = _two('LARGE', 'HEAVY', 120)   # Heavy behind Large: 'any other pair' 3 nm = 78 s
    assert not failed(s, p)


def test_ground_wait_needs_hold_pad_capacity():
    s = demo(); s.runway_closed = []; s.flights = s.flights[:1]; f = s.flights[0]
    s.gates = {g: [Window(start=0, end=2400)] for g in s.gates}   # every gate busy until 2400
    p = schedule(s, s.flights)
    a = p.assignments[0]
    assert a.gate_in > a.landing + f.taxi and p.metrics['ground_wait'] > 0 and not failed(s, p)
    s.hold_pad_capacity = 0
    assert any(c['rule'] == 'HOLD_PAD_CAPACITY' and not c['passed'] for c in validate(s, p))
    q = schedule(s, s.flights)                                   # without pads it must hold airborne instead
    assert q.unresolved or q.metrics['ground_wait'] == 0


def test_crew_overtime_and_speed_absorb_metrics():
    s = demo(); p = fcfs(s)
    d = {x['flight_id']: x for x in p.details}
    for a in p.assignments:
        f = next(f for f in s.flights if f.id == a.flight_id)
        end = a.gate_in + f.turnaround
        assert d[a.flight_id]['crew_overtime'] == max(0, end - max(a.gate_in, s.crew_shift_end[a.crew]))
        assert d[a.flight_id]['speed_absorb'] == min(a.landing - f.ready, f.enroute_absorb)
        assert d[a.flight_id]['airborne_hold'] + d[a.flight_id]['speed_absorb'] == a.landing - f.ready


# ---------------------------------------------------------------- negotiation
def test_two_round_consensus_with_concession():
    r = asyncio.run(negotiate(demo()))
    assert r['rounds'] == 2 and r['model_calls'] == 0 and r['outcome'] == 'CONSENSUS'
    msgs = [x for x in r['trace'] if x['type'] == 'agent_message']
    assert len(msgs) == 6 and {m['sender'] for m in msgs} == {'ATC', 'PILOT', 'GROUND'}
    assert r['plan']['id'] in [c['plan']['id'] for c in r['candidates']]
    # demo is tuned so strict limits conflict; the agreed plan carries recorded concessions
    assert r['concessions'] and all(f['tier'] == 'strict' for v in r['concessions'].values() for f in v)
    chosen = next(c for c in r['candidates'] if c['plan']['id'] == r['plan']['id'])
    assert set(chosen['accepted_by']) == {'ATC', 'PILOT', 'GROUND'}
    assert any(m['rejections'] for m in msgs)                     # visible rejected alternatives with reasons


def test_mediation_runs_when_no_plan_preferred_by_all():
    r = asyncio.run(negotiate(demo()))
    coord = [e for e in r['trace'] if e['type'] == 'coordinator']
    if not coord[0]['preferred_by_all']:
        assert any(e.get('action') == 'MEDIATION' for e in coord)


def test_relaxed_limit_breach_never_accepted():
    for i in range(6):
        r = asyncio.run(negotiate(smoke(i)))
        if r['decision'] != 'CONSENSUS': continue
        chosen = next(c for c in r['candidates'] if c['plan']['id'] == r['plan']['id'])
        assert all(f['tier'] == 'strict' for v in chosen['role_findings'].values() for f in (v or []))


def _plan_with(metrics_over):
    s = demo(); p = fcfs(s); p.metrics.update(metrics_over); return s, p


def test_turbulence_and_emergency_limits():
    s, p = _plan_with({'turbulence_holding': 600, 'emergency_holding': 60})
    best = {k: 0 for k in p.metrics}; best['min_fuel_margin'] = 10**6
    v = assess('PILOT', s, p, best)
    codes = {(f['reason_code'], f['tier']) for f in v['limit_findings']}
    assert ('TURBULENCE_HOLD', 'relaxed') in codes and ('EMERGENCY_HELD', 'relaxed') in codes
    emergency = next(x for x in POLICY['PILOT'] if x[0] == 'EMERGENCY_HELD')
    assert emergency[2] == emergency[3]            # never relaxed


def test_model_failure_falls_back():
    async def fail(*args): raise ValueError('malformed')
    r = asyncio.run(negotiate(demo(), chooser=fail))
    assert r['fallback_reason'] == 'AGENT_ERROR' and r['outcome'] == 'FALLBACK_PLAN' and r['violations'] == 0


def test_unknown_id_never_accepted():
    async def bad(*args): return Choice(ranked_ids=['invented'])
    r = asyncio.run(negotiate(demo(), chooser=bad))
    assert r['fallback_reason'] == 'AGENT_ERROR'


def test_no_agreement_falls_back():
    async def decline(*args): return Choice(ranked_ids=[])
    r = asyncio.run(negotiate(demo(), chooser=decline))
    assert r['fallback_reason'] == 'NO_AGREEMENT_IN_MENU' and r['decision'] == 'FALLBACK' and r['violations'] == 0


def test_agents_disagree_no_false_consensus():
    async def split(role, round_no, options, *a):
        ids = [o['id'] for o in options]
        return Choice(ranked_ids=ids[:1] if role == 'ATC' else ids[1:])
    r = asyncio.run(negotiate(demo(), chooser=split))
    assert r['decision'] == 'FALLBACK'


# ---------------------------------------------------------------- API
def wait(client, rid):
    for _ in range(600):
        r = client.get('/runs/' + rid).json()
        if r['status'] != 'RUNNING': return r
        time.sleep(.05)
    raise AssertionError('run timed out')


def test_full_api_approval_and_stale(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        assert c.get('/health').status_code == 200
        r = wait(c, c.post('/runs', json={'mode': 'fixed'}).json()['id'])
        assert r['outcome'] == 'CONSENSUS'
        assert c.post('/runs/' + r['id'] + '/approve').status_code == 200
        c.post('/disruptions', json={'kind': 'turbulence', 'flight_id': 'F1'})
        assert c.get('/state').json()['flights'][0]['motion_event']
        assert c.post('/runs/' + r['id'] + '/approve').status_code == 409
        assert c.get('/runs/' + r['id']).json()['status'] == 'STALE'
        assert c.get('/events').json()
        assert c.get('/ui').status_code == 200
        assert c.get('/ui/fonts/barlow-latin-400-normal.woff2').status_code == 200
        assert c.get('/ui/fonts/..%2F..%2Frecovery%2Fapi.py').status_code == 404
        assert c.get('/results/historical-30').status_code == 200 and c.get('/results/secrets').status_code == 404
        assert c.get('/events/latest').json()['id'] > 0


def test_incomplete_plan_cannot_be_approved(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        c.post('/disruptions', json={'kind': 'runway_closure', 'start': 0, 'end': 14000})
        r = wait(c, c.post('/runs', json={'mode': 'fixed'}).json()['id'])
        assert r['plan']['unresolved'] and c.post('/runs/' + r['id'] + '/approve').status_code == 409


def test_delay_disruption_moves_eta_and_fuel_limit(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        f0 = c.get('/state').json()['flights'][1]
        c.post('/disruptions', json={'kind': 'delay', 'flight_id': f0['id'], 'start': 600})
        f1 = c.get('/state').json()['flights'][1]
        assert f1['ready'] == f0['ready'] + 600 and f1['latest'] == f0['latest'] + 600


def test_telemetry_latched_and_duplicates(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        t = dict(device_id='node1', flight_id='F1', boot_id='a', seq=1, uptime_ms=100, emergency_flag=True)
        r = c.post('/telemetry', json=t).json(); assert r['accepted'] and 'run_id' in r
        assert c.post('/telemetry', json=t).json()['accepted'] is False
        version = c.get('/state').json()['version']
        t['seq'] = 2; assert 'run_id' not in c.post('/telemetry', json=t).json()
        assert c.get('/state').json()['version'] == version
        run = wait(c, r['run_id'])
        if run['status'] == 'REVIEW_REQUIRED' and not run['plan']['unresolved']:
            c.post('/runs/' + run['id'] + '/approve')
            t['seq'] = 3; c.post('/telemetry', json=t)    # heartbeat: the run may outlast the 3 s offline timeout
            assert c.get('/devices/node1/status').json()['led'] == 'green'
        c.post('/disruptions', json={'kind': 'gate_closure', 'gate': 'G1', 'start': 0, 'end': 500})
        assert c.get('/devices/node1/status').json()['led'] != 'green'


def test_claude_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        assert c.post('/runs', json={'mode': 'claude'}).status_code == 422


def test_state_persists_without_restoring_approval(tmp_path):
    db = str(tmp_path / 'test.db')
    with TestClient(create_app(db)) as c:
        c.post('/disruptions', json={'kind': 'emergency', 'flight_id': 'F1'})
    with TestClient(create_app(db)) as c:
        assert c.get('/state').json()['flights'][0]['emergency']


def test_mid_run_change_is_stale(tmp_path, monkeypatch):
    import recovery.api as api
    original = api.negotiate
    async def slow(*args, **kwargs):
        await asyncio.sleep(.15)
        return await original(*args, **kwargs)
    monkeypatch.setattr(api, 'negotiate', slow)
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        rid = c.post('/runs', json={'mode': 'fixed'}).json()['id']
        c.post('/disruptions', json={'kind': 'emergency', 'flight_id': 'F2'})
        wait(c, rid)
        assert c.get('/runs/' + rid).json()['status'] == 'STALE'
        assert c.post('/runs/' + rid + '/approve').status_code == 409


def test_offline_device_and_motion_debounce(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        t = dict(device_id='n', flight_id='F1', boot_id='b', seq=0, uptime_ms=0, motion_score=1.2)
        for i in range(1, 4):
            t['seq'] = i; r = c.post('/telemetry', json=t).json()
            assert ('run_id' in r) == (i == 3)
        assert c.get('/state').json()['flights'][0]['motion_event']
        c.app.state.store.devices['n']['seen'] -= 4
        assert c.get('/devices/n/status').json()['status'] == 'OFFLINE'


# ---------------------------------------------------------------- CSV import
HEAD = ['YEAR', 'MONTH', 'DAY', 'AIRLINE', 'FLIGHT_NUMBER', 'TAIL_NUMBER', 'ORIGIN_AIRPORT', 'DESTINATION_AIRPORT',
        'SCHEDULED_DEPARTURE', 'SCHEDULED_ARRIVAL', 'SCHEDULED_TIME', 'DISTANCE', 'TAXI_IN', 'TAXI_OUT',
        'LATE_AIRCRAFT_DELAY', 'WEATHER_DELAY', 'ARRIVAL_DELAY', 'CANCELLED', 'DIVERTED']


def _csv(tmp_path, rows):
    file = tmp_path / 'flights.csv'
    with file.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=HEAD); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, '') for k in HEAD})
    return file


def _row(**kw):
    base = dict(YEAR=2015, MONTH=8, DAY=3, AIRLINE='AA', FLIGHT_NUMBER=1, TAIL_NUMBER='N1', ORIGIN_AIRPORT='DFW',
                DESTINATION_AIRPORT='ORD', SCHEDULED_DEPARTURE='0900', SCHEDULED_ARRIVAL='1200', SCHEDULED_TIME='150',
                DISTANCE='800', TAXI_IN='8', TAXI_OUT='15', LATE_AIRCRAFT_DELAY='0', WEATHER_DELAY='0',
                ARRIVAL_DELAY='30', CANCELLED='0', DIVERTED='0')
    base.update(kw); return base


def test_csv_delay_drives_scenario(tmp_path):
    file = _csv(tmp_path, [
        _row(FLIGHT_NUMBER=10, TAIL_NUMBER='N10', WEATHER_DELAY='25', LATE_AIRCRAFT_DELAY='30', ARRIVAL_DELAY='777'),  # focal
        _row(FLIGHT_NUMBER=11, TAIL_NUMBER='N11', SCHEDULED_ARRIVAL='1220', LATE_AIRCRAFT_DELAY='25'),
        _row(FLIGHT_NUMBER=12, TAIL_NUMBER='N10', ORIGIN_AIRPORT='ORD', DESTINATION_AIRPORT='LGA',
             SCHEDULED_DEPARTURE='1345', SCHEDULED_ARRIVAL='1650', LATE_AIRCRAFT_DELAY='40'),               # focal tail's outbound
        _row(AIRLINE='UA', FLIGHT_NUMBER=99, TAIL_NUMBER='N99', SCHEDULED_ARRIVAL='1500'),                # background
    ])
    s = load(file, 1)
    focal = next(f for f in s.flights if f.id == s.focal_id)
    other = next(f for f in s.flights if f.id == 'AA11')
    assert focal.ready - other.ready == (12 * 60 + 30 - (12 * 60 + 20 + 25)) * 60          # ETA = sched + late-aircraft
    w = s.runway_closed[0]; assert w.start == focal.ready and w.end - w.start == 25 * 60   # constructed closure
    assert s.provenance['disruption']['label'].startswith('CONSTRUCTED')
    assert s.provenance['flights'][s.focal_id]['rotation']['kind'] == 'derived_tail_rotation'
    assert focal.scheduled_departure == (13 * 60 + 45 - s.provenance['time_origin_local_minutes']) * 60
    assert s.provenance['background_traffic']['movements'] >= 1
    assert focal.scheduled_departure not in s.runway_reserved          # rotation departure not double-counted
    assert 'ARRIVAL_DELAY' not in s.provenance['focal_source'] and 'ARRIVAL_DELAY' not in focal.note
    assert s.provenance['recorded_outcome_withheld']['ARRIVAL_DELAY'] == '777'
    r = asyncio.run(negotiate(s)); assert r['violations'] == 0


def test_csv_numeric_airport_requires_map(tmp_path):
    file = _csv(tmp_path, [_row(DESTINATION_AIRPORT='13930', WEATHER_DELAY='20')])
    with pytest.raises(ValueError, match='numeric airport'): load(file, 1)
    s = load(file, 1, {'13930': 'ORD'})
    assert s.provenance['airport'] == 'ORD'


def test_csv_rejects_ineligible_row(tmp_path):
    file = _csv(tmp_path, [_row(WEATHER_DELAY='10', LATE_AIRCRAFT_DELAY='5')])
    with pytest.raises(ValueError, match='WEATHER_DELAY'): load(file, 1)


@pytest.mark.skipif(not os.getenv('FLIGHTS_CSV'), reason='set FLIGHTS_CSV to run against the real DOT file')
def test_solver_and_validator_agree_on_real_windows():
    from recovery.data import FlightIndex, build, suggest
    ix = FlightIndex(os.environ['FLIGHTS_CSV'])
    for r in suggest(ix, 12, seed=3) + [169609, 292105, 3125]:
        s = build(ix, r); plans, proof = candidate_menu(s)
        assert proof['solver_plans_rejected_by_validator'] == 0, r
        assert max(p.metrics['resolved'] for p in plans) == proof['max_resolved'], r


@pytest.mark.skipif(not os.getenv('FLIGHTS_CSV'), reason='set FLIGHTS_CSV to run against the real DOT file')
def test_real_dot_row_end_to_end():
    s = load(os.environ['FLIGHTS_CSV'], 169609)
    r = asyncio.run(negotiate(s))
    assert r['violations'] == 0 and r['outcome'] in ('CONSENSUS', 'FALLBACK_PLAN', 'PROVEN_INFEASIBLE', 'NO_PLAN_FOUND')


# ---------------------------------------------------------------- regressions from external review
def test_taxiway_queue_plan_rejected_so_solver_and_validator_agree():
    s = Scenario(id='repro', focal_id='X', crew_count=1, hold_pad_capacity=0,
                 flights=[Flight(id='X', ready=0, latest=600, scheduled_departure=1200, turnaround=600, taxi=300, gates=['A'], preferred_gate='A')],
                 gates={'A': [Window(start=900, end=14000)]}, runway_closed=[Window(start=1100, end=2000)])
    p = finalize(s, [Assignment(flight_id='X', landing=0, gate='A', gate_in=300, gate_out=900, departure=2010, crew=0)], [], 'hand')
    assert any(c['rule'] == 'TASK_ORDER' and not c['passed'] for c in validate(s, p))
    _, proof = candidate_menu(s)
    assert proof['proven'] and proof['max_resolved'] == 0


def test_off_grid_plan_rejected():
    s = demo(); p = fcfs(s); a = p.assignments[0]
    a.landing += 10; a.gate_in += 10; a.gate_out += 10; a.departure += 10
    assert any(c['rule'] == 'GRID_ALIGNED' and not c['passed'] for c in validate(s, p))


def test_no_generator_beats_a_proof():
    import random
    for i in range(6):
        s = smoke(i); plans, proof = candidate_menu(s)
        assert proof['proven']
        rnd = random.Random(i)
        for k in range(40):
            order = sorted(s.flights, key=lambda f: (not f.emergency, rnd.random()))
            assert schedule(s, order, k % 3).metrics['resolved'] <= proof['max_resolved']
        assert all(p.metrics['resolved'] <= proof['max_resolved'] for p in plans)
        assert proof['solver_plans_rejected_by_validator'] == 0                 # solver never builds an invalid plan
        assert max(p.metrics['resolved'] for p in plans) == proof['max_resolved']   # the menu reaches the proof


def test_overtime_counts_work_after_shift_once():
    s = Scenario(id='ot', focal_id='A1', crew_count=1, crew_shift_end=[1500], gates={'A': [], 'B': []}, flights=[
        Flight(id='A1', ready=0, latest=600, scheduled_departure=5000, turnaround=1500, taxi=300, gates=['A'], preferred_gate='A'),
        Flight(id='A2', ready=1200, latest=1800, scheduled_departure=6000, turnaround=300, taxi=300, gates=['B'], preferred_gate='B')])
    p = finalize(s, [Assignment(flight_id='A1', landing=0, gate='A', gate_in=300, gate_out=1800, departure=2100, crew=0),
                     Assignment(flight_id='A2', landing=1500, gate='B', gate_in=1800, gate_out=2100, departure=2400, crew=0)], [], 'hand')
    assert p.metrics['crew_overtime'] == 600          # 300 s of A1 + all 300 s of A2, not 300 + 600


def test_solver_overtime_matches_metric():
    from recovery.solver import mediate
    s = demo(); plans, proof = candidate_menu(s)
    target = min(p.metrics['crew_overtime'] for p in plans if p.metrics['resolved'] == proof['max_resolved'])
    p, status = mediate(s, proof['max_resolved'], {'crew_overtime': target}, 'test')
    assert p is not None and p.metrics['crew_overtime'] <= target


def test_event_cursor_survives_run_started(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        first = c.get('/events/latest').json()['id']
        wait(c, c.post('/runs', json={'mode': 'fixed'}).json()['id'])
        c.post('/disruptions', json={'kind': 'emergency', 'flight_id': 'F2'})
        ev = c.get(f'/events?after={first}').json()
        ids = [e['event_id'] for e in ev]
        assert ids == sorted(ids) and all(isinstance(i, int) for i in ids)
        types = [e['type'] for e in ev]
        assert 'run_started' in types and 'run_completed' in types and types[-1] == 'state_changed'
        started = next(e for e in ev if e['type'] == 'run_started')
        assert isinstance(started['run_id'], str)
        assert c.get(f'/events?after={ids[types.index("run_started")]}').json()   # pagination continues past it


# ---------------------------------------------------------------- organisers' workbook limits
def _forced_feasible(s, plan):
    """Fix a plan's decisions inside the CP-SAT model: it must be feasible there too."""
    from ortools.sat.python import cp_model
    from recovery import solver
    m, V = solver._build(s, 'max_resolved')
    for a in plan.assignments:
        x, L, GI, GO, D, gb, cb = V[a.flight_id][:7]
        m.Add(x == 1); m.Add(L == a.landing); m.Add(GI == a.gate_in); m.Add(GO == a.gate_out)
        m.Add(gb[a.gate] == 1); m.Add(cb[a.crew] == 1)
    for fid in plan.unresolved: m.Add(V[fid][0] == 0)
    sol = cp_model.CpSolver(); sol.parameters.max_time_in_seconds = 10
    return sol.StatusName(sol.Solve(m)) in ('OPTIMAL', 'FEASIBLE')


def _busy():
    """Demo with fixed background traffic, a tight hourly cap and one mixed runway stream."""
    from recovery.models import Move
    s = demo(); s.active_runways = 1; s.max_ops_per_hour = 9
    s.background = [Move(time=t, kind=k, wake=w) for t, k, w in
                    [(1800, 'A', 'HEAVY'), (2100, 'D', 'LARGE'), (2400, 'A', 'LARGE'), (3300, 'D', 'HEAVY'), (3900, 'A', 'LARGE')]]
    return s


def test_every_validated_plan_is_feasible_in_solver_model():
    for s in [smoke(i) for i in range(6)] + [_busy()]:
        for p in candidates(s, use_solver=False):
            assert _forced_feasible(s, p), (s.id, p.source)


def test_workbook_is_loaded():
    from recovery import constraints as C
    c = C.load()
    assert c['loaded_from_workbook'] and len(c['airports']) > 300
    ord_ = C.airport('ORD')
    assert (ord_['active_runways'], ord_['gates'], ord_['max_runway_ops_per_hour'], ord_['min_turnaround_min'], ord_['spare_gates_at_any_time']) == (4, 150, 90, 45, 3)
    assert c['wake_departure_s']['SUPER']['HEAVY'] == 180 and c['wake_departure_s']['LARGE']['SMALL'] == 60
    assert c['wake_arrival_s']['HEAVY']['LARGE'] == 129              # 5 nm at 140 kt
    assert c['fuel']['final_reserve_min'] == 30 and c['fuel']['overtime_multiplier'] == 1.5 and c['rules']['max_turns'] == 3


def test_hourly_cap_respected_and_checked():
    s = _busy(); p = fcfs(s)
    assert not failed(s, p)
    s.max_ops_per_hour = 6                          # tighter than the plan uses
    assert any(c['rule'] == 'RUNWAY_HOURLY_CAPACITY' and not c['passed'] for c in validate(s, p))
    q = fcfs(s)                                     # the heuristic respects the tighter cap
    assert not failed(s, q)


def test_streams_mixed_vs_segregated():
    s, p = _two('LARGE', 'LARGE', 0)                # landing and departure of X/Y at identical times
    s.active_runways = 2
    assert not any(c['rule'] == 'RUNWAY_WAKE_SEPARATION' and not c['passed'] and c['evidence']['kinds'][0] != c['evidence']['kinds'][1]
                   for c in validate(s, p))
    s, p = _two('LARGE', 'LARGE', 60); s.active_runways = 1
    p.assignments[1].landing = p.assignments[0].departure - 30          # arrival 30 s before a departure, one stream
    assert any(c['rule'] == 'RUNWAY_WAKE_SEPARATION' and not c['passed'] for c in validate(s, p))


def test_gate_change_notice_duty_limit_and_reserve():
    s = demo(); s.runway_closed = []; s.flights = s.flights[:1]; f = s.flights[0]
    p = schedule(s, s.flights); a = p.assignments[0]
    assert a.gate == f.preferred_gate                         # lands 10 min after decision: no gate change allowed
    a.gate = next(g for g in f.gates if g != f.preferred_gate)
    assert any(c['rule'] == 'GATE_CHANGE_NOTICE' and not c['passed'] for c in validate(s, p))
    p = schedule(s, s.flights)
    f.duty_end = p.assignments[0].landing - 30; f.ready = f.duty_end - 600          # 30 s of avoidable overrun
    p = finalize(s, p.assignments, [], 'duty')
    assert p.metrics['duty_overrun'] == 30 and not any(c['rule'] == 'FLIGHT_CREW_DUTY' for c in validate(s, p))
    best = {k: 0 for k in p.metrics}; best['min_fuel_margin'] = 10**6
    assert any(x['reason_code'] == 'FLIGHT_CREW_DUTY' and x['tier'] == 'relaxed' for x in assess('PILOT', s, p, best)['limit_findings'])
    f.duty_end = None; f.ready = 600; p = schedule(s, s.flights); f.latest = p.assignments[0].landing - 30
    assert any(c['rule'] == 'FINAL_FUEL_RESERVE' and not c['passed'] for c in validate(s, p))


def test_contingency_is_a_concession_not_a_rejection():
    s, p = _plan_with({'contingency_used': 300})
    best = {k: 0 for k in p.metrics}; best['min_fuel_margin'] = 10**6
    v = assess('PILOT', s, p, best)
    f = next(x for x in v['limit_findings'] if x['reason_code'] == 'CONTINGENCY_FUEL_USED')
    assert f['tier'] == 'strict' and f['relaxed_limit'] is None
    assert not any(x['reason_code'] == 'CONTINGENCY_FUEL_USED' and x['tier'] == 'relaxed' for x in v['limit_findings'])
    import json; json.dumps(v)                       # no infinities in agent payloads


def test_fallback_logs_why():
    async def decline(*args): return Choice(ranked_ids=[])
    r = asyncio.run(negotiate(demo(), chooser=decline))
    assert r['decision'] == 'FALLBACK' and 'turns' in r['fallback_explanation'] and r['turn_limit'] == 3


def test_consensus_log_and_constraints_endpoints(tmp_path):
    with TestClient(create_app(str(tmp_path / 'test.db'))) as c:
        r = wait(c, c.post('/runs', json={'mode': 'fixed'}).json()['id'])
        log = c.get(f"/runs/{r['id']}/log").json()
        assert [t['turn'] for t in log['turns']] == [1, 2] and all(len(t['messages']) == 3 for t in log['turns'])
        assert log['turn_limit'] == 3 and log['output'] and all('gate' in o for o in log['output'])
        k = c.get('/constraints').json()
        assert k['loaded_from_workbook'] and k['max_turns'] == 3 and k['stated_changes'] and k['wake_table']


def test_importer_applies_airport_row(tmp_path):
    file = _csv(tmp_path, [_row(FLIGHT_NUMBER=10, WEATHER_DELAY='25', SCHEDULED_TIME='150'), _row(FLIGHT_NUMBER=11, SCHEDULED_ARRIVAL='1220')])
    s = load(file, 1)
    f = next(x for x in s.flights if x.id == s.focal_id)
    assert (s.active_runways, s.max_ops_per_hour, list(s.gates)) == (4, 90, ['G1', 'G2', 'G3'])   # ORD row
    assert f.turnaround == 45 * 60 and f.latest - f.ready == 45 * 60 and f.contingency == 450
    assert s.provenance['constraints']['size_class'] == 'Large hub'
