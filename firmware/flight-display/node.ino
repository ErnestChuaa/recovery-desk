#include <Wire.h>
#include <SPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

// ==========================================
// 1. COPY config.example.h TO config.h AND FILL IN YOUR VALUES
//    BEFORE UPLOADING. config.h is gitignored, so real Wi-Fi
//    credentials never enter the repository or git history.
// ==========================================
#include "config.h"

// Hardware Pins
#define TFT_CS   5
#define TFT_DC   17
#define TFT_RST  2
Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

Adafruit_MPU6050 mpu;
const int BUTTON_PIN = 0; // Onboard BOOT button

// BGR Color Fix for your 2.8" panel (Red & Blue swapped in hardware)
#define COLOR_BLACK  0x0000
#define COLOR_WHITE  0xFFFF
#define COLOR_RED    0x001F // Renders BRIGHT RED on your BGR screen
#define COLOR_GREEN  0x07E0 // Renders BRIGHT GREEN
#define COLOR_YELLOW 0x07FF // Renders BRIGHT YELLOW
#define COLOR_GRAY   0x8410

enum NodeState {
  STATE_WAITING,
  STATE_NEGOTIATING,
  STATE_AWAITING_APPROVAL,
  STATE_APPROVED,
  STATE_UNRESOLVED,
  STATE_OFFLINE
};

NodeState currentState = STATE_OFFLINE;

bool emergencyLatched = false;
bool motionEventLatched = false;
bool lastEmergencyDrawn = false;
bool lastMotionDrawn = false;

// Button edge-detection for clean toggling
bool lastButtonState = HIGH;
unsigned long lastDebounceTime = 0;

// Wi-Fi Polling Timer (every 1 second)
unsigned long lastPollTime = 0;
const unsigned long POLL_INTERVAL = 1000;

// Flight Assignment Data from Backend
String landTime = "T+08:30";
String gateAssigned = "G2";
String lastLandTime = "";
String lastGateAssigned = "";

// IMU Resting Baseline (automatically handles upside-down mounting)
float baseX = 0, baseY = 0, baseZ = 0;
bool needScreenRedraw = true;

// ==========================================
// DRAW 4-LINE SCREEN (ASTRA SPEC)
// ==========================================
void drawScreen() {
  tft.fillRect(0, 0, 240, 320, COLOR_BLACK);

  // Pick top status bar color (Virtual LED)
  uint16_t statusColor = COLOR_YELLOW;
  if (currentState == STATE_APPROVED) statusColor = COLOR_GREEN;
  else if (currentState == STATE_UNRESOLVED || currentState == STATE_OFFLINE) statusColor = COLOR_RED;

  // Draw Virtual LED bar at top
  tft.fillRect(10, 10, 220, 8, statusColor);
  tft.setTextSize(2);

  // --- LINE 1: Flight ID & Connection Status ---
  tft.setCursor(10, 30);
  tft.setTextColor(COLOR_WHITE);
  if (currentState == STATE_OFFLINE) {
    tft.print(FLIGHT_ID + "  ");
    tft.setTextColor(COLOR_RED);
    tft.println("OFFLINE");
  } else {
    tft.print(FLIGHT_ID + "   ");
    tft.setTextColor(COLOR_GREEN);
    tft.println("ONLINE");
  }
  tft.drawFastHLine(10, 52, 220, COLOR_GRAY);

  // --- OFFLINE VIEW ---
  if (currentState == STATE_OFFLINE) {
    tft.setCursor(10, 75);
    tft.setTextColor(COLOR_RED);
    tft.println("OFFLINE /");
    tft.setCursor(10, 100);
    tft.println("CHECK WIFI");
    return;
  }

  // --- APPROVED VIEW (When emergency is cleared) ---
  if (currentState == STATE_APPROVED && !emergencyLatched) {
    tft.setCursor(10, 75);
    tft.setTextColor(COLOR_GREEN);
    tft.println("PLAN APPROVED");

    tft.setCursor(10, 115);
    tft.setTextColor(COLOR_WHITE);
    tft.println("LAND: " + landTime);

    tft.setCursor(10, 150);
    tft.println("GATE: " + gateAssigned);
    return;
  }

  // --- ACTIVE EVENT / NEGOTIATION VIEW ---
  // Line 2: Event Status
  tft.setCursor(10, 75);
  if (emergencyLatched) {
    tft.setTextColor(COLOR_RED);
    tft.println("EVENT: EMERGENCY");
  } else if (motionEventLatched) {
    tft.setTextColor(COLOR_YELLOW);
    tft.println("EVENT: DISRUPTION");
  } else {
    tft.setTextColor(COLOR_GRAY);
    tft.println("EVENT: NONE");
  }

  // Line 3 & 4: State Messages
  tft.setCursor(10, 115);
  tft.setTextColor(statusColor);

  if (currentState == STATE_WAITING) {
    tft.println("WAITING FOR PLAN");
    tft.setCursor(10, 150);
    tft.setTextColor(COLOR_WHITE);
    tft.println("Standby...");
  } 
  else if (currentState == STATE_NEGOTIATING) {
    tft.println("STATUS:");
    tft.setCursor(10, 138);
    tft.println("NEGOTIATING");
    tft.setCursor(10, 175);
    tft.setTextColor(COLOR_WHITE);
    tft.setTextSize(1);
    tft.println("Awaiting decision...");
  } 
  else if (currentState == STATE_AWAITING_APPROVAL) {
    tft.println("AWAITING");
    tft.setCursor(10, 138);
    tft.println("APPROVAL");
    tft.setCursor(10, 175);
    tft.setTextColor(COLOR_WHITE);
    tft.setTextSize(1);
    tft.println("Operator review...");
  } 
  else if (currentState == STATE_APPROVED && emergencyLatched) {
    tft.setTextColor(COLOR_GREEN);
    tft.println("PLAN APPROVED");
    tft.setCursor(10, 145);
    tft.setTextColor(COLOR_WHITE);
    tft.println("LAND: " + landTime);
    tft.setCursor(10, 175);
    tft.println("GATE: " + gateAssigned);
  }
  else if (currentState == STATE_UNRESOLVED) {
    tft.println("UNRESOLVED /");
    tft.setCursor(10, 138);
    tft.println("REVIEW");
  }
}

// ==========================================
// SEND EVENT TO LAPTOP (POST /api/event)
// ==========================================
void sendEventToBackend(String eventType) {
  if (WiFi.status() != WL_CONNECTED) {
    currentState = STATE_OFFLINE;
    needScreenRedraw = true;
    return;
  }

  HTTPClient http;
  http.setTimeout(1500);
  http.begin(SERVER_URL + "/api/event");
  http.addHeader("Content-Type", "application/json");

  String payload = "{\"flight_id\":\"" + FLIGHT_ID + "\",\"event\":\"" + eventType + "\"}";
  int httpCode = http.POST(payload);
  Serial.printf("[POST /api/event] %s -> HTTP %d\n", eventType.c_str(), httpCode);
  http.end();
}

// ==========================================
// POLL LAPTOP STATUS (GET /api/status/FLIGHT_A)
// ==========================================
void pollBackendStatus() {
  if (WiFi.status() != WL_CONNECTED) {
    if (currentState != STATE_OFFLINE) {
      currentState = STATE_OFFLINE;
      needScreenRedraw = true;
    }
    return;
  }

  HTTPClient http;
  http.setTimeout(1500);
  http.begin(SERVER_URL + "/api/status/" + FLIGHT_PATH);
  int httpCode = http.GET();

  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<256> doc;
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      String statusStr = doc["status"] | "WAITING";
      landTime         = doc["landing_time"] | "T+08:30";
      gateAssigned     = doc["gate"] | "G2";

      if (doc.containsKey("emergency_active") && doc["emergency_active"] == false) {
        emergencyLatched = false;
        motionEventLatched = false;
      }

      NodeState newState = currentState;
      if (statusStr == "WAITING")                newState = STATE_WAITING;
      else if (statusStr == "NEGOTIATING")       newState = STATE_NEGOTIATING;
      else if (statusStr == "AWAITING_APPROVAL") newState = STATE_AWAITING_APPROVAL;
      else if (statusStr == "PLAN_APPROVED")     newState = STATE_APPROVED;
      else if (statusStr == "UNRESOLVED")        newState = STATE_UNRESOLVED;

      // Only redraw TFT if state or values changed (prevents screen flicker)
      if (newState != currentState || landTime != lastLandTime || gateAssigned != lastGateAssigned ||
          emergencyLatched != lastEmergencyDrawn || motionEventLatched != lastMotionDrawn) {
        currentState = newState;
        lastLandTime = landTime;
        lastGateAssigned = gateAssigned;
        lastEmergencyDrawn = emergencyLatched;
        lastMotionDrawn = motionEventLatched;
        needScreenRedraw = true;
      }
    }
  } else {
    Serial.printf("[GET status] Failed, HTTP code: %d\n", httpCode);
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  tft.begin();
  // Wipe all 4 orientations to clear any leftover ghost pixels in RAM
  for (int r = 0; r < 4; r++) {
    tft.setRotation(r);
    tft.fillScreen(COLOR_BLACK);
  }
  tft.setRotation(0); // Change to 2 if upside down from your viewing angle

  // Calibrate upside-down IMU resting baseline
  Wire.begin(21, 22);
  if (mpu.begin()) {
    delay(200);
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    baseX = a.acceleration.x;
    baseY = a.acceleration.y;
    baseZ = a.acceleration.z;
  }

  // Show connecting screen
  tft.setCursor(10, 30);
  tft.setTextColor(COLOR_YELLOW);
  tft.setTextSize(2);
  tft.println("CONNECTING WIFI...");

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(400);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected! IP: " + WiFi.localIP().toString());
    currentState = STATE_WAITING;
  } else {
    Serial.println("\nWiFi Timeout - Starting Offline");
    currentState = STATE_OFFLINE;
  }

  drawScreen();
}

void loop() {
  unsigned long now = millis();

  // --- 1. BOOT BUTTON TOGGLE ---
  bool reading = digitalRead(BUTTON_PIN);
  if (reading == LOW && lastButtonState == HIGH && (now - lastDebounceTime > 250)) {
    lastDebounceTime = now;

    if (!emergencyLatched) {
      // 1st Press: Immediate EMERGENCY display + notify laptop
      emergencyLatched = true;
      currentState = STATE_NEGOTIATING;
      drawScreen();
      sendEventToBackend("EMERGENCY");
    } else {
      // 2nd Press: Clear emergency back to WAITING
      emergencyLatched = false;
      motionEventLatched = false;
      currentState = STATE_WAITING;
      drawScreen();
      sendEventToBackend("CLEAR");
    }
    lastEmergencyDrawn = emergencyLatched;
    needScreenRedraw = false;
  }
  lastButtonState = reading;

  // --- 2. IMU MOTION TRIGGER ---
  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);
  float delta = abs(a.acceleration.x - baseX) + abs(a.acceleration.y - baseY) + abs(a.acceleration.z - baseZ);

  if (delta > 4.0 && !motionEventLatched && !emergencyLatched) {
    motionEventLatched = true;
    currentState = STATE_NEGOTIATING;
    drawScreen();
    sendEventToBackend("DISRUPTION");
    lastMotionDrawn = motionEventLatched;
    needScreenRedraw = false;
  } else if (delta <= 4.0) {
    baseX = 0.95 * baseX + 0.05 * a.acceleration.x;
    baseY = 0.95 * baseY + 0.05 * a.acceleration.y;
    baseZ = 0.95 * baseZ + 0.05 * a.acceleration.z;
  }

  // --- 3. POLL BACKEND STATUS EVERY 1 SECOND ---
  if (now - lastPollTime >= POLL_INTERVAL) {
    lastPollTime = now;
    pollBackendStatus();
  }

  if (needScreenRedraw) {
    drawScreen();
    needScreenRedraw = false;
  }

  delay(30);
}
