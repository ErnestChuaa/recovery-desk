#pragma once
// Copy to config.h; choose UNO_WIFI_REV2 or ESP32 in node.ino.
const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASSWORD = "YOUR_PASSWORD";
const char* SERVER_HOST = "192.168.1.100";
const int SERVER_PORT = 8000;
const char* DEVICE_ID = "node1";
const char* FLIGHT_ID = "F1";
// Pins differ by board; ESP32 defaults below. UNO: button 2, LEDs 3,4,5.
#ifdef ESP32
const int BUTTON_PIN=27, RED_PIN=25, YELLOW_PIN=26, GREEN_PIN=33;
#else
const int BUTTON_PIN=2, RED_PIN=3, YELLOW_PIN=4, GREEN_PIN=5;
#endif
