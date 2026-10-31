"""2015 DOT flights CSV -> labelled recovery scenario.

The focal row's delay causes drive the disruption:
  LATE_AIRCRAFT_DELAY -> the flight's runway ETA moves later by that many minutes
                         (an inbound-aircraft delay is known before arrival: derived, no hindsight)
  WEATHER_DELAY       -> a CONSTRUCTED runway closure of that many minutes starting at the
                         focal flight's ETA (DOT records delay minutes, not closures)
Every value is labelled historical, derived or synthetic in scenario.provenance.

Other flights at the destination become fixed background runway movements, so the
negotiated flights compete with real traffic. NOTE: the supplied challenge file
contains only delayed flights, so background traffic is an undercount.
"""
import argparse, csv, json, random, statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from .models import Flight, Move, Scenario
from . import constraints as C

FIELDS = ('YEAR', 'MONTH', 'DAY', 'AIRLINE', 'FLIGHT_NUMBER', 'TAIL_NUMBER', 'ORIGIN_AIRPORT', 'DESTINATION_AIRPORT', 'SCHEDULED_DEPARTURE',
          'SCHEDULED_ARRIVAL', 'SCHEDULED_TIME', 'DISTANCE', 'TAXI_IN', 'TAXI_OUT', 'LATE_AIRCRAFT_DELAY', 'WEATHER_DELAY',
          'ARRIVAL_DELAY', 'AIR_SYSTEM_DELAY', 'CANCELLED', 'DIVERTED')
# Realised outcomes of the historical flight. Kept for display only; never given to agents or the scheduler.
OUTCOME_FIELDS = ('ARRIVAL_DELAY', 'AIR_SYSTEM_DELAY', 'TAXI_IN', 'TAXI_OUT')
REQUIRED = {'YEAR', 'MONTH', 'DAY', 'AIRLINE', 'DESTINATION_AIRPORT', 'SCHEDULED_ARRIVAL', 'WEATHER_DELAY', 'LATE_AIRCRAFT_DELAY'}


def minutes(raw):
    n = int(raw)
    if n == 2400: return 1440
    h, m = divmod(n, 100)
    if h > 23 or m > 59: raise ValueError('invalid HHMM')
    return h * 60 + m


def num(x, default=0.0):
    try: return float(x) if x not in (None, '') else default
    except ValueError: return default


class FlightIndex:
    """Reads the CSV once; rows indexed by local date and airport."""
    def __init__(self, path, airport_map=None):
        self.path = Path(path); self.map = airport_map or {}
        self.rows = {}; self.by_day_dest = defaultdict(list); self.by_day_origin = defaultdict(list)
        taxi_in = defaultdict(list); taxi_out = defaultdict(list)
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            missing = REQUIRED - set(reader.fieldnames or [])
            if missing: raise ValueError('Expected DOT/Kaggle-style columns; missing ' + str(sorted(missing)))
            for i, r in enumerate(reader, 1):
                rec = {k: r.get(k, '') for k in FIELDS}
                rec['row'] = i; rec['day'] = (r['YEAR'], r['MONTH'], r['DAY'])
                self.rows[i] = rec
                self.by_day_dest[(rec['day'], r['DESTINATION_AIRPORT'])].append(i)
                self.by_day_origin[(rec['day'], r['ORIGIN_AIRPORT'])].append(i)
                if r.get('TAXI_IN'): taxi_in[r['DESTINATION_AIRPORT']].append(num(r['TAXI_IN']))
                if r.get('TAXI_OUT'): taxi_out[r['ORIGIN_AIRPORT']].append(num(r['TAXI_OUT']))
        self.taxi_in = {a: statistics.median(v) for a, v in taxi_in.items()}
        self.taxi_out = {a: statistics.median(v) for a, v in taxi_out.items()}

    def airport(self, code):
        if code.isdigit():
            if code not in self.map: raise ValueError('numeric airport ID requires --airport-map JSON lookup')
            return self.map[code]
        return code

    def eligible(self, i):
        r = self.rows[i]
        return num(r['WEATHER_DELAY']) > 15 or num(r['LATE_AIRCRAFT_DELAY']) > 20


def _wake(r):
    # PROXY ONLY: DOT has no aircraft type. Long stage length suggests a widebody (FAA Heavy);
    # everything else is treated as FAA Large (A320/737/CRJ/ERJ class).
    return 'HEAVY' if num(r['DISTANCE']) >= 2000 else 'LARGE'


def build(ix: FlightIndex, row_number, limit=8, window=90, holding_allowance=None, hold_pads=2, scope='same_first'):
    if not 1 <= limit <= 10: raise ValueError('limit must be 1..10')
    focal = ix.rows.get(row_number)
    if not focal: raise ValueError('focal row not found')
    if not ix.eligible(row_number):
        raise ValueError('focal row must have WEATHER_DELAY >15 or LATE_AIRCRAFT_DELAY >20')
    if focal['CANCELLED'] == '1' or focal['DIVERTED'] == '1': raise ValueError('focal flight was cancelled or diverted')
    day = focal['day']; dest_code = focal['DESTINATION_AIRPORT']; dest = ix.airport(dest_code)
    anchor = minutes(focal['SCHEDULED_ARRIVAL'])

    # Operating limits for this airport from the organisers' workbook.
    lim = C.airport(dest); fuel = C.load()['fuel']
    gates = tuple(f'G{i + 1}' for i in range(lim['spare_gates_at_any_time']))
    if holding_allowance is None: holding_allowance = int(fuel['spare_on_arrival_min'])

    def eta(r):  # derived: scheduled arrival + known inbound-aircraft delay (minutes)
        return minutes(r['SCHEDULED_ARRIVAL']) + int(num(r['LATE_AIRCRAFT_DELAY']))

    same = []
    for i in ix.by_day_dest[(day, dest_code)]:
        r = ix.rows[i]
        if r['CANCELLED'] == '1' or r['DIVERTED'] == '1' or not r['SCHEDULED_ARRIVAL']: continue
        if scope == 'same_airline' and r['AIRLINE'] != focal['AIRLINE']: continue
        if abs(minutes(r['SCHEDULED_ARRIVAL']) - anchor) <= window: same.append(i)
    # Focal first, then the focal airline's flights, then other airlines' (scope='same_first'), nearest first.
    same.sort(key=lambda i: (i != row_number, ix.rows[i]['AIRLINE'] != focal['AIRLINE'], abs(minutes(ix.rows[i]['SCHEDULED_ARRIVAL']) - anchor), i))
    chosen = same[:limit]
    weather = int(num(focal['WEATHER_DELAY']))
    focal_eta = eta(focal)
    closure = (focal_eta, focal_eta + weather) if weather > 15 else None
    origin = min([eta(ix.rows[i]) for i in chosen] + ([closure[0]] if closure else [])) - 10
    sec = lambda m: int((m - origin) * 60)

    taxi_in = int(round(ix.taxi_in.get(dest_code, 7))) * 60
    taxi_out = int(round(ix.taxi_out.get(dest_code, 15))) * 60
    # Earliest scheduled departure of each tail today (any airport), for flight-crew report time.
    tail_first = {}
    for (d_, _a), rows_ in ix.by_day_origin.items():
        if d_ != day: continue
        for j in rows_:
            t_ = ix.rows[j]['TAIL_NUMBER']
            if t_ and ix.rows[j]['SCHEDULED_DEPARTURE']:
                m_ = minutes(ix.rows[j]['SCHEDULED_DEPARTURE'])
                tail_first[t_] = min(tail_first.get(t_, m_), m_)
    flights, fprov = [], {}
    outbound_rows = set()   # tail-rotation departures modelled as the negotiated flights' own departures
    rnd_ids = set()
    for k, i in enumerate(sorted(chosen, key=lambda i: (eta(ix.rows[i]), i))):
        r = ix.rows[i]
        fid = f"{r['AIRLINE']}{r['FLIGHT_NUMBER']}"
        if fid in rnd_ids: fid += f'-r{i}'
        rnd_ids.add(fid)
        ready = sec(eta(r)); wk = _wake(r)
        turnaround = lim['min_turnaround_min'] * 60
        # Flight crew duty (workbook: 13 h from report). Report = 60 min before the tail's first scheduled
        # departure today in the file (derived; the file holds delayed flights only, so this may be late).
        report = tail_first.get(r['TAIL_NUMBER'])
        duty_end = sec(report - 60 + fuel['flight_duty_max_h'] * 60) if report is not None else None
        contingency = int(num(r['SCHEDULED_TIME'], 60) * 60 * fuel['contingency_pct'] / 100)
        # derived rotation link: next scheduled departure of the same tail from this airport today
        nxt = sorted((minutes(ix.rows[j]['SCHEDULED_DEPARTURE']), j) for j in ix.by_day_origin[(day, dest_code)]
                     if r['TAIL_NUMBER'] and ix.rows[j]['TAIL_NUMBER'] == r['TAIL_NUMBER']
                     and minutes(ix.rows[j]['SCHEDULED_DEPARTURE']) > minutes(r['SCHEDULED_ARRIVAL']))
        if nxt:
            sched_dep = sec(nxt[0][0]); rotation = {'kind': 'derived_tail_rotation', 'outbound_row': nxt[0][1]}
            outbound_rows.add(nxt[0][1])
        else: sched_dep = sec(minutes(r['SCHEDULED_ARRIVAL']) + 60); rotation = {'kind': 'synthetic', 'rule': 'scheduled arrival + 60 min'}
        remaining = min(ready, int(num(r['SCHEDULED_TIME'], 60) * 60))
        absorb = (int(0.05 * remaining) // 30) * 30   # ~5% speed reduction over remaining flight time (synthetic)
        rr = random.Random(i)
        conn = rr.randrange(0, 31)
        flights.append(Flight(id=fid, ready=ready, latest=ready + holding_allowance * 60, scheduled_departure=max(0, sched_dep),
            turnaround=turnaround, taxi=taxi_in, taxi_out=taxi_out, wake=wk, enroute_absorb=absorb,
            contingency=contingency, duty_end=max(0, duty_end) if duty_end is not None else None,
            gates=list(gates), preferred_gate=gates[k % len(gates)], connections=conn,
            connection_deadline=max(0, sec(minutes(r['SCHEDULED_ARRIVAL']) + 40)),
            note=(f"Historical trigger row {i}: WEATHER_DELAY={r['WEATHER_DELAY']} min, LATE_AIRCRAFT_DELAY={r['LATE_AIRCRAFT_DELAY']} min. "
                  f"These delay causes build the scenario; the recorded outcome is withheld from agents." if i == row_number else '')))
        fprov[fid] = {'source_row': i, 'eta': 'derived: SCHEDULED_ARRIVAL + LATE_AIRCRAFT_DELAY', 'rotation': rotation,
                      'wake': 'proxy: DISTANCE>=2000 mi -> HEAVY else LARGE',
                      'latest': f'workbook: ETA + {holding_allowance} min spare fuel; the 30-min final reserve is never used',
                      'contingency': f"workbook: {fuel['contingency_pct']:g}% of scheduled block time",
                      'duty_end': 'workbook 13 h duty; report time derived (60 min before the tail\'s first departure in the file)' if duty_end is not None else 'no departure for this tail in the file',
                      'enroute_absorb': 'synthetic: ~5% speed reduction over remaining flight time',
                      'turnaround': f"workbook: {lim['min_turnaround_min']} min minimum at {dest}", 'connections': 'synthetic: seeded by source row'}
    focal_id = next(fid for fid, p in fprov.items() if p['source_row'] == row_number)
    latest_end = max(max(f.latest, f.scheduled_departure) for f in flights)
    horizon = min(86400, latest_end + 3 * 3600)
    # Background runway traffic (fixed): all other arrivals and departures at this airport today.
    chosen_set = set(chosen); bg = []; dropped = 0
    for key, which in (((day, dest_code), 'arr'), ((day, dest_code), 'dep')):
        src = ix.by_day_dest[key] if which == 'arr' else ix.by_day_origin[key]
        for j in src:
            if j in chosen_set or j in outbound_rows: continue   # never count a negotiated movement twice
            r = ix.rows[j]
            if r['CANCELLED'] == '1' or (which == 'arr' and r['DIVERTED'] == '1'): continue
            t = sec(eta(r)) if which == 'arr' else sec(minutes(r['SCHEDULED_DEPARTURE']))
            if not 0 <= t <= horizon: continue
            if closure and sec(closure[0]) <= t < sec(closure[1]): dropped += 1; continue
            bg.append(Move(time=t, kind='A' if which == 'arr' else 'D', wake=_wake(r)))
    runway_closed = [{'start': sec(closure[0]), 'end': sec(closure[1])}] if closure else []
    crews = 2 if len(flights) <= 5 else 3
    s = Scenario(id=f'dot-{row_number}', flights=flights, gates={g: [] for g in gates}, focal_id=focal_id,
        horizon=horizon, runway_closed=runway_closed, background=sorted(bg, key=lambda m: m.time), crew_count=crews,
        # 8-hour ground shifts (workbook) that started 6 h, 5 h, ... before the scenario: ends are synthetic.
        crew_shift_end=[min(horizon, 7200 + 3600 * c) for c in range(crews)], hold_pad_capacity=hold_pads,
        active_runways=lim['active_runways'], max_ops_per_hour=lim['max_runway_ops_per_hour'],
        provenance={'kind': 'historical_seed_with_synthetic_operations', 'source_file': ix.path.name,
            'source_rows': chosen, 'focal_row': row_number, 'focal_source': {k: focal[k] for k in FIELDS if k not in OUTCOME_FIELDS},
            'recorded_outcome_withheld': {k: focal[k] for k in OUTCOME_FIELDS}, 'airport': dest,
            'local_date': str(date(*map(int, day))), 'time_origin_local_minutes': origin,
            'time_note': 'Seconds from time_origin on the destination-local date. Flights crossing midnight are not modelled.',
            'disruption': ({'type': 'runway_closure', 'label': 'CONSTRUCTED from WEATHER_DELAY (DOT records delay minutes, not closures)',
                            'minutes': weather, 'window': runway_closed[0]} if closure else
                           {'type': 'late_inbound_only', 'label': 'LATE_AIRCRAFT_DELAY shifts ETAs; no runway closure constructed'}),
            'background_traffic': {'movements': len(bg), 'dropped_inside_closure': dropped,
                'excluded_as_negotiated': len(chosen_set) + len(outbound_rows),
                'note': 'All other arrivals/departures at this airport in the file. The challenge file contains only delayed flights, so real traffic is higher.'},
            'flights': fprov,
            'constraints': {**lim, 'source': C.load()['source'], 'loaded_from_workbook': C.load()['loaded_from_workbook'],
                            'stream_mode': 'arrival and departure streams' if lim['active_runways'] >= 2 else 'one mixed stream'},
            'derived_fields': ['ETA (scheduled arrival + late-aircraft delay)', 'tail rotation links', 'median taxi-in/out at airport',
                               'flight-crew report time'],
            'workbook_fields': ['runways and hourly movement cap', 'spare gates', 'minimum turnaround', 'FAA wake separation',
                                'spare fuel and final reserve', 'contingency fuel', 'holding fuel burn', 'flight-crew duty', 'ground-crew shift and overtime rate',
                                'gate-change notice'],
            'synthetic_fields': ['preferred gates', 'crew count and shift start times', 'wake category proxy', 'en-route absorption',
                                 'connections', 'hold-pad capacity'],
            'not_used': ('Realised outcomes (ARRIVAL_DELAY, AIR_SYSTEM_DELAY, actual taxi times) are withheld from agents and the scheduler. '
                         'WEATHER_DELAY and LATE_AIRCRAFT_DELAY are used, as the brief asks, to construct the disruption.'),
            'scope': (f'{len(flights)} arrivals within +/-{window} min negotiate ({sum(ix.rows[i]["AIRLINE"] == focal["AIRLINE"] for i in chosen)} '
                      f'from {focal["AIRLINE"]}; others fill the window when scope=same_first); remaining traffic is fixed background')})
    return s


def load(path, row_number, airport_map=None, limit=8, **kw):
    return build(FlightIndex(path, airport_map), row_number, limit=limit, **kw)


def suggest(ix: FlightIndex, n=10, min_flights=4, seed=0):
    """Documented selection rule for evaluation windows: random eligible rows with >= min_flights arrivals within +/-90 min."""
    out = []; rnd = random.Random(seed); ids = list(ix.rows); rnd.shuffle(ids)
    for i in ids:
        r = ix.rows[i]
        if not ix.eligible(i) or r['CANCELLED'] == '1' or r['DIVERTED'] == '1': continue
        anchor = minutes(r['SCHEDULED_ARRIVAL'])
        k = sum(1 for j in ix.by_day_dest[(r['day'], r['DESTINATION_AIRPORT'])]
                if ix.rows[j]['CANCELLED'] != '1' and abs(minutes(ix.rows[j]['SCHEDULED_ARRIVAL']) - anchor) <= 90)
        if k >= min_flights: out.append(i)
        if len(out) >= n: break
    return out


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('csv'); p.add_argument('--row', type=int)
    p.add_argument('--airport-map'); p.add_argument('--output', default='scenario.json')
    p.add_argument('--suggest', type=int, help='print N candidate rows with >=4 arrivals within 90 min')
    a = p.parse_args()
    mapping = json.loads(Path(a.airport_map).read_text()) if a.airport_map else None
    ix = FlightIndex(a.csv, mapping)
    if a.suggest:
        for i in suggest(ix, a.suggest):
            r = ix.rows[i]
            print(i, r['AIRLINE'], r['FLIGHT_NUMBER'], r['DESTINATION_AIRPORT'], 'WEATHER', r['WEATHER_DELAY'], 'LATE_AIRCRAFT', r['LATE_AIRCRAFT_DELAY'])
    else:
        if not a.row: p.error('--row is required unless --suggest is used')
        Path(a.output).write_text(build(ix, a.row).model_dump_json(indent=2), encoding='utf-8')
