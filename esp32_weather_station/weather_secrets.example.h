#ifndef WEATHER_SECRETS_H
#define WEATHER_SECRETS_H

// Copiez ce fichier vers weather_secrets.h (non versionne) et remplissez vos valeurs.

#define WIFI_SSID "TON_WIFI"
#define WIFI_PASSWORD "TON_MOT_DE_PASSE_WIFI"

#define WEATHER_HAS_FIXED_GEO 0
#define WEATHER_FIXED_LAT 0.0f
#define WEATHER_FIXED_LON 0.0f

#ifndef ENABLE_WEB_DASHBOARD
#define ENABLE_WEB_DASHBOARD 0
#endif
#ifndef DASHBOARD_PORT
#define DASHBOARD_PORT 80
#endif

#ifndef ENABLE_DATASET_LOG
#define ENABLE_DATASET_LOG 0
#endif
#ifndef DATASET_LOG_INTERVAL_MS
#define DATASET_LOG_INTERVAL_MS (30000UL)
#endif
#ifndef DATASET_NTP_SERVER
#define DATASET_NTP_SERVER "pool.ntp.org"
#endif

#ifndef ENABLE_MQTT
#define ENABLE_MQTT 1
#endif

// HiveMQ Cloud (TLS port 8883)
#ifndef MQTT_BROKER_HOST
#define MQTT_BROKER_HOST "d5d4693246d54f46a43cefa118dea176.s1.eu.hivemq.cloud"
#endif
#ifndef MQTT_BROKER_PORT
#define MQTT_BROKER_PORT 8883
#endif
#ifndef MQTT_USE_TLS
#define MQTT_USE_TLS 1
#endif
#ifndef MQTT_CLIENT_ID
#define MQTT_CLIENT_ID "esp32_irrigation_station01"
#endif
#ifndef MQTT_TOPIC_TELEMETRY
#define MQTT_TOPIC_TELEMETRY "irrigation/station01/telemetry"
#endif
#ifndef MQTT_TOPIC_COMMAND
#define MQTT_TOPIC_COMMAND "irrigation/station01/command/manual"
#endif
#ifndef MQTT_TOPIC_RELAY
#define MQTT_TOPIC_RELAY "irrigation/station01/command/relay"
#endif
#ifndef MQTT_USE_AUTH
#define MQTT_USE_AUTH 1
#endif
#ifndef MQTT_USER
#define MQTT_USER "irrigation_station01"
#endif
#ifndef MQTT_PASSWORD
#define MQTT_PASSWORD "TON_MOT_DE_PASSE_HIVEMQ"
#endif

#endif
