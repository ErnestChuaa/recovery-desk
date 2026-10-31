"""Pretend to be the two ESP32 boards: poll status every second like the firmware, optionally press a button.

  python scripts/simulate_esp32.py --url http://192.168.43.10:8000 --press "FLIGHT A" EMERGENCY
  python scripts/simulate_esp32.py --press "FLIGHT B" CLEAR --seconds 5
Prints a line whenever a board's status, light, landing time or gate changes.
"""
import argparse, time
import httpx

p = argparse.ArgumentParser()
p.add_argument('--url', default='http://localhost:8000')
p.add_argument('--press', nargs=2, metavar=('BOARD', 'EVENT'), help='e.g. "FLIGHT A" EMERGENCY|DISRUPTION|CLEAR')
p.add_argument('--seconds', type=int, default=30)
a = p.parse_args()
last = {}
with httpx.Client(timeout=5) as c:
    if a.press:
        r = c.post(a.url + '/api/event', json={'flight_id': a.press[0], 'event': a.press[1]}); r.raise_for_status()
        print('pressed', a.press, '->', r.json())
    end = time.time() + a.seconds
    while time.time() < end:
        for board in ('FLIGHT_A', 'FLIGHT_B'):
            try: j = c.get(f'{a.url}/api/status/{board}').json()
            except httpx.HTTPError as e: print(board, 'unreachable:', e); continue
            key = (j['status'], j['led'], j['landing_time'], j['gate'])
            if last.get(board) != key:
                last[board] = key
                print(time.strftime('%H:%M:%S'), j['flight_id'], j['status'], j['led'], 'land', j['landing_time'], 'gate', j['gate'], 'emergency' if j['emergency_active'] else '')
        time.sleep(1)
