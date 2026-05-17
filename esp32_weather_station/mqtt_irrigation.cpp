#include "weather_secrets.h"
#include "mqtt_irrigation.h"

#if ENABLE_MQTT

#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>
#if MQTT_USE_TLS
#include <WiFiClientSecure.h>
#endif

extern int g_crop_idx;
extern int g_soil_idx;
extern int g_crop_age_days;
extern bool g_manual_ready;

#if MQTT_USE_TLS
static WiFiClientSecure s_wifi_mqtt;
#else
static WiFiClient s_wifi_mqtt;
#endif
static PubSubClient s_mqtt(s_wifi_mqtt);
static char s_payload[1400];
static char s_rx_cmd[512];
static uint32_t s_last_reconnect_ms;

static const char* kCropNames[] = {"Maize", "Rice", "Tomato", "Wheat"};
static const char* kSoilNames[] = {"Clayey", "Loamy", "Sandy", "Silty"};

static void mqtt_on_message(char* topic, byte* payload, unsigned int length) {
  (void)topic;
  if (length >= sizeof(s_rx_cmd)) length = sizeof(s_rx_cmd) - 1;
  memcpy(s_rx_cmd, payload, length);
  s_rx_cmd[length] = '\0';

  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, s_rx_cmd);
  if (err) {
    Serial.print(F("[MQTT] JSON commande invalide: "));
    Serial.println(err.c_str());
    return;
  }
  int age = doc["crop_age_days"] | 0;
  int ci = doc["crop_idx"] | 0;
  int si = doc["soil_idx"] | 0;
  if (age < 1) age = 1;
  if (age > 120) age = 120;
  if (ci < 0) ci = 0;
  if (ci > 3) ci = 3;
  if (si < 0) si = 0;
  if (si > 3) si = 3;
  g_crop_age_days = age;
  g_crop_idx = ci;
  g_soil_idx = si;
  g_manual_ready = true;
  Serial.println(F("[MQTT] Saisie manuelle appliquee (topic commande)."));
  Serial.print(F("  age_jours="));
  Serial.println(g_crop_age_days);
  Serial.print(F("  culture="));
  Serial.print(kCropNames[g_crop_idx]);
  Serial.print(F(" (idx="));
  Serial.print(g_crop_idx);
  Serial.println(F(")"));
  Serial.print(F("  sol="));
  Serial.print(kSoilNames[g_soil_idx]);
  Serial.print(F(" (idx="));
  Serial.print(g_soil_idx);
  Serial.println(F(")"));
  irrigation_request_sensor_cycle();
}

static void mqtt_print_rc_hint(int rc) {
  Serial.print(F("[MQTT] Echec, rc="));
  Serial.println(rc);
  if (rc == -2) {
    Serial.println(F("  -> CONN_FAILED: host/port/TLS (HiveMQ=8883+TLS, pas 1883 local)"));
  } else if (rc == -4) {
    Serial.println(F("  -> TIMEOUT: WiFi sans Internet ou pare-feu"));
  } else if (rc == 4) {
    Serial.println(F("  -> BAD_CREDENTIALS: verifiez MQTT_USER / MQTT_PASSWORD"));
  } else if (rc == 5) {
    Serial.println(F("  -> NOT_AUTHORIZED: compte HiveMQ ou permissions"));
  }
}

static bool mqtt_connect() {
  if (strlen(MQTT_BROKER_HOST) == 0) return false;
  if (WiFi.status() != WL_CONNECTED) return false;

#if MQTT_USE_TLS
  s_wifi_mqtt.setInsecure();
#endif

  s_mqtt.setServer(MQTT_BROKER_HOST, MQTT_BROKER_PORT);
  s_mqtt.setCallback(mqtt_on_message);
  s_mqtt.setBufferSize(1536);
  if (s_mqtt.connected()) return true;

  Serial.print(F("[MQTT] Connexion "));
  Serial.print(MQTT_BROKER_HOST);
  Serial.print(F(":"));
  Serial.print(MQTT_BROKER_PORT);
#if MQTT_USE_TLS
  Serial.print(F(" (TLS)"));
#endif
  Serial.println(F(" ..."));

  bool ok;
#if MQTT_USE_AUTH
  ok = s_mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASSWORD);
#else
  ok = s_mqtt.connect(MQTT_CLIENT_ID);
#endif
  if (!ok) {
    mqtt_print_rc_hint(s_mqtt.state());
    return false;
  }
  if (s_mqtt.subscribe(MQTT_TOPIC_COMMAND)) {
    Serial.print(F("[MQTT] Abonne "));
    Serial.println(MQTT_TOPIC_COMMAND);
  } else {
    Serial.println(F("[MQTT] Echec abonnement commande"));
  }
  return true;
}

void mqtt_irrigation_begin() {
  s_last_reconnect_ms = 0;
  if (strlen(MQTT_BROKER_HOST) == 0) {
    Serial.println(F("[MQTT] Desactive: MQTT_BROKER_HOST vide."));
    return;
  }
  Serial.println(F("[MQTT] Config firmware :"));
  Serial.print(F("  broker="));
  Serial.println(MQTT_BROKER_HOST);
  Serial.print(F("  port="));
  Serial.print(MQTT_BROKER_PORT);
#if MQTT_USE_TLS
  Serial.println(F(" TLS=oui"));
#else
  Serial.println(F(" TLS=non"));
#endif
  Serial.print(F("  telemetry="));
  Serial.println(MQTT_TOPIC_TELEMETRY);
  if (strlen(MQTT_PASSWORD) < 4 || strstr(MQTT_PASSWORD, "REMPLACER") != nullptr) {
    Serial.println(F("[MQTT] ATTENTION: MQTT_PASSWORD non configure dans weather_secrets.h"));
  }
  mqtt_connect();
}

void mqtt_irrigation_loop() {
  if (strlen(MQTT_BROKER_HOST) == 0) return;
  if (WiFi.status() != WL_CONNECTED) return;
  if (!s_mqtt.connected()) {
    uint32_t ms = millis();
    if (ms - s_last_reconnect_ms < 3000UL) return;
    s_last_reconnect_ms = ms;
    mqtt_connect();
    return;
  }
  s_mqtt.loop();
}

void mqtt_irrigation_publish_state(
    uint32_t uptime_s,
    bool wifi_ok,
    const char* ip,
    float soil_local,
    float temp_c,
    float rh_pct,
    float flow_lpm,
    float wx_temp,
    float wx_rh,
    float wx_rain,
    float wx_wind,
    bool wx_station_used,
    const float raw_features[5],
    int crop_idx,
    int soil_idx,
    int crop_age_days,
    bool manual_ok,
    float clf_p,
    bool fallback,
    bool wind_blk,
    int irrigate,
    float vol_model_l,
    float soil_target_pct,
    bool valve_on,
    float dose_d,
    float dose_t) {
  if (strlen(MQTT_BROKER_HOST) == 0) return;
  if (!s_mqtt.connected()) return;

  auto jf = [](float v) { return (isnan(v) || isinf(v)) ? 0.f : v; };

  StaticJsonDocument<1200> doc;
  doc["wifi_connected"] = wifi_ok;
  doc["ip"] = ip;
  doc["uptime_s"] = uptime_s;

  JsonObject s = doc.createNestedObject("sensors");
  s["soil_pct"] = jf(soil_local);
  s["temp_c"] = jf(temp_c);
  s["rh_pct"] = jf(rh_pct);
  s["flow_lpm"] = jf(flow_lpm);

  JsonObject w = doc.createNestedObject("weather");
  w["temp_c"] = jf(wx_temp);
  w["rh_pct"] = jf(wx_rh);
  w["rain_mm"] = jf(wx_rain);
  w["wind_ms"] = jf(wx_wind);
  w["station_used"] = wx_station_used;

  JsonObject m = doc.createNestedObject("model_inputs");
  m["soil_pct"] = jf(raw_features[0]);
  m["temp_c"] = jf(raw_features[1]);
  m["rh_pct"] = jf(raw_features[2]);
  m["rain_mm"] = jf(raw_features[3]);
  if (manual_ok) {
    m["age_days"] = jf((float)crop_age_days);
    m["crop"] = kCropNames[crop_idx];
    m["soil"] = kSoilNames[soil_idx];
    m["crop_idx"] = crop_idx;
    m["soil_idx"] = soil_idx;
  } else {
    m["age_days"] = nullptr;
    m["crop"] = "";
    m["soil"] = "";
    m["crop_idx"] = nullptr;
    m["soil_idx"] = nullptr;
  }

  JsonObject d = doc.createNestedObject("decision");
  d["prediction_active"] = manual_ok;
  d["p"] = jf(clf_p);
  d["fallback"] = fallback;
  d["wind_block"] = wind_blk;
  d["irrigate"] = irrigate;
  d["volume_model_l"] = jf(vol_model_l);
  d["soil_target_pct"] = jf(soil_target_pct);
  d["valve_on"] = valve_on;
  d["dose_delivered_l"] = jf(dose_d);
  d["dose_target_l"] = jf(dose_t);

  JsonObject man = doc.createNestedObject("manual");
  man["confirmed"] = manual_ok;
  if (manual_ok) {
    man["crop_age_days"] = crop_age_days;
    man["crop_idx"] = crop_idx;
    man["soil_idx"] = soil_idx;
  } else {
    man["crop_age_days"] = nullptr;
    man["crop_idx"] = nullptr;
    man["soil_idx"] = nullptr;
  }

  size_t n = serializeJson(doc, s_payload, sizeof(s_payload) - 1);
  if (n == 0 || doc.overflowed()) {
    Serial.println(F("[MQTT] JSON telemetry trop grand ou overflow"));
    return;
  }
  s_payload[n] = '\0';
  if (!s_mqtt.publish(MQTT_TOPIC_TELEMETRY, s_payload, true)) {
    Serial.println(F("[MQTT] publish telemetry echoue"));
  }
}

#endif  // ENABLE_MQTT

