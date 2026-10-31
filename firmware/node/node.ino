// Libraries: ArduinoHttpClient, ArduinoJson (v7).
// ESP32: Adafruit MPU6050 + Adafruit Unified Sensor + Adafruit BusIO.
// UNO WiFi Rev2: WiFiNINA + Arduino_LSM6DS3. Standard UNO R3 is unsupported here.
#include <ArduinoHttpClient.h>
#include <ArduinoJson.h>
#include <math.h>
#ifdef ESP32
#include <WiFi.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
Adafruit_MPU6050 imu;
#else
#include <WiFiNINA.h>
#include <Arduino_LSM6DS3.h>
#endif
#include "config.h"
WiFiClient wifi;
HttpClient http(wifi, SERVER_HOST, SERVER_PORT);
unsigned long seq=0,lastSend=0,lastGood=0,lastEdge=0,highestVersion=0;
bool lastRaw=false,debounced=false,imuOK=false;
char bootId[32];
void leds(int r,int y,int g) { digitalWrite(RED_PIN,r);digitalWrite(YELLOW_PIN,y);digitalWrite(GREEN_PIN,g); }
void setup() {
  Serial.begin(115200); pinMode(BUTTON_PIN,INPUT_PULLUP);
  pinMode(RED_PIN,OUTPUT);pinMode(YELLOW_PIN,OUTPUT);pinMode(GREEN_PIN,OUTPUT); leds(1,0,0);
#ifdef ESP32
  snprintf(bootId,sizeof bootId,"%lu",(unsigned long)esp_random()); imuOK=imu.begin();
#else
  imuOK=IMU.begin();
  snprintf(bootId,sizeof bootId,"%lu-%u",micros(),analogRead(A0));
#endif
  // Pressing button at startup adds entropy on boards without a random source.
  WiFi.begin(WIFI_SSID,WIFI_PASSWORD); http.setHttpResponseTimeout(1500);
}
void loop() {
  unsigned long now=millis(); bool raw=digitalRead(BUTTON_PIN)==LOW;
  if(raw!=lastRaw) { lastRaw=raw; lastEdge=now; }
  if(now-lastEdge>50) debounced=raw;
  if(now-lastGood>2500) leds(1,0,0); // Never keep a stale green indication.
  if(now-lastSend<500) return; lastSend=now;
  if(WiFi.status()!=WL_CONNECTED) { leds(1,0,0); WiFi.begin(WIFI_SSID,WIFI_PASSWORD); return; }
  float ax=0,ay=0,az=1;
  if(imuOK) {
#ifdef ESP32
    sensors_event_t a,g,temp; imu.getEvent(&a,&g,&temp);
    ax=a.acceleration.x/9.80665f;ay=a.acceleration.y/9.80665f;az=a.acceleration.z/9.80665f;
#else
    if(IMU.accelerationAvailable()) IMU.readAcceleration(ax,ay,az);
#endif
  }
  // Bench motion proxy only, not a calibrated aircraft turbulence measurement.
  float motion=fminf(20.0f,fabsf(sqrtf(ax*ax+ay*ay+az*az)-1.0f));
  JsonDocument doc; doc["device_id"]=DEVICE_ID;doc["flight_id"]=FLIGHT_ID;
  doc["boot_id"]=bootId;doc["seq"]=++seq;doc["uptime_ms"]=now;
  doc["emergency_flag"]=debounced;doc["motion_score"]=motion;
  String body;serializeJson(doc,body);
  http.post("/telemetry","application/json",body);
  int status=http.responseStatusCode();String response=http.responseBody();http.stop();
  if(status!=200) {leds(1,0,0);return;}
  JsonDocument reply;
  if(deserializeJson(reply,response)) {leds(1,0,0);return;}
  unsigned long version=reply["state_version"] | 0;
  if(version<highestVersion) {leds(1,0,0);return;}
  highestVersion=version;lastGood=millis();const char* led=reply["led"] | "red";
  leds(!strcmp(led,"red"),!strcmp(led,"yellow"),!strcmp(led,"green"));
}
