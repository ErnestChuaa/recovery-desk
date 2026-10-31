import asyncio, json
import httpx
from recovery.agents import choose, negotiate
from recovery.models import Choice, demo


def test_claude_transport_and_schema(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"ranked_ids":["p1"],"explanation":"Prefer earlier arrival."}'}]})
    original = httpx.AsyncClient
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-not-a-real-key'); monkeypatch.setenv('CLAUDE_MODEL', 'test-model')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    c = asyncio.run(choose('PILOT', 1, [{'id': 'p1', 'local_cost': 2, 'strict_ok': True}], 'claude'))
    assert c.ranked_ids == ['p1'] and 'PILOT' in captured[0]['system']
    assert 'CONTINGENCY_FUEL_USED' in captured[0]['system'] and 'CREW_OVERTIME' not in captured[0]['system']   # own limits only
    assert captured[0]['model'] == 'test-model'


def test_claude_cannot_pick_unchecked_plan(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"ranked_ids":["not-on-menu"]}'}]})
    original = httpx.AsyncClient
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k'); monkeypatch.setenv('CLAUDE_MODEL', 'm')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    r = asyncio.run(negotiate(demo(), mode='claude'))
    assert r['fallback_reason'] == 'AGENT_ERROR' and r['violations'] == 0 and 1 <= r['model_calls'] <= 3


def test_separate_contexts_and_votes():
    calls = []
    async def record(role, round_no, options, mode, notes, offers):
        calls.append((role, round_no, options, offers, notes))
        return Choice(ranked_ids=[o['id'] for o in options])
    r = asyncio.run(negotiate(demo(), chooser=record))
    assert r['outcome'] == 'CONSENSUS' and len(calls) == 6
    for role, round_no, options, offers, notes in calls:
        assert all(c['role'] == role for o in options for c in o['checks'])
        assert all('local_cost' in o and 'strict_ok' in o for o in options)
        if round_no == 2:
            assert all(isinstance(ids, list) for ids in offers.values())
            assert all('offered_by' in o for o in options)
        if role == 'ATC': assert 'Prefer contact gate A' not in notes     # flight notes go to PILOT/GROUND only


def test_payload_is_compact():
    sizes = []
    async def record(role, round_no, options, mode, notes, offers):
        sizes.append(len(json.dumps({'o': options, 'n': notes, 'x': offers})))
        return Choice(ranked_ids=[o['id'] for o in options])
    asyncio.run(negotiate(demo(), chooser=record))
    assert max(sizes) < 40000


def test_agents_never_see_recorded_outcome(tmp_path, monkeypatch):
    import csv
    head = ['YEAR', 'MONTH', 'DAY', 'AIRLINE', 'FLIGHT_NUMBER', 'TAIL_NUMBER', 'ORIGIN_AIRPORT', 'DESTINATION_AIRPORT',
            'SCHEDULED_DEPARTURE', 'SCHEDULED_ARRIVAL', 'SCHEDULED_TIME', 'DISTANCE', 'TAXI_IN', 'TAXI_OUT',
            'LATE_AIRCRAFT_DELAY', 'WEATHER_DELAY', 'ARRIVAL_DELAY', 'AIR_SYSTEM_DELAY', 'CANCELLED', 'DIVERTED']
    rows = [dict(YEAR=2015, MONTH=8, DAY=3, AIRLINE='AA', FLIGHT_NUMBER=n, TAIL_NUMBER=f'N{n}', ORIGIN_AIRPORT='DFW',
                 DESTINATION_AIRPORT='ORD', SCHEDULED_DEPARTURE='0900', SCHEDULED_ARRIVAL=arr, SCHEDULED_TIME='150',
                 DISTANCE='800', TAXI_IN='8', TAXI_OUT='15', LATE_AIRCRAFT_DELAY='30', WEATHER_DELAY=w,
                 ARRIVAL_DELAY='4321', AIR_SYSTEM_DELAY='9876', CANCELLED='0', DIVERTED='0')
            for n, arr, w in [(10, '1200', '25'), (11, '1215', '0'), (12, '1230', '0')]]
    f = tmp_path / 'flights.csv'
    with f.open('w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=head); w.writeheader(); w.writerows(rows)
    from recovery.data import load
    s = load(f, 1)
    seen = []
    async def record(role, round_no, options, mode, notes, offers):
        seen.append(json.dumps({'o': options, 'n': notes, 'x': offers}))
        return Choice(ranked_ids=[o['id'] for o in options])
    asyncio.run(negotiate(s, chooser=record))
    bodies = []
    def handler(request):
        bodies.append(request.content.decode())
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"ranked_ids":[]}'}]})
    original = httpx.AsyncClient
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k'); monkeypatch.setenv('CLAUDE_MODEL', 'm')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    asyncio.run(negotiate(s, mode='claude'))
    everything = ' '.join(seen + bodies)
    assert bodies and seen
    for leak in ('ARRIVAL_DELAY', 'AIR_SYSTEM_DELAY', '4321', '9876'):
        assert leak not in everything, leak
