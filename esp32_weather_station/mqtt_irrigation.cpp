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
static char s_payload[1536];
static char s_client_id[48];
static char s_rx_cmd[512];
static uint32_t s_last_reconnect_ms;
static uint32_t s_publish_ok_count;
static uint32_t s_last_publish_skip_log_ms;
static uint32_t s_last_publish_ok_log_ms;
static uint32_t s_last_publish_fail_log_ms;
static bool s_mqtt_buffer_ok = false;

static void mqtt_make_client_id() {
  uint32_t mac = (uint32_t)(ESP.getEfuseMac() >> 16);
  snprintf(s_client_id, sizeof(s_client_id), "%s_%04X", MQTT_CLIENT_ID, (unsigned)(mac & 0xFFFF));
}

static bool mqtt_config_buffer() {
  if (s_mqtt.connected()) return s_mqtt_buffer_ok;
  s_mqtt_buffer_ok = false;
  if (s_mqtt.setBufferSize(1536)) {
    s_mqtt_buffer_ok = true;
    Serial.println(F("[MQTT] Buffer 1536 octets OK"));
    return true;
  }
  Serial.print(F("[MQTT] setBufferSize echoue, heap="));
  Serial.println(ESP.getFreeHeap());
  return false;
}

static const char* kCropNames[] = {"Maize", "Rice", "Tomato", "Wheat"};
static const char* kSoilNames[] = {"Clayey", "Loamy", "Sandy", "Silty"};

static bool mqtt_apply_manual_json(StaticJsonDocument<256>& doc, const char* via) {
  const char* cmd = doc["cmd"] | "";
  bool is_relay = (cmd && strcmp(cmd, "manual") == 0);
  if (!is_relay && doc.containsKey("sensors")) {
    return false;
  }
  int age = doc["crop_age_days"] | 0;
  int ci = doc["crop_idx"] | 0;
  int si = doc["soil_idx"] | 0;
  if (age < 1 && !is_relay) return false;
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
  Serial.print(F("[MQTT] Saisie manuelle appliquee ("));
  Serial.print(via);
  Serial.println(F(")."));
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
  return true;
}

static void mqtt_on_message(char* topic, byte* payload, unsigned int length) {
  Serial.print(F("[MQTT] RX topic="));
  Serial.println(topic);
  if (length >= sizeof(s_rx_cmd)) length = sizeof(s_rx_cmd) - 1;
  memcpy(s_rx_cmd, payload, length);
  s_rx_cmd[length] = '\0';

  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, s_rx_cmd);
  if (err) {
    Serial.print(F("[MQTT] JSON invalide: "));
    Serial.println(err.c_str());
    return;
  }
  if (mqtt_apply_manual_json(doc, "commande")) return;
  Serial.print(F("[MQTT] Commande ignoree, payload="));
  Serial.println(s_rx_cmd);
}

static void mqtt_publish_online_ping();

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
  mqtt_config_buffer();
  if (s_mqtt.connected()) return true;

  Serial.print(F("[MQTT] Connexion "));
  Serial.print(MQTT_BROKER_HOST);
  Serial.print(F(":"));
  Serial.print(MQTT_BROKER_PORT);
#if MQTT_USE_TLS
  Serial.print(F(" (TLS)"));
#endif
  Serial.println(F(" ..."));

  mqtt_make_client_id();
  Serial.print(F("[MQTT] client_id="));
  Serial.println(s_client_id);

  bool ok;
#if MQTT_USE_AUTH
  ok = s_mqtt.connect(s_client_id, MQTT_USER, MQTT_PASSWORD);
#else
  ok = s_mqtt.connect(s_client_id);
#endif
  if (!ok) {
    mqtt_print_rc_hint(s_mqtt.state());
    return false;
  }
  for (int i = 0; i < 5; i++) {
    s_mqtt.loop();
    delay(10);
  }
  Serial.println(F("[MQTT] Connecte a HiveMQ"));
  bool sub_ok = s_mqtt.subscribe(MQTT_TOPIC_COMMAND);
  if (sub_ok) {
    Serial.print(F("[MQTT] Abonne "));
    Serial.println(MQTT_TOPIC_COMMAND);
  } else {
    Serial.println(F("[MQTT] Echec abonnement commande"));
  }
  const char* legacy_cmd = "irrigation/command/manual";
  if (strcmp(legacy_cmd, MQTT_TOPIC_COMMAND) != 0) {
    if (s_mqtt.subscribe(legacy_cmd)) {
      Serial.print(F("[MQTT] Abonne (legacy) "));
      Serial.println(legacy_cmd);
    }
  }
  if (strlen(MQTT_TOPIC_RELAY) > 0 && s_mqtt.subscribe(MQTT_TOPIC_RELAY)) {
    Serial.print(F("[MQTT] Abonne (relay) "));
    Serial.println(MQTT_TOPIC_RELAY);
  }
  mqtt_publish_online_ping();
  return true;
}

/** Petit message garanti < 400 o — verifie permission PUBLISH sur topic telemetry. */
static void mqtt_publish_online_ping() {
  if (!s_mqtt.connected()) return;

  StaticJsonDocument<384> doc;
  doc["wifi_connected"] = true;
  char ipb[20] = {};
  WiFi.localIP().toString().toCharArray(ipb, sizeof(ipb));
  doc["ip"] = ipb;
  doc["uptime_s"] = millis() / 1000UL;

  JsonObject s = doc.createNestedObject("sensors");
  s["soil_pct"] = 0.f;
  s["temp_c"] = 0.f;
  s["rh_pct"] = 0.f;
  s["flow_lpm"] = 0.f;

  JsonObject w = doc.createNestedObject("weather");
  w["temp_c"] = 0.f;
  w["rh_pct"] = 0.f;
  w["rain_mm"] = 0.f;
  w["wind_ms"] = 0.f;
  w["station_used"] = false;

  doc.createNestedObject("model_inputs");
  JsonObject man = doc.createNestedObject("manual");
  man["confirmed"] = false;
  JsonObject dec = doc.createNestedObject("decision");
  dec["prediction_active"] = false;

  char buf[400];
  size_t n = serializeJson(doc, buf, sizeof(buf) - 1);
  if (n == 0) return;
  buf[n] = '\0';

  bool pub = s_mqtt.publish(MQTT_TOPIC_TELEMETRY, buf, true);
  if (!pub) pub = s_mqtt.publish(MQTT_TOPIC_TELEMETRY, buf, false);
  s_mqtt.loop();
  Serial.print(F("[MQTT] Heartbeat telemetry "));
  Serial.print(pub ? F("OK") : F("ECHEC"));
  Serial.print(F(" ("));
  Serial.print(n);
  Serial.print(F(" o) topic="));
  Serial.println(MQTT_TOPIC_TELEMETRY);
  if (!pub) {
    Serial.println(F("[MQTT] -> HiveMQ: autoriser PUBLISH sur irrigation/# pour cet utilisateur"));
  }
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
  Serial.print(F("  user="));
  Serial.println(MQTT_USER);
  if (strlen(MQTT_PASSWORD) < 4 || strstr(MQTT_PASSWORD, "REMPLACER") != nullptr) {
    Serial.println(F("[MQTT] ATTENTION: MQTT_PASSWORD non configure dans weather_secrets.h"));
  }
  mqtt_connect();
}

void mqtt_irrigation_test_publish() {
  for (int i = 0; i < 8; i++) {
    s_mqtt.loop();
    delay(20);
  }
  if (!s_mqtt.connected()) {
    Serial.println(F("[MQTT] test: reconnexion..."));
    mqtt_connect();
  }
  if (!s_mqtt.connected()) {
    Serial.println(F("[MQTT] test: impossible (pas connecte a HiveMQ)"));
    return;
  }
  mqtt_publish_online_ping();
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
  for (int i = 0; i < 8; i++) {
    s_mqtt.loop();
  }
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
  if (!s_mqtt.connected()) {
    uint32_t ms = millis();
    if (ms - s_last_publish_skip_log_ms >= 30000UL) {
      s_last_publish_skip_log_ms = ms;
      Serial.println(F("[MQTT] Telemetrie non envoyee: MQTT deconnecte (WiFi OK mais pas HiveMQ)."));
    }
    return;
  }

  auto jf = [](float v) { return (isnan(v) || isinf(v)) ? 0.f : v; };

  StaticJsonDocument<1024> doc;
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
  if (manual_ok) {
    m["age_days"] = crop_age_days;
    m["crop_idx"] = crop_idx;
    m["soil_idx"] = soil_idx;
    m["crop"] = kCropNames[crop_idx];
    m["soil"] = kSoilNames[soil_idx];
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
  }

  if (doc.overflowed()) {
    Serial.println(F("[MQTT] JSON overflow"));
    return;
  }
  size_t need = measureJson(doc);
  if (need >= sizeof(s_payload)) {
    Serial.print(F("[MQTT] JSON trop grand ("));
    Serial.print(need);
    Serial.println(F(" o)"));
    return;
  }
  size_t n = serializeJson(doc, s_payload, sizeof(s_payload));
  if (n == 0) return;
  s_payload[n] = '\0';

  bool pub = s_mqtt.publish(MQTT_TOPIC_TELEMETRY, s_payload, true);
  if (!pub) pub = s_mqtt.publish(MQTT_TOPIC_TELEMETRY, s_payload, false);
  if (!pub) {
    uint32_t ms = millis();
    if (ms - s_last_publish_fail_log_ms >= 15000UL) {
      s_last_publish_fail_log_ms = ms;
      Serial.print(F("[MQTT] publish echoue rc="));
      Serial.print(s_mqtt.state());
      Serial.print(F(" n="));
      Serial.println(n);
    }
    return;
  }
  s_mqtt.loop();
  s_publish_ok_count++;
  uint32_t ms = millis();
  if (s_publish_ok_count <= 3 || ms - s_last_publish_ok_log_ms >= 60000UL) {
    s_last_publish_ok_log_ms = ms;
    Serial.print(F("[MQTT] OK publie "));
    Serial.print(n);
    Serial.print(F(" o -> "));
    Serial.println(MQTT_TOPIC_TELEMETRY);
  }
}

#endif  // ENABLE_MQTT

