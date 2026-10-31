"""Operational limits from the organisers' workbook (data/challenge2_operational_constraints.xlsx).

Every number the scheduler, validator, solver and agents use for airports, wake separation, fuel and
crew comes from here. Edit the workbook to change a limit; restart the server to pick it up. If the
file is missing, the built-in defaults below (copied from the same workbook) are used and the
provenance says so. Interpretations and modelling choices are listed in STATED_CHANGES and shown
on the dashboard, as the rules of play require.
"""
import math, os, re
from functools import lru_cache
from pathlib import Path

WORKBOOK = Path(os.getenv('CONSTRAINTS_XLSX', Path(__file__).resolve().parent.parent / 'data' / 'challenge2_operational_constraints.xlsx'))

CATEGORIES = ('SUPER', 'HEAVY', 'LARGE', 'SMALL')
APPROACH_SPEED_KT = 140          # stated assumption: converts arrival separation (nm) to time

# Defaults equal to the workbook's values (used only if the workbook cannot be read).
_DEFAULT_WAKE = [  # leader, follower, nm, departure seconds
    ('SUPER', 'HEAVY', 6, 180), ('SUPER', 'LARGE', 7, 180), ('SUPER', 'SMALL', 8, 180),
    ('HEAVY', 'HEAVY', 4, 120), ('HEAVY', 'LARGE', 5, 120), ('HEAVY', 'SMALL', 6, 120),
    ('LARGE', 'SMALL', 4, 60)]
_DEFAULT_OTHER = (3, 60)
_DEFAULT_CLASSES = {
    'Large hub': dict(active_runways=4, gates=150, max_runway_ops_per_hour=90, min_turnaround_min=45, spare_gates_at_any_time=3),
    'Medium hub': dict(active_runways=3, gates=70, max_runway_ops_per_hour=60, min_turnaround_min=35, spare_gates_at_any_time=3),
    'Small hub': dict(active_runways=2, gates=30, max_runway_ops_per_hour=36, min_turnaround_min=35, spare_gates_at_any_time=2),
    'Non-hub': dict(active_runways=1, gates=8, max_runway_ops_per_hour=20, min_turnaround_min=35, spare_gates_at_any_time=2)}
_DEFAULT_FUEL = dict(final_reserve_min=30, contingency_pct=5, burn_narrow_kg_h=2400, burn_wide_kg_h=6500,
                     spare_on_arrival_min=45, flight_duty_max_h=13, ground_shift_h=8, overtime_multiplier=1.5,
                     gate_change_notice_min=20)
_DEFAULT_RULES = {'max_turns': 3}

STATED_CHANGES = [
    'Runways: the airport is modelled as one arrival stream and one departure stream (segregated mode) when it has 2 or more active runways, '
    'and one mixed stream when it has 1. Wake separation applies within a stream; total movements are capped at max_runway_ops_per_hour.',
    'Hourly cap is checked over every 60-minute window starting on a 15-minute boundary.',
    f'Arrival wake separation in nm is converted to time at an assumed {APPROACH_SPEED_KT} kt approach speed. '
    'Departure separation uses the workbook seconds. Arrival-departure pairs in a mixed stream use the departure seconds.',
    'Spare fuel on arrival (45 min of holding) is read as fuel above the 30-minute final reserve, so the hard landing limit is ETA + 45 min; '
    'the final reserve is never planned into. Contingency fuel (5% of trip, as minutes of the scheduled block time) is the Pilot\'s preferred buffer.',
    'Holding fuel is reported in kg using the workbook burn rates (HEAVY/SUPER = widebody, others = narrowbody). Delay absorbed by slowing down en route is assumed to burn no extra fuel.',
    'Flight crew duty (13 h from report) is a Pilot limit, not a hard rule: an airborne aircraft must still land. The Pilot rejects plans '
    'that add holding past the crew\'s duty end; if only such plans exist, the fallback may use one and the breach goes to the operator '
    '(an extension decision). Report time = 60 min before the tail\'s first scheduled departure that day in the file.',
    'Ground crew: 8-hour shifts (start times synthetic); work after shift end is overtime, costed at 1.5x in the Ground agent\'s ranking.',
    'Gate change notice: a flight may only be moved off its planned gate if it lands at least 20 min after the decision time (scenario start).',
    'Gates: the negotiated flights share the airport\'s spare_gates_at_any_time gates (G1, G2, ...); other gates are assumed occupied.',
    'Turns: one turn = all three agents send one structured message. We use 2 of the 3 allowed turns; if they do not agree, the fallback decides and the log says why.',
    'Wake category: DOT data has no aircraft type, so stage length >= 2000 mi is treated as HEAVY and everything else as LARGE (a proxy).',
]


def _num(text, default):
    m = re.search(r'[\d,.]+', str(text or ''))
    return float(m.group(0).replace(',', '')) if m else default


@lru_cache(maxsize=1)
def load():
    """Returns a dict with airports, wake tables, fuel/crew rules, rules of play and provenance."""
    out = {'source': str(WORKBOOK.name), 'loaded_from_workbook': False, 'airports': {}, 'classes': dict(_DEFAULT_CLASSES),
           'fuel': dict(_DEFAULT_FUEL), 'rules': dict(_DEFAULT_RULES), 'rules_text': [], 'stated_changes': STATED_CHANGES,
           'approach_speed_kt': APPROACH_SPEED_KT}
    wake = list(_DEFAULT_WAKE); other = _DEFAULT_OTHER
    try:
        import openpyxl
        wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
        ws = wb['airport_capacity']
        rows = list(ws.iter_rows(values_only=True))
        hi = next(i for i, r in enumerate(rows) if r and str(r[0]).strip() == 'IATA_CODE')   # header row, wherever it is
        head = [str(h).strip() if h else '' for h in rows[hi]]
        for r in rows[hi + 1:]:
            if not r or not r[0]: continue
            d = dict(zip(head, r))
            out['airports'][str(d['IATA_CODE']).strip()] = {
                'name': d.get('AIRPORT'), 'size_class': d.get('size_class'),
                'active_runways': int(d['active_runways']), 'gates': int(d['gates']),
                'max_runway_ops_per_hour': int(d['max_runway_ops_per_hour']), 'min_turnaround_min': int(d['min_turnaround_min']),
                'spare_gates_at_any_time': int(d['spare_gates_at_any_time'])}
        wake = []
        wrows = list(wb['wake_separation'].iter_rows(values_only=True))
        wi = next(i for i, r in enumerate(wrows) if r and str(r[0]).strip().lower() == 'leader')
        for r in wrows[wi + 1:]:
            if not r or not r[0]: continue
            leader = str(r[0]).strip()
            if leader.lower().startswith('any other'): other = (int(r[2]), int(r[3])); continue
            wake.append((leader.split()[0].upper(), str(r[1]).strip().upper(), int(r[2]), int(r[3])))
        if not out['airports'] or not wake: raise ValueError('workbook parsed but airport or wake table is empty')
        fuel = {str(r[0]).strip().lower(): r[1] for r in wb['fuel_and_crew'].iter_rows(values_only=True) if r and r[0] and str(r[0]).strip().lower() != 'rule'}
        get = lambda key, default: _num(next((v for k, v in fuel.items() if key in k), None), default)
        shift_text = str(next((v for k, v in fuel.items() if 'ground crew shift' in k), ''))
        mult = re.search(r'([\d.]+)\s*x', shift_text)
        out['fuel'] = dict(final_reserve_min=get('final fuel reserve', 30), contingency_pct=get('contingency', 5),
                           burn_narrow_kg_h=get('narrowbody', 2400), burn_wide_kg_h=get('widebody', 6500),
                           spare_on_arrival_min=get('spare fuel', 45), flight_duty_max_h=get('flight crew max duty', 13),
                           ground_shift_h=get('ground crew shift', 8), overtime_multiplier=float(mult.group(1)) if mult else 1.5,
                           gate_change_notice_min=get('gate change notice', 20))
        rules = [(str(r[0]), str(r[1])) for r in wb['rules_of_play'].iter_rows(values_only=True) if r and r[0] and str(r[0]).strip().lower() != 'topic']
        out['rules_text'] = rules
        turn = next((d for t, d in rules if t.lower() == 'turn'), '')
        m = re.search(r'within (\d+) turns', turn)
        out['rules']['max_turns'] = int(m.group(1)) if m else 3
        out['loaded_from_workbook'] = True
    except FileNotFoundError:          # no workbook: same values built in, and provenance says so
        out['load_error'] = 'workbook not found; using built-in copies of its values'
    except ImportError:
        out['load_error'] = 'openpyxl not installed; using built-in copies of the workbook values'
    # Any other error (a malformed or edited workbook) is raised: silently using defaults would hide it.
    dep = {a: {b: other[1] for b in CATEGORIES} for a in CATEGORIES}
    arr = {a: {b: math.ceil(other[0] * 3600 / APPROACH_SPEED_KT) for b in CATEGORIES} for a in CATEGORIES}
    for leader, follower, nm, secs in wake:
        if leader in dep and follower in dep[leader]:
            dep[leader][follower] = secs
            arr[leader][follower] = math.ceil(nm * 3600 / APPROACH_SPEED_KT)
    out['wake_departure_s'] = dep; out['wake_arrival_s'] = arr
    out['wake_table'] = [dict(leader=l, follower=f, nm=n, departure_s=s) for l, f, n, s in wake] + [dict(leader='any other pair', follower='', nm=other[0], departure_s=other[1])]
    return out


def airport(code):
    """Limits for an airport; unknown airports get the Non-hub class (stated in provenance)."""
    c = load()
    if code in c['airports']: return {**c['airports'][code], 'iata': code, 'from_workbook_row': True}
    return {**c['classes']['Non-hub'], 'size_class': 'Non-hub', 'iata': code, 'from_workbook_row': False}


def burn_kg_per_h(wake):
    f = load()['fuel']
    return f['burn_wide_kg_h'] if wake in ('HEAVY', 'SUPER') else f['burn_narrow_kg_h']
