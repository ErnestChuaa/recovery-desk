"""Bench-test the hardware API without a physical node. No actual sensor readings."""
import argparse,time,uuid
import httpx
p=argparse.ArgumentParser();p.add_argument('--url',default='http://localhost:8000')
p.add_argument('--device',default='software-node');p.add_argument('--flight',default='F1')
p.add_argument('--emergency',action='store_true');p.add_argument('--motion',action='store_true')
p.add_argument('--duration',type=int,default=20);a=p.parse_args();boot=str(uuid.uuid4())
with httpx.Client(timeout=10) as c:
    for seq in range(1,a.duration*2+1):
        r=c.post(a.url+'/telemetry',json={'device_id':a.device,'flight_id':a.flight,'boot_id':boot,
            'seq':seq,'uptime_ms':seq*500,'emergency_flag':a.emergency and seq==3,
            'motion_score':1.2 if a.motion and 3<=seq<=6 else 0})
        r.raise_for_status();print(r.json());time.sleep(.5)
