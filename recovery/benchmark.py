"""Evaluation harness.

  python -m recovery.benchmark --csv flights.csv --count 30          # real DOT windows (documented selection rule)
  python -m recovery.benchmark                                        # synthetic smoke profiles
  python -m recovery.benchmark --scenarios dir/                       # your own scenario JSON files
  add --claude for paid LLM-agent runs (needs ANTHROPIC_API_KEY, CLAUDE_MODEL)

Methods compared on the same scenarios, constraints and validator:
  fcfs           status quo: constraint-respecting first-come-first-served (greedy)
  central        central planner: same menu, single declared neutral score, no negotiation
  fixed_greedy   negotiation with greedy-only menu (ablation: no CP-SAT candidates, no mediation)
  fixed          negotiation with fixed-rule agents (full system)
  claude         negotiation with Claude agents (optional)
Writes JSON (all per-run values) and a Markdown summary.
"""
import argparse, asyncio, json, random, statistics, time
from pathlib import Path
from .models import Scenario, Window, demo
from .engine import fcfs, validate, candidate_menu, fallback
from .agents import negotiate

KEYS = ['resolved', 'holding', 'holding_fuel_kg', 'speed_absorb', 'ground_wait', 'departure_delay', 'crew_overtime', 'crew_overtime_cost', 'missed_connections', 'gate_changes', 'holding_stack']


def smoke(i):
    """Six synthetic stress profiles on the workbook-based demo."""
    s = demo(i); profile = i % 6
    if profile == 1: s.runway_closed = [Window(start=0, end=3000)]                       # long closure
    elif profile == 2: s.gates['G1'] = [Window(start=0, end=4200)]; s.gates['G2'] = [Window(start=0, end=3000)]   # gate outages
    elif profile == 3: s.crew_count = 1; s.crew_shift_end = [3600]                      # one ground crew
    elif profile == 4:                                                                    # little spare fuel
        for f in s.flights: f.latest = f.ready + 1200
    elif profile == 5: s.flights[-1].emergency = True; s.taxi_closed = [Window(start=2400, end=3300)]
    s.provenance['smoke_profile'] = profile
    return s


def row(s, method, plan, start, extra=None):
    checks = validate(s, plan)
    return dict(scenario=s.id, flights=len(s.flights), method=method, plan_id=plan.id,
                metrics={k: plan.metrics[k] for k in KEYS}, unresolved=plan.unresolved,
                violations=sum(not c['passed'] for c in checks), runtime_seconds=round(time.monotonic() - start, 3), **(extra or {}))


async def evaluate(ss, claude=False):
    rows = []
    for s in ss:
        t = time.monotonic(); rows.append(row(s, 'fcfs', fcfs(s), t, {'outcome': 'PLAN' if not fcfs(s).unresolved else 'INCOMPLETE'}))
        t = time.monotonic(); pool, proof = candidate_menu(s)
        rows.append(row(s, 'central', fallback(s, pool), t, {'outcome': 'PLAN', 'proof': proof}))
        for method, kw in [('fixed_greedy', dict(use_solver=False)), ('fixed', {})] + ([('claude', dict(mode='claude'))] if claude else []):
            t = time.monotonic(); r = await negotiate(s, **kw)
            from .models import Plan
            p = Plan.model_validate(r['plan'])
            rows.append(row(s, method, p, t, dict(outcome=r['outcome'], decision=r['decision'], fallback_reason=r['fallback_reason'], error_type=r.get('error_type'),
                proof=r['proof'], concessions={k: [f['reason_code'] for f in v] for k, v in r['concessions'].items()},
                mediation=[e.get('tier') for e in r['trace'] if e.get('action') == 'MEDIATION' and e.get('plan_id')],
                model_calls=r['model_calls'])))
    return rows


def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else 0


def summarise(rows, label):
    methods = [m for m in ['fcfs', 'central', 'fixed_greedy', 'fixed', 'claude'] if any(r['method'] == m for r in rows)]
    scen = sorted({r['scenario'] for r in rows}); n = len(scen)
    flights = sum(r['flights'] for r in rows if r['method'] == 'fcfs')
    proven_max = sum((r.get('proof') or {}).get('max_resolved') or 0 for r in rows if r['method'] == 'central')
    rejected = sum((r.get('proof') or {}).get('solver_plans_rejected_by_validator') or 0 for r in rows if r['method'] == 'central')
    L = [f'# Benchmark results\n', f'**{label}** — {n} scenarios, {flights} flights. Simulation results; not real-world savings.\n',
         f'CP-SAT upper bound (proven maximum placeable flights): {proven_max}/{flights} '
         f'({sum(bool((r.get("proof") or {}).get("proven")) for r in rows if r["method"] == "central")}/{n} scenarios proven optimal).\n',
         f'Solver plans rejected by the validator: {rejected} (must be 0; otherwise solver and validator disagree).\n',
         '| Metric | ' + ' | '.join(methods) + ' |', '|---|' + '---|' * len(methods)]
    def col(fn): return [fn([r for r in rows if r['method'] == m]) for m in methods]
    def line(name, vals): L.append(f'| {name} | ' + ' | '.join(str(v) for v in vals) + ' |')
    line('Flights placed', col(lambda rs: f"{sum(r['metrics']['resolved'] for r in rs):.0f}/{flights}"))
    line('Scenarios with every flight placed', col(lambda rs: f"{sum(not r['unresolved'] for r in rs)}/{n}"))
    line('Consensus (unanimous, complete or partial)', col(lambda rs: f"{sum(r.get('decision') == 'CONSENSUS' for r in rs)}/{n}" if 'decision' in rs[0] else 'n/a'))
    line('Fallback invoked', col(lambda rs: f"{sum(r.get('decision') == 'FALLBACK' for r in rs)}/{n}" if 'decision' in rs[0] else 'n/a'))
    line('Mediation plan used in round 2', col(lambda rs: f"{sum(bool(r.get('mediation')) for r in rs)}/{n}" if 'decision' in rs[0] else 'n/a'))
    line('Validator violations in chosen plans', col(lambda rs: sum(r['violations'] for r in rs)))
    for k, name, div in [('holding', 'Airborne holding (min)', 60), ('holding_fuel_kg', 'Holding fuel burned (kg, workbook burn rates)', 1), ('speed_absorb', 'Delay absorbed by speed reduction (min)', 60),
                         ('ground_wait', 'Ground wait for gate (min)', 60), ('departure_delay', 'Outbound departure delay (min)', 60),
                         ('crew_overtime', 'Ground-crew overtime (min)', 60), ('missed_connections', 'Missed-connection pax (synthetic)', 1),
                         ('gate_changes', 'Gate changes', 1)]:
        line(name, col(lambda rs, k=k, div=div: f"{sum(r['metrics'][k] for r in rs) / div:.0f}"))
    line('Runtime median / p95 (s)', col(lambda rs: f"{statistics.median([r['runtime_seconds'] for r in rs]):.2f} / {pct([r['runtime_seconds'] for r in rs], .95):.2f}"))
    L.append('\nTotals are summed over flights that were placed; always read them beside "Flights placed". Missed-connection counts are synthetic.\n')
    # paired comparison vs FCFS
    by = {(r['scenario'], r['method']): r for r in rows}
    L.append('## Paired comparison: full system (fixed) vs FCFS, per scenario\n')
    L.append('| Metric | better | same | worse |'); L.append('|---|---|---|---|')
    for k in ['resolved', 'holding', 'holding_fuel_kg', 'crew_overtime', 'missed_connections', 'departure_delay', 'ground_wait', 'gate_changes']:
        b = s_ = w = 0
        for sc in scen:
            a, f = by[(sc, 'fixed')]['metrics'][k], by[(sc, 'fcfs')]['metrics'][k]
            if k == 'resolved': a, f = -a, -f
            if by[(sc, 'fixed')]['metrics']['resolved'] != by[(sc, 'fcfs')]['metrics']['resolved'] and k != 'resolved': continue
            if a < f: b += 1
            elif a == f: s_ += 1
            else: w += 1
        L.append(f'| {k} | {b} | {s_} | {w} |')
    L.append('\n(Non-"resolved" rows compare only scenarios where both methods placed the same number of flights.)\n')
    fails = [r for r in rows if r['method'] == 'fixed' and (r['unresolved'] or r.get('decision') == 'FALLBACK')]
    L.append('## Failure and fallback cases (full system)\n')
    for r in fails:
        L.append(f"- `{r['scenario']}`: outcome {r['outcome']}, decision {r['decision']}"
                 f"{', reason ' + r['fallback_reason'] if r.get('fallback_reason') else ''}; unresolved {r['unresolved'] or 'none'}; "
                 f"proof {r['proof'].get('status')} max {r['proof'].get('max_resolved')}/{r['flights']}")
    if not fails: L.append('- none in this set')
    return '\n'.join(L) + '\n'


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scenarios', help='directory of scenario JSON files')
    p.add_argument('--csv', help='DOT flights.csv: evaluate rows chosen by data.suggest (documented rule)')
    p.add_argument('--count', type=int, default=30); p.add_argument('--seed', type=int, default=0)
    p.add_argument('--exclude', type=int, nargs='*', default=[169609, 280983], help='rows used for the demo (chosen by looking at outcomes), excluded from evaluation')
    p.add_argument('--claude', action='store_true'); p.add_argument('--output', default='benchmark-results.json')
    a = p.parse_args()
    if a.csv:
        from .data import FlightIndex, build, suggest
        ix = FlightIndex(a.csv); picked = [i for i in suggest(ix, a.count + len(a.exclude), seed=a.seed) if i not in a.exclude][:a.count]
        ss = [build(ix, i) for i in picked]
        label = (f'Historical DOT windows (seed {a.seed}): random eligible rows with >=4 arrivals within +/-90 min; '
                 f'demo rows {a.exclude} excluded. Limits from the organisers\' workbook; other operations synthetic (see provenance).')
    elif a.scenarios:
        ss = [Scenario.model_validate_json(f.read_text(encoding='utf-8')) for f in sorted(Path(a.scenarios).glob('*.json'))]
        label = 'User-provided scenarios; inspect provenance'
    else:
        ss = [smoke(i) for i in range(6)]; label = 'Synthetic stress profiles (6 distinct, workbook Medium-hub limits); NOT historical evaluation'
    if not ss: raise ValueError('No scenarios')
    rows = await evaluate(ss, a.claude)
    out = Path(a.output)
    out.write_text(json.dumps({'label': label, 'scenario_count': len(ss), 'rows': rows,
        'metric_units': {'holding': 'seconds', 'speed_absorb': 'seconds', 'ground_wait': 'seconds', 'departure_delay': 'seconds',
                         'crew_overtime': 'seconds', 'missed_connections': 'synthetic passenger count'}}, indent=2), encoding='utf-8')
    out.with_suffix('.md').write_text(summarise(rows, label), encoding='utf-8')
    print(out.with_suffix('.md').read_text())

if __name__ == '__main__': asyncio.run(main())
