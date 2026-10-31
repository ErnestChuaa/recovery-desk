import asyncio, json, os, socket, sqlite3, time, uuid
from typing import Literal
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from .models import Scenario, Telemetry, Disruption, NegotiateRequest, Window, Plan, demo
from .engine import validate, fcfs
from .agents import negotiate

class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')
        self.lock=asyncio.Lock(); self.tasks=set(); self.devices={}; self.approved=None
        self.latest_run=None; self.bindings={}   # ESP32 boards -> scenario flight IDs (in memory)
        saved=self.get('state')
        try: self.state=Scenario.model_validate(saved) if saved else demo()
        except Exception: self.state=demo()      # saved state from an older version: start from the demo
    def get(self,key):
        row=self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,json.dumps(value))); self.db.commit()
    def event(self,payload):
        payload={'server_time':time.time(),**payload}
        cur=self.db.execute('INSERT INTO events(payload) VALUES (?)',(json.dumps(payload),)); self.db.commit()
        return cur.lastrowid
    def changed(self,reason):
        self.state.version+=1; self.approved=None
        self.put('state',self.state.model_dump()); self.event({'type':'state_changed','version':self.state.version,'reason':reason})

def lan_ips():
    """IPv4 addresses other devices on the hotspot can reach (no packets are sent)."""
    ips=[]
    try:
        u=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); u.connect(('10.255.255.255',1)); ips.append(u.getsockname()[0]); u.close()
    except OSError: pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET): ips.append(info[4][0])
    except OSError: pass
    return [ip for ip in dict.fromkeys(ips) if not ip.startswith('127.')]

BOARDS=('FLIGHT A','FLIGHT B')

def norm_board(raw):
    """'FLIGHT_A', 'flight a', 'FLIGHT%20A', 'FlightA' and 'A' all mean 'FLIGHT A'."""
    x=' '.join(str(raw).strip().upper().replace('_',' ').replace('-',' ').split())
    compact=x.replace(' ','')
    if compact in ('A','B'): compact='FLIGHT'+compact
    if compact in ('FLIGHTA','FLIGHTB'): return 'FLIGHT '+compact[-1]
    raise HTTPException(404,f'unknown board {raw!r}; use FLIGHT_A or FLIGHT_B')

class HardwareEvent(BaseModel):
    flight_id: str
    event: Literal['EMERGENCY','DISRUPTION','CLEAR']

def create_app(db_path=None):
    @asynccontextmanager
    async def life(app):
        app.state.store=Store(db_path or os.getenv('RECOVERY_DB','recovery.sqlite3'))
        # Jobs cannot resume across process restart. Never restore approval to hardware.
        st=app.state.store
        for key,value in st.db.execute("SELECT key,value FROM kv WHERE key LIKE 'run:%'").fetchall():
            run=json.loads(value)
            if run.get('status')=='RUNNING': run['status']='INTERRUPTED'; st.put(key,run)
        port=os.getenv('PORT','8000')
        for ip in lan_ips():
            print(f'[Recovery Desk] ESP32 boards: set server to http://{ip}:{port}  (status: /api/status/FLIGHT_A)',flush=True)
        yield
        for task in st.tasks: task.cancel()
        await asyncio.gather(*st.tasks,return_exceptions=True); st.db.close()
    app=FastAPI(title='Recovery Desk',version='1.0.0',lifespan=life)
    app.add_middleware(CORSMiddleware,allow_origins=os.getenv('DASHBOARD_ORIGINS','http://localhost:3000,http://localhost:8501').split(','),allow_methods=['*'],allow_headers=['*'])
    def store(): return app.state.store
    async def launch(mode):
        st=store()
        async with st.lock:
            snapshot=st.state.model_copy(deep=True); rid=str(uuid.uuid4())
            run={'id':rid,'status':'RUNNING','state_version':snapshot.version,'mode':mode,'scenario_snapshot':snapshot.model_dump()}
            st.put('run:'+rid,run); st.event({'type':'run_started','run_id':rid,'mode':mode,'state_version':snapshot.version})
            st.latest_run=run
        async def work():
            async def emit(e): st.event({'run_id':rid,**e})
            try:
                result=await negotiate(snapshot,mode,emit)
                async with st.lock:
                    run.update(result)
                    run['status']='REVIEW_REQUIRED' if st.state.version==snapshot.version else 'STALE'
                    st.put('run:'+rid,run); st.event({'type':'run_completed','run_id':rid,'status':run['status'],'outcome':run['outcome']})
                    if run['status']!='STALE' and run['plan']['unresolved']:
                        st.put('hardware_outcome',{'version':snapshot.version})
            except Exception as e:
                run.update(status='ERROR',error=type(e).__name__); st.put('run:'+rid,run)
                st.event({'type':'run_completed','run_id':rid,'status':'ERROR','outcome':None})
        task=asyncio.create_task(work()); st.tasks.add(task); task.add_done_callback(st.tasks.discard)
        return run
    @app.get('/health')
    async def health(): return {'ok':True,'simulation_only':True}

    # ------------------------------------------------------------------ ESP32 board contract
    # Boards poll GET /api/status/FLIGHT_A|FLIGHT_B every second and POST /api/event on a button
    # press or IMU shake. Status is derived from the backend's real state on every poll, so the
    # boards and the dashboard can never disagree. Only five status strings are ever returned.
    def bindings(st):
        ids=[f.id for f in st.state.flights]
        if not st.bindings or any(v not in ids for v in st.bindings.values()):
            others=[f.id for f in sorted(st.state.flights,key=lambda f:(f.ready,f.id)) if f.id!=st.state.focal_id]
            st.bindings={'FLIGHT A':st.state.focal_id,'FLIGHT B':others[0] if others else st.state.focal_id}
        return st.bindings
    def hw_time(st,t):
        origin=st.state.provenance.get('time_origin_local_minutes')
        if os.getenv('HARDWARE_TIME_FORMAT')=='clock' and origin is not None:
            m=int(origin+t//60); return f'{(m//60)%24:02d}:{m%60:02d}'
        m=t//60; return f'T+{m//60:02d}:{m%60:02d}'
    def board_status(st,board):
        fid=bindings(st)[board]; f=next(x for x in st.state.flights if x.id==fid)
        engaged=f.emergency or f.motion_event
        out={'flight_id':board,'status':'WAITING','led':'YELLOW','landing_time':'--','gate':'--','emergency_active':False}
        def slot(plan):
            a=next((a for a in plan['assignments'] if a['flight_id']==fid),None)
            return {'landing_time':hw_time(st,a['landing']),'gate':a['gate']} if a else {}
        ap=st.approved if st.approved and st.approved['state_version']==st.state.version else None
        if ap and slot(ap['plan']):
            return {**out,'status':'PLAN_APPROVED','led':'GREEN',**slot(ap['plan']),'emergency_active':False}
        run=st.latest_run if st.latest_run and st.latest_run.get('state_version')==st.state.version else None
        if run and run['status']!='RUNNING':
            plan=run.get('plan')
            if plan:
                unresolved=plan['unresolved']
                if fid in unresolved or (engaged and unresolved):
                    return {**out,'status':'UNRESOLVED','led':'RED','emergency_active':True}
                if engaged and run['status']=='REVIEW_REQUIRED':
                    return {**out,'status':'AWAITING_APPROVAL','led':'YELLOW',**slot(plan),'emergency_active':True}
            elif engaged:   # ERROR or INTERRUPTED: no plan to review
                return {**out,'status':'UNRESOLVED','led':'RED','emergency_active':True}
        if engaged:
            return {**out,'status':'NEGOTIATING','led':'YELLOW','emergency_active':True}
        return out
    @app.get('/api/status/{flight_path}')
    async def api_status(flight_path:str):
        st=store(); return board_status(st,norm_board(flight_path))
    @app.post('/api/event')
    async def api_event(e:HardwareEvent):
        st=store(); board=norm_board(e.flight_id); start=False
        async with st.lock:
            fid=bindings(st)[board]; f=next(x for x in st.state.flights if x.id==fid)
            before=(f.emergency,f.motion_event)
            if e.event=='EMERGENCY': f.emergency=True
            elif e.event=='DISRUPTION':
                if not f.motion_event: f.note=(f.note+' Board reported motion/turbulence.').strip()[:1200]
                f.motion_event=True
            else: f.emergency=False; f.motion_event=False
            changed=(f.emergency,f.motion_event)!=before
            st.event({'type':'hardware_api_event','board':board,'flight_id':fid,'event':e.event,'changed':changed})
            if changed:
                # Flip to NEGOTIATING before the agents start: the version bump makes any old plan
                # stale, so the very next 1 s poll already reads NEGOTIATING (no flicker).
                st.changed('hardware_event')
                start=any(x.emergency or x.motion_event for x in st.state.flights if x.id in bindings(st).values())
        if start: await launch(os.getenv('HARDWARE_AGENT_MODE','fixed'))
        return board_status(st,board)
    class Bindings(BaseModel):
        model_config={'extra':'forbid'}
        flight_a: str | None = None
        flight_b: str | None = None
    @app.get('/api/hardware')
    async def api_hardware():
        st=store(); port=os.getenv('PORT','8000')
        return {'bindings':bindings(st),'boards':[board_status(st,b) for b in BOARDS],
                'urls':[f'http://{ip}:{port}' for ip in lan_ips()]}
    @app.put('/api/hardware/bindings')
    async def api_bind(b:Bindings):
        st=store(); ids=[f.id for f in st.state.flights]
        async with st.lock:
            cur=dict(bindings(st))
            for board,val in (('FLIGHT A',b.flight_a),('FLIGHT B',b.flight_b)):
                if val is None: continue
                if val not in ids: raise HTTPException(422,f'{val} is not a flight in this scenario')
                cur[board]=val
            st.bindings=cur; st.event({'type':'hardware_bindings','bindings':cur})
        return await api_hardware()
    @app.get('/state')
    async def state(): return store().state
    @app.put('/scenario')
    async def scenario(s:Scenario):
        st=store()
        async with st.lock:
            s.version=st.state.version; st.state=s; st.changed('scenario_loaded'); st.devices.clear(); st.bindings={}
        return st.state
    class CsvRow(BaseModel):
        row: int
    index_cache={}
    @app.post('/scenario/from-csv')
    async def from_csv(body:CsvRow):
        path=os.getenv('FLIGHTS_CSV')
        if not path or not Path(path).exists(): raise HTTPException(422,'Set FLIGHTS_CSV to the DOT flights.csv path')
        from .data import FlightIndex, build
        if path not in index_cache: index_cache[path]=await asyncio.to_thread(FlightIndex,path)
        try: s=await asyncio.to_thread(build,index_cache[path],body.row)
        except ValueError as e: raise HTTPException(422,str(e))
        return await scenario(s)
    @app.get('/scenario/suggest')
    async def suggest_rows(n:int=10):
        path=os.getenv('FLIGHTS_CSV')
        if not path or not Path(path).exists(): raise HTTPException(422,'Set FLIGHTS_CSV to the DOT flights.csv path')
        from .data import FlightIndex, suggest
        if path not in index_cache: index_cache[path]=await asyncio.to_thread(FlightIndex,path)
        ix=index_cache[path]
        return [{'row':i,**{k:ix.rows[i][k] for k in ('AIRLINE','FLIGHT_NUMBER','DESTINATION_AIRPORT','WEATHER_DELAY','LATE_AIRCRAFT_DELAY')}} for i in suggest(ix,min(n,50))]
    @app.get('/ui/fonts/{name}')
    async def ui_font(name:str):
        base=(Path(__file__).resolve().parent.parent/'dashboard'/'fonts').resolve()
        path=(base/name).resolve()
        if path.parent!=base or path.suffix!='.woff2' or not path.exists(): raise HTTPException(404,'unknown font')
        return FileResponse(path,media_type='font/woff2',headers={'Cache-Control':'max-age=86400'})
    @app.get('/ui')
    async def ui(): return FileResponse(Path(__file__).resolve().parent.parent/'dashboard'/'index.html')
    @app.get('/scenario/demo')
    async def sample(seed:int=0): return demo(seed)
    @app.get('/baseline')
    async def baseline():
        snapshot=store().state.model_copy(deep=True)
        return {'state_version':snapshot.version,'plan':await asyncio.to_thread(fcfs,snapshot)}
    @app.post('/disruptions')
    async def disruption(d:Disruption):
        st=store()
        async with st.lock:
            s=st.state.model_copy(deep=True)
            if d.kind in ('runway_closure','taxi_closure','gate_closure'):
                if d.end<=d.start: raise HTTPException(422,'end must exceed start')
                w=Window(start=d.start,end=d.end)
                if d.kind=='runway_closure': s.runway_closed.append(w)
                elif d.kind=='taxi_closure': s.taxi_closed.append(w)
                else:
                    if d.gate not in s.gates: raise HTTPException(404,'unknown gate')
                    s.gates[d.gate].append(w)
            else:
                f=next((f for f in s.flights if f.id==d.flight_id),None)
                if not f: raise HTTPException(404,'unknown flight')
                if d.kind=='latest':
                    if d.end<f.ready or d.end>s.horizon: raise HTTPException(422,'latest outside scenario bounds')
                    f.latest=d.end
                elif d.kind=='delay':
                    # start = extra seconds of en-route delay; ETA and fuel deadline move together
                    if f.latest+d.start>s.horizon: raise HTTPException(422,'delay exceeds scenario horizon')
                    f.ready+=d.start; f.latest+=d.start
                elif d.kind in ('turbulence','reset_turbulence'): f.motion_event=d.kind=='turbulence'
                else: f.emergency=d.kind=='emergency'
            st.state=s; st.changed(d.kind)
        return st.state
    @app.post('/runs',status_code=202)
    async def start(body:NegotiateRequest):
        if body.mode=='claude' and not (os.getenv('ANTHROPIC_API_KEY') and os.getenv('CLAUDE_MODEL')):
            raise HTTPException(422,'Set ANTHROPIC_API_KEY and CLAUDE_MODEL')
        return await launch(body.mode)
    @app.get('/constraints')
    async def constraints_view():
        """Operational limits from the organisers' workbook, the rules of play and every stated change."""
        from . import constraints as C
        c=C.load(); st=store(); code=st.state.provenance.get('airport')
        return {'source':c['source'],'loaded_from_workbook':c['loaded_from_workbook'],'load_error':c.get('load_error'),
                'airport':C.airport(code) if code else st.state.provenance.get('constraints'),
                'scenario':{'active_runways':st.state.active_runways,'max_ops_per_hour':st.state.max_ops_per_hour,
                            'gates':list(st.state.gates),'gate_change_notice_s':st.state.gate_change_notice,'decision_time':st.state.decision_time},
                'wake_table':c['wake_table'],'approach_speed_kt':c['approach_speed_kt'],'fuel_and_crew':c['fuel'],
                'rules_of_play':[{'topic':t,'detail':d} for t,d in c['rules_text']],'max_turns':c['rules']['max_turns'],
                'stated_changes':c['stated_changes'],'airports_in_workbook':len(c['airports'])}
    def log_time(r,t):
        origin=r['scenario_snapshot'].get('provenance',{}).get('time_origin_local_minutes')
        if origin is None: m=t//60; return f'T+{m//60:02d}:{m%60:02d}'
        m=int(origin+t//60); return f'{(m//60)%24:02d}:{m%60:02d} local'
    @app.get('/runs/{rid}/log')
    async def consensus_log(rid:str):
        """Consensus log for one run: every turn's structured messages, the decision and why."""
        r=await run(rid)
        if 'trace' not in r: raise HTTPException(409,'run not complete')
        turns={}
        for e in r['trace']:
            if e.get('type')=='agent_message':
                turns.setdefault(e['round'],[]).append({k:e[k] for k in ('sender','message_type','accepted_plan_ids','preferred_plan_ids','rejections','conceded','concessions','summary','summary_source')})
        return {'run_id':rid,'scenario_id':r['scenario_snapshot']['id'],'state_version':r['state_version'],'mode':r['mode'],
                'status':r['status'],'turn_limit':r.get('turn_limit'),'turns_used':r['rounds'],
                'turns':[{'turn':k,'messages':v} for k,v in sorted(turns.items())],
                'coordinator':[e for e in r['trace'] if e.get('type') in ('menu','coordinator','agent_error')],
                'decision':{'outcome':r['outcome'],'decision':r['decision'],'plan_id':r['plan']['id'],'fallback_reason':r.get('fallback_reason'),
                            'why':r.get('fallback_explanation'),'concessions':r.get('concessions'),'unresolved':r['plan']['unresolved']},
                'output':[{'flight':d['flight_id'],'revised_arrival_s':d['revised_eta'],'revised_arrival':log_time(r,d['revised_eta']),
                           'gate':d['gate'],'gate_changed':d['gate_changed'],'flight_plan_change':d.get('flight_plan_adjustment'),
                           'airborne_hold_min':round(d['airborne_hold']/60,1),'holding_fuel_kg':d.get('holding_fuel_kg')} for d in r['plan']['details']],
                'checks_passed':sum(c['passed'] for c in r['checks']),'checks_total':len(r['checks'])}
    @app.get('/runs/latest')
    async def latest_run():
        """Most recent run for the current scenario version (or null), so a dashboard opened later
        can pick up a negotiation that a board or another session started."""
        st=store(); r=st.latest_run
        if not r or r.get('state_version')!=st.state.version: return None
        return await run(r['id'])
    @app.get('/runs/{rid}')
    async def run(rid:str):
        r=store().get('run:'+rid)
        if not r: raise HTTPException(404,'unknown run')
        if r['state_version']!=store().state.version: r['status']='STALE'
        return r
    @app.post('/runs/{rid}/approve')
    async def approve(rid:str):
        st=store()
        async with st.lock:
            r=st.get('run:'+rid)
            if not r: raise HTTPException(404,'unknown run')
            if r['status']!='REVIEW_REQUIRED' or r['state_version']!=st.state.version: raise HTTPException(409,'not current/reviewable')
            p=Plan.model_validate(r['plan'])
            if p.unresolved or not all(x['passed'] for x in validate(st.state,p)): raise HTTPException(409,'incomplete or invalid plan')
            st.approved={'run_id':rid,'state_version':st.state.version,'plan':p.model_dump()}
            r['status']='APPROVED'; st.put('run:'+rid,r); st.event({'type':'approved','run_id':rid,'state_version':st.state.version})
            return st.approved
    @app.get('/runs/{rid}/timeline')
    async def timeline(rid:str):
        r=await run(rid)
        if 'plan' not in r: raise HTTPException(409,'run not complete')
        events=[]
        for a in r['plan']['assignments']:
            for phase in ('landing','gate_in','gate_out','departure'):
                events.append({'time':a[phase],'phase':phase,'flight_id':a['flight_id'],'gate':a['gate'],'crew':a['crew']})
        return {'state_version':r['state_version'],'status':r['status'],'events':sorted(events,key=lambda e:(e['time'],e['flight_id'],e['phase']))}
    def device_status(device_id):
        st=store(); d=st.devices.get(device_id)
        if not d: return {'device_id':device_id,'led':'red','status':'UNKNOWN_DEVICE','state_version':st.state.version}
        if time.monotonic()-d['seen']>3: status='OFFLINE'; led='red'; assignment=None
        else:
            approved=st.approved
            assignment=next((x for x in approved['plan']['assignments'] if x['flight_id']==d['flight_id']),None) if approved and approved['state_version']==st.state.version else None
            unresolved=st.get('hardware_outcome')
            status='APPROVED_SIMULATION_ASSIGNMENT' if assignment else 'REPLAN_OR_REVIEW_PENDING'
            led='green' if assignment else 'yellow'
            if not assignment and unresolved and unresolved['version']==st.state.version: status='REVIEW_REQUIRED'; led='red'
        return {'device_id':device_id,'flight_id':d['flight_id'],'state_version':st.state.version,'status':status,'led':led,'assignment':assignment,'command_ttl_ms':2500}
    @app.get('/devices/{device_id}/status')
    async def status(device_id:str): return device_status(device_id)
    @app.get('/devices')
    async def devices(): return [device_status(k) for k in store().devices]
    @app.post('/telemetry')
    async def telemetry(t:Telemetry):
        st=store(); changed=False
        async with st.lock:
            f=next((f for f in st.state.flights if f.id==t.flight_id),None)
            if not f: raise HTTPException(404,'flight not in scenario')
            old=st.devices.get(t.device_id)
            if old and old['flight_id']!=t.flight_id: raise HTTPException(409,'device already bound to another flight')
            if old and old['boot_id']==t.boot_id and t.seq<=old['seq']:
                return {'accepted':False,'reason':'duplicate_or_out_of_order',**device_status(t.device_id)}
            motion_count=(old.get('motion_count',0) if old else 0)+1 if t.motion_score>=0.8 else 0
            st.devices[t.device_id]={**t.model_dump(),'seen':time.monotonic(),'motion_count':motion_count}
            if t.emergency_flag and not f.emergency: f.emergency=True; changed=True
            # Motion is a latched simulated discomfort preference, never measured aviation turbulence.
            if motion_count>=3 and not f.motion_event:
                f.motion_event=True; f.note+=' Simulated motion event: prefer less holding.'; changed=True
            if changed: st.changed('hardware_event')
            st.event({'type':'telemetry','device_id':t.device_id,'flight_id':t.flight_id,'seq':t.seq,'motion_score':t.motion_score,'emergency_flag':t.emergency_flag})
        if changed:
            r=await launch(os.getenv('HARDWARE_AGENT_MODE','fixed'))
            return {'accepted':True,'run_id':r['id'],**device_status(t.device_id)}
        return {'accepted':True,**device_status(t.device_id)}
    @app.get('/events')
    async def events(after:int=0):
        # event_id is the stream cursor. It is set last so a payload field called 'id' (a run ID) can never replace it.
        return [{**json.loads(row[1]),'event_id':row[0]} for row in store().db.execute('SELECT id,payload FROM events WHERE id>? ORDER BY id LIMIT 500',(after,))]
    @app.get('/events/latest')
    async def latest_event():
        row=store().db.execute('SELECT MAX(id) FROM events').fetchone()
        return {'id':row[0] or 0}
    @app.get('/results/{name}')
    async def results(name:str):
        allowed={'historical-30','synthetic-smoke'}
        if name not in allowed: raise HTTPException(404,'unknown result set')
        path=Path(__file__).resolve().parent.parent/'results'/f'{name}.json'
        if not path.exists(): raise HTTPException(404,'result file missing; run the benchmark')
        return FileResponse(path,media_type='application/json')
    @app.get('/events/stream')
    async def stream(request:Request,after:int=0):
        async def generate():
            cursor=after
            while not await request.is_disconnected():
                batch=await events(cursor)
                for e in batch:
                    cursor=e['event_id']; yield f'id: {cursor}\ndata: {json.dumps(e)}\n\n'
                if not batch: yield ': heartbeat\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(generate(),media_type='text/event-stream')
    return app

app=create_app()
