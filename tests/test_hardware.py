"""ESP32 board contract: POST /api/event and GET /api/status/{FLIGHT_A|FLIGHT_B}."""
import time
from fastapi.testclient import TestClient
from recovery.api import create_app

KEYS = {'flight_id', 'status', 'led', 'landing_time', 'gate', 'emergency_active'}
STATUSES = {'WAITING', 'NEGOTIATING', 'AWAITING_APPROVAL', 'PLAN_APPROVED', 'UNRESOLVED'}
PAIRS = {'WAITING': ('YELLOW', False), 'NEGOTIATING': ('YELLOW', True), 'AWAITING_APPROVAL': ('YELLOW', True),
         'PLAN_APPROVED': ('GREEN', False), 'UNRESOLVED': ('RED', True)}


def status(c, path):
    r = c.get('/api/status/' + path); assert r.status_code == 200
    j = r.json(); assert set(j) == KEYS and j['status'] in STATUSES
    assert (j['led'], j['emergency_active']) == PAIRS[j['status']], j
    return j


def settle(c, path, waiting_for=('NEGOTIATING',), timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = status(c, path)
        if j['status'] not in waiting_for: return j
        time.sleep(.1)
    raise AssertionError('still ' + j['status'])


def test_full_board_cycle(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        a, b = status(c, 'FLIGHT_A'), status(c, 'FLIGHT_B')
        assert a['status'] == b['status'] == 'WAITING' and a['flight_id'] == 'FLIGHT A' and b['flight_id'] == 'FLIGHT B'
        r = c.post('/api/event', json={'flight_id': 'FLIGHT A', 'event': 'EMERGENCY'}).json()
        assert r['status'] == 'NEGOTIATING' and r['emergency_active'] is True       # immediate, no flicker
        assert status(c, 'FLIGHT_A')['status'] == 'NEGOTIATING'
        assert status(c, 'FLIGHT_B')['status'] == 'WAITING'                         # B's board is not in an emergency
        a = settle(c, 'FLIGHT_A')
        assert a['status'] == 'AWAITING_APPROVAL' and a['landing_time'].startswith('T+') and a['gate'] != '--'
        run_id = c.get('/runs/' + [e for e in c.get('/events').json() if e['type'] == 'run_started'][-1]['run_id']).json()['id']
        assert c.post(f'/runs/{run_id}/approve').status_code == 200
        a, b = status(c, 'FLIGHT_A'), status(c, 'FLIGHT_B')
        assert a['status'] == b['status'] == 'PLAN_APPROVED' and a['led'] == 'GREEN' and a['emergency_active'] is False
        assert b['landing_time'].startswith('T+') and b['gate'] != '--'
        r = c.post('/api/event', json={'flight_id': 'FLIGHT A', 'event': 'CLEAR'}).json()
        assert r['status'] == 'WAITING' and r['emergency_active'] is False
        assert status(c, 'FLIGHT_B')['status'] == 'WAITING'                         # old approval is stale


def test_disruption_on_imu_and_debounce(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        for _ in range(4):   # a shake sends several events; only the first changes anything
            assert c.post('/api/event', json={'flight_id': 'FLIGHT A', 'event': 'DISRUPTION'}).json()['status'] in ('NEGOTIATING', 'AWAITING_APPROVAL')
        assert c.get('/state').json()['flights'][0]['motion_event'] is True
        started = [e for e in c.get('/events').json() if e['type'] == 'run_started']
        assert len(started) == 1
        settle(c, 'FLIGHT_A')


def test_path_normalisation(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        for p in ('FLIGHT_A', 'flight_a', 'FLIGHT%20A', 'FlightA', 'A'):
            assert status(c, p)['flight_id'] == 'FLIGHT A'
        assert c.get('/api/status/FLIGHT_C').status_code == 404
        assert c.post('/api/event', json={'flight_id': 'FLIGHT B', 'event': 'PANIC'}).status_code == 422


def test_unresolved_is_red(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        c.post('/disruptions', json={'kind': 'runway_closure', 'start': 0, 'end': 14000})
        c.post('/api/event', json={'flight_id': 'FLIGHT_B', 'event': 'EMERGENCY'})
        j = settle(c, 'FLIGHT_B')
        assert j['status'] == 'UNRESOLVED' and j['led'] == 'RED'


def test_dashboard_change_makes_boards_stale(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        c.post('/api/event', json={'flight_id': 'FLIGHT A', 'event': 'EMERGENCY'})
        settle(c, 'FLIGHT_A')
        c.post('/disruptions', json={'kind': 'gate_closure', 'gate': 'G2', 'start': 0, 'end': 600})
        assert status(c, 'FLIGHT_A')['status'] == 'NEGOTIATING'          # plan no longer current; run again


def test_bindings(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        h = c.get('/api/hardware').json()
        assert h['bindings'] == {'FLIGHT A': 'F1', 'FLIGHT B': 'F2'} and len(h['boards']) == 2
        assert c.put('/api/hardware/bindings', json={'flight_b': 'F4'}).json()['bindings']['FLIGHT B'] == 'F4'
        assert c.put('/api/hardware/bindings', json={'flight_a': 'NOPE'}).status_code == 422
        c.post('/api/event', json={'flight_id': 'FLIGHT B', 'event': 'EMERGENCY'})
        assert next(f for f in c.get('/state').json()['flights'] if f['id'] == 'F4')['emergency'] is True


def test_latest_run_lets_a_late_dashboard_approve(tmp_path):
    with TestClient(create_app(str(tmp_path / 't.db'))) as c:
        assert c.get('/runs/latest').json() is None
        c.post('/api/event', json={'flight_id': 'FLIGHT A', 'event': 'EMERGENCY'})
        settle(c, 'FLIGHT_A')
        r = c.get('/runs/latest').json()
        assert r['status'] == 'REVIEW_REQUIRED'
        assert c.post(f"/runs/{r['id']}/approve").status_code == 200
        assert status(c, 'FLIGHT_A')['status'] == 'PLAN_APPROVED'
        c.post('/api/event', json={'flight_id': 'FLIGHT A', 'event': 'CLEAR'})
        assert c.get('/runs/latest').json() is None
