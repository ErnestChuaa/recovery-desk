from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from . import constraints as C

Wake = Literal['SUPER', 'HEAVY', 'LARGE', 'SMALL']          # FAA categories used by the workbook
_LEGACY_WAKE = {'H': 'HEAVY', 'M': 'LARGE', 'L': 'SMALL'}     # earlier H/M/L labels still load


def _wake_tables():
    c = C.load()
    return {k: dict(v) for k, v in c['wake_arrival_s'].items()}, {k: dict(v) for k, v in c['wake_departure_s'].items()}


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Window(Model):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start: raise ValueError('window end must exceed start')
        return self


class Move(Model):
    """A fixed background runway movement (another flight we do not negotiate)."""
    time: int = Field(ge=0)
    kind: Literal['A', 'D'] = 'D'
    wake: Wake = 'LARGE'

    @field_validator('wake', mode='before')
    @classmethod
    def legacy(cls, v): return _LEGACY_WAKE.get(v, v)


class Flight(Model):
    id: str = Field(min_length=1, max_length=60)
    ready: int = Field(ge=0)                     # earliest landing (ETA at runway)
    latest: int = Field(ge=0)                    # latest landing that keeps the final fuel reserve (hard)
    scheduled_departure: int = Field(ge=0)
    turnaround: int = Field(default=2100, ge=60)
    taxi: int = Field(default=300, ge=0)         # taxi-in seconds
    taxi_out: int | None = Field(default=None, ge=0)  # None -> same as taxi
    wake: Wake = 'LARGE'
    contingency: int = Field(default=0, ge=0)    # contingency fuel as seconds of holding (5% of trip)
    duty_end: int | None = Field(default=None, ge=0)  # flight crew duty limit: must land by this time
    enroute_absorb: int = Field(default=0, ge=0)  # delay absorbable by slowing down en route (flight-plan change)
    gates: list[str] = Field(min_length=1)
    preferred_gate: str
    connections: int = Field(default=0, ge=0)
    connection_deadline: int = Field(default=100000, ge=0)
    emergency: bool = False
    motion_event: bool = False                   # reported turbulence / motion for this flight
    note: str = Field(default='', max_length=1200)

    @field_validator('wake', mode='before')
    @classmethod
    def legacy(cls, v): return _LEGACY_WAKE.get(v, v)

    @property
    def tout(self) -> int:
        return self.taxi if self.taxi_out is None else self.taxi_out

    @property
    def land_by(self) -> int:
        # Hard landing limit is the final fuel reserve only. Flight-crew duty is a Pilot-agent limit
        # (an airborne aircraft must still land; going past duty needs an extension decision).
        return self.latest

    @model_validator(mode='after')
    def valid(self):
        if self.latest < self.ready: raise ValueError('latest must be >= ready')
        if self.preferred_gate not in self.gates: raise ValueError('preferred gate must be compatible')
        return self


class Scenario(Model):
    id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    horizon: int = Field(default=14400, ge=600, le=86400)
    separation: int = Field(default=60, ge=1)    # runway occupancy floor under every wake value
    wake_arrival_s: dict[str, dict[str, int]] = Field(default_factory=lambda: _wake_tables()[0])
    wake_departure_s: dict[str, dict[str, int]] = Field(default_factory=lambda: _wake_tables()[1])
    active_runways: int = Field(default=1, ge=1, le=10)   # 1 = one mixed stream; >=2 = arrival and departure streams
    max_ops_per_hour: int | None = Field(default=None, ge=1)  # all runway movements in any 60-min window
    crew_count: int = Field(default=2, ge=1, le=20)
    crew_shift_end: list[int] = []               # per crew; empty -> no shift limit (no overtime)
    hold_pad_capacity: int = Field(default=2, ge=0, le=20)
    max_ground_wait: int = Field(default=2400, ge=0)
    decision_time: int = Field(default=0, ge=0)  # when the plan is decided (gate-change notice counts from here)
    gate_change_notice: int = Field(default=1200, ge=0)
    flights: list[Flight] = Field(min_length=1, max_length=10)
    gates: dict[str, list[Window]]
    runway_closed: list[Window] = []
    background: list[Move] = []
    runway_reserved: list[int] = []              # legacy: background departures as bare times
    taxi_closed: list[Window] = []
    focal_id: str
    provenance: dict = {}

    @model_validator(mode='before')
    @classmethod
    def drop_legacy(cls, data):
        if isinstance(data, dict): data = {k: v for k, v in data.items() if k != 'wake_buffer'}
        return data

    @model_validator(mode='after')
    def unique(self):
        ids = [f.id for f in self.flights]
        if len(ids) != len(set(ids)) or self.focal_id not in ids: raise ValueError('flight IDs must be unique; focal must exist')
        if any(g not in self.gates for f in self.flights for g in f.gates): raise ValueError('unknown gate')
        if any(f.latest > self.horizon for f in self.flights): raise ValueError('latest exceeds horizon')
        if self.crew_shift_end and len(self.crew_shift_end) != self.crew_count: raise ValueError('crew_shift_end must list one time per crew')
        for table in (self.wake_arrival_s, self.wake_departure_s):
            for a in C.CATEGORIES:
                for b in C.CATEGORIES:
                    if table.get(a, {}).get(b) is None: raise ValueError('wake tables must cover SUPER/HEAVY/LARGE/SMALL pairs')
        return self

    @property
    def segregated(self) -> bool:
        return self.active_runways >= 2

    def moves(self):
        """All fixed background movements, including legacy bare times (treated as LARGE departures)."""
        return list(self.background) + [Move(time=t) for t in self.runway_reserved]

    def gap(self, lead_kind, lead_wake, fol_kind, fol_wake):
        """Seconds required between two movements, or None if they use different streams."""
        if self.segregated and lead_kind != fol_kind: return None
        table = self.wake_arrival_s if lead_kind == fol_kind == 'A' else self.wake_departure_s
        return max(self.separation, table[lead_wake][fol_wake])

    def max_gap(self):
        return max(self.separation, *(v for t in (self.wake_arrival_s, self.wake_departure_s) for r in t.values() for v in r.values()))


class Assignment(Model):
    flight_id: str
    landing: int
    gate: str
    gate_in: int
    gate_out: int
    departure: int
    crew: int


class Plan(Model):
    id: str
    assignments: list[Assignment]
    unresolved: list[str] = []
    metrics: dict[str, float] = {}
    details: list[dict] = []
    source: str = ''


class Choice(Model):
    ranked_ids: list[str]
    explanation: str = Field(default='', max_length=1500)


class Telemetry(Model):
    device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    flight_id: str
    boot_id: str = Field(min_length=1, max_length=40)
    seq: int = Field(ge=0)
    uptime_ms: int = Field(ge=0)
    emergency_flag: bool = False
    motion_score: float = Field(default=0, ge=0, le=20)


class NegotiateRequest(Model):
    mode: Literal['fixed', 'claude'] = 'fixed'


class Disruption(Model):
    kind: Literal['runway_closure', 'taxi_closure', 'gate_closure', 'latest', 'emergency', 'reset_emergency',
                  'turbulence', 'reset_turbulence', 'delay']
    start: int = Field(default=0, ge=0)
    end: int = Field(default=1, ge=1)
    flight_id: str | None = None
    gate: str | None = None


def demo(seed=0):
    """Six-flight synthetic fixture using the workbook's Medium-hub limits (3 spare gates, 35-min
    minimum turnaround, 60 movements per hour, 3 runways so arrival and departure streams, 45 min
    spare fuel, 5% contingency). Flight times are synthetic."""
    import random
    r = random.Random(seed)
    limits = C.load()['classes']['Medium hub']; fuel = C.load()['fuel']
    gates = [f'G{i + 1}' for i in range(limits['spare_gates_at_any_time'])]
    flights = []
    for i in range(6):
        ready = 600 + i * 420
        block = 5400 + r.randrange(0, 4) * 900                       # synthetic scheduled block time
        flights.append(Flight(
            id=f'F{i+1}', ready=ready, latest=ready + int(fuel['spare_on_arrival_min'] * 60),
            scheduled_departure=ready + 3600 + i * 300, turnaround=limits['min_turnaround_min'] * 60,
            wake='HEAVY' if i in (1, 4) else 'LARGE', contingency=int(block * fuel['contingency_pct'] / 100),
            enroute_absorb=300 if ready >= 1200 else 0,
            gates=gates, preferred_gate=gates[i % len(gates)],
            connections=10 + i * 3, connection_deadline=ready + 2400,
            note='Prefer contact gate G1 for assisted transfers.' if i == 0 else ''))
    shift = int(fuel['ground_shift_h'] * 3600)
    return Scenario(id=f'demo-{seed}', focal_id='F1', flights=flights,
        gates={g: [] for g in gates}, active_runways=limits['active_runways'], max_ops_per_hour=limits['max_runway_ops_per_hour'],
        runway_closed=[Window(start=0, end=1500)], crew_count=2, crew_shift_end=[shift - 7 * 3600, shift - 6 * 3600],   # 8-h shifts that began 7 h and 6 h before scenario start
        provenance={'kind': 'synthetic', 'seed': seed, 'time_unit': 'seconds from scenario start',
            'rules': 'Limits from the organisers\' workbook (Medium hub class); flight times, crews and connections are synthetic',
            'constraints': {'airport_class': 'Medium hub', **limits, 'source': C.load()['source']},
            'scope': 'Arrival and departure streams, 3 spare gates, 2 ground crews on 8-hour shifts, 2 ground-hold positions'})
