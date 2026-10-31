#ifndef CONFIG_H
#define CONFIG_H

// --- NETWORK CONFIGURATION ---
const char* WIFI_SSID     = "YOUR_HOTSPOT_NAME";     // 2.4GHz Mobile Hotspot
const char* WIFI_PASSWORD = "YOUR_HOTSPOT_PASSWORD";
const String SERVER_URL   = "http://119.234.108.48:8000"; // Backend Laptop IP + Port

// --- NODE IDENTIFICATION ---
const String FLIGHT_ID    = "FLIGHT A"; // Change to "FLIGHT B" for the second board
const String FLIGHT_PATH  = "FLIGHT_A"; // URL path segment (/api/status/FLIGHT_A)

#endif
