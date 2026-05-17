#ifndef WEATHER_SECRETS_H
#define WEATHER_SECRETS_H

#define WIFI_SSID "Ab_mb"
#define WIFI_PASSWORD "1234567890"

// Optionnel: forcer la geolocalisation si les APIs IP sont bloquees
// 0 = desactive (auto IP), 1 = utilise les coordonnees ci-dessous
#define WEATHER_HAS_FIXED_GEO 0
#define WEATHER_FIXED_LAT 0.0f
#define WEATHER_FIXED_LON 0.0f

// --- Architecture legere (recommande) : dashboard + CSV sur le PC (pc_irrigation_bridge) ---
// Desactive le serveur HTTP sur l'ESP32 (economie RAM / flash).
#ifndef ENABLE_WEB_DASHBOARD
#define ENABLE_WEB_DASHBOARD 0
#endif
#ifndef DASHBOARD_PORT
#define DASHBOARD_PORT 80
#endif

// Journal CSV sur LittleFS ESP32 (desactive par defaut : enregistrement via MQTT sur le PC).
#ifndef ENABLE_DATASET_LOG
#define ENABLE_DATASET_LOG 0
#endif
#ifndef DATASET_LOG_INTERVAL_MS
#define DATASET_LOG_INTERVAL_MS (30000UL)
#endif
#ifndef DATASET_NTP_SERVER
#define DATASET_NTP_SERVER "pool.ntp.org"
#endif

// MQTT : telemetrie + commandes manuelles (broker Mosquitto sur le PC ou sur le reseau local).
#ifndef ENABLE_MQTT
#define ENABLE_MQTT 1
#endif
// IP IPv4 de la carte "Wi-Fi" (ipconfig) — meme sous-reseau que l'ESP32
#ifndef MQTT_BROKER_HOST
#define MQTT_BROKER_HOST "10.222.28.36"
#endif
#ifndef MQTT_BROKER_PORT
#define MQTT_BROKER_PORT 1883
#endif
#ifndef MQTT_CLIENT_ID
#define MQTT_CLIENT_ID "esp32_irrigation"
#endif
#ifndef MQTT_TOPIC_TELEMETRY
#define MQTT_TOPIC_TELEMETRY "irrigation/station/telemetry"
#endif
#ifndef MQTT_TOPIC_COMMAND
#define MQTT_TOPIC_COMMAND "irrigation/command/manual"
#endif
#ifndef MQTT_USE_AUTH
#define MQTT_USE_AUTH 0
#endif
#ifndef MQTT_USER
#define MQTT_USER ""
#endif
#ifndef MQTT_PASSWORD
#define MQTT_PASSWORD ""
#endif

// Si LittleFS echoue avec le label par defaut, le firmware essaie aussi ce label (souvent "spiffs" avec Partition Scheme "with spiffs").
// #define DATASET_LITTLEFS_PARTITION_ALT "littlefs"

#endif
