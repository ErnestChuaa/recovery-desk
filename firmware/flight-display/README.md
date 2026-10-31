# Flight Display Node (Flight A board sketch)

Self-contained copy of the ESP32 flight-display node sketch, kept separate from `firmware/node/` so both versions stay intact.

> **Contract note:** this sketch implements its own hardware contract (`FLIGHT_ID "FLIGHT A"`, path `FLIGHT_A`, JSON `{flight_id, event}`) and has **not** been verified against the backend in this repo. The root `HARDWARE.md` / `INTEGRATION.md` describe the other firmware's contract (`firmware/node/`). Reconcile the two before demo day.

## Hardware (Flight A)

* ESP32 development board
* 2.8" ILI9341 TFT display (BGR color configuration - red/blue swapped in hardware, handled in code)
* MPU-6050 IMU (accelerometer/gyroscope)
* Onboard BOOT button
* Network: 2.4 GHz Wi-Fi mobile hotspot to the FastAPI laptop backend

### Pin mapping

| Peripheral | Pin |
|---|---|
| TFT `CS` | GPIO 5 |
| TFT `DC` | GPIO 17 |
| TFT `RST` | GPIO 2 |
| MPU-6050 `SDA` | GPIO 21 |
| MPU-6050 `SCL` | GPIO 22 |
| BOOT button | GPIO 0 |

## Firmware setup

1. Arduino IDE with the ESP32 board package.
2. Libraries: Adafruit ILI9341, Adafruit GFX, Adafruit MPU6050 (+ Adafruit Unified Sensor), ArduinoJson.
3. Copy `config.example.h` to `config.h` and fill in your values (`config.h` is gitignored - the sketch will not compile without it, on purpose):
   * `WIFI_SSID` / `WIFI_PASSWORD` - 2.4 GHz hotspot credentials
   * `SERVER_URL` - backend laptop address, e.g. `http://<LAPTOP_IP>:8000`
   * `FLIGHT_ID` / `FLIGHT_PATH` - `"FLIGHT A"` / `"FLIGHT_A"` for this board
4. Open `node.ino`, select the ESP32 board, upload.

## API contract (as implemented by `node.ino`)

### Trigger event
* `POST http://<SERVER_IP>:8000/api/event`, `Content-Type: application/json`
* Payload: `{"flight_id": "FLIGHT A", "event": "EMERGENCY"}`
* Sent events: `"EMERGENCY"` (BOOT button 1st press), `"CLEAR"` (2nd press), `"DISRUPTION"` (IMU motion delta over threshold)

### Status polling (once per second)
* `GET http://<SERVER_IP>:8000/api/status/FLIGHT_A`
* Expected response:
  ```json
  {
    "status": "PLAN_APPROVED",
    "landing_time": "T+08:30",
    "gate": "G2",
    "emergency_active": false
  }
  ```
* Handled states: `"WAITING"`, `"NEGOTIATING"`, `"AWAITING_APPROVAL"`, `"PLAN_APPROVED"`, `"UNRESOLVED"`; anything unreachable shows `OFFLINE / CHECK WIFI`.

## Verify

* Serial monitor at 115200 baud prints `WiFi Connected! IP: ...`.
* TFT shows `FLIGHT A ONLINE` with the virtual LED bar; button press latches `EVENT: EMERGENCY` and POSTs to the backend.
