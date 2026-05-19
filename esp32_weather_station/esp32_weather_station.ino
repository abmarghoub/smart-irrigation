#include <Arduino.h>
#include <math.h>
#include <stdio.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include "device_config.h"
#include "wifi_provision.h"
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <DHT.h>

#include "weather_secrets.h"
#include "mqtt_irrigation.h"
#include "dataset_log.h"
#if ENABLE_WEB_DASHBOARD
#include <WebServer.h>
#include "web_dashboard.h"
#endif
#include "preprocess_params.h"
#include "smallnn_classifier_weights.h"
#include "smallnn_regressor_weights.h"

// Fallback geo optionnel (a definir dans weather_secrets.h si besoin)
#ifndef WEATHER_HAS_FIXED_GEO
#define WEATHER_HAS_FIXED_GEO 0
#endif
#ifndef WEATHER_FIXED_LAT
#define WEATHER_FIXED_LAT 0.0f
#endif
#ifndef WEATHER_FIXED_LON
#define WEATHER_FIXED_LON 0.0f
#endif

// --- Capteurs utilisateur ---
static const int PIN_DHT = 4;
static const int PIN_SOIL_ADC = 34;
static const int PIN_FLOW = 5;
static const int PIN_VALVE = 18;

static DHT dht(PIN_DHT, DHT22);

// Sonde sol B42: calibrer selon vos mesures ADC (sec / humide)
static int SOIL_ADC_DRY = 3500;
static int SOIL_ADC_WET = 1500;

// YF-S201: Q(L/min) = freq_hz / 7.5 (valeur typique)
static float FLOW_PULSES_PER_LPM = 7.5f;
volatile uint32_t g_flow_pulses = 0;
static volatile uint32_t g_flow_last_us = 0;
static float g_flow_lpm = 0.f;
static uint32_t g_prev_ms = 0;

// Saisie utilisateur (crop_name / soil_type / crop_age_days) — actifs seulement apres confirmation
// (non static : visibles par mqtt_irrigation.cpp pour les commandes MQTT)
int g_crop_idx = 0; // 0 Maize,1 Rice,2 Tomato,3 Wheat
int g_soil_idx = 0; // 0 Clayey,1 Loamy,2 Sandy,3 Silty
int g_crop_age_days = 1;
static const char* kCropNames[] = {"Maize", "Rice", "Tomato", "Wheat"};
static const char* kSoilNames[] = {"Clayey", "Loamy", "Sandy", "Silty"};

// Station meteo (Open-Meteo via IP -> lat/lon)
static bool g_geo_ok = false;
static float g_lat = 0.f, g_lon = 0.f;
static float g_wx_temp = NAN;
static float g_wx_hum = NAN;
static float g_wx_rain_mm = NAN;
static float g_wx_wind_ms = NAN;
static uint32_t g_last_weather_ms = 0;
static const uint32_t WEATHER_FETCH_MS = 10UL * 60UL * 1000UL;
bool g_manual_ready = false;

static const float MLP_CONFIDENCE_MARGIN = 0.15f;
static const float WIND_BLOCK_THRESHOLD_MS = 8.0f; // securite: vent fort => stop arrosage
// Protection anti-gaspillage: dose limitee + pause entre 2 arrosages
static const float WATER_REQUEST_SCALE = 1.0f; // la calibration est faite dans la sortie MLP
// Reglages "test plante": limites prudentes pour eviter tout sur-arrosage.
static const float MIN_DOSE_LITERS = 0.20f;     // petite impulsion minimale
static const float MAX_DOSE_LITERS = 2.00f;     // plafond par cycle
static const uint32_t IRRIGATION_COOLDOWN_MS = 30UL * 60UL * 1000UL;
static const float SOIL_TARGET_PCT_BY_TYPE[4] = {42.f, 38.f, 30.f, 36.f}; // Clayey, Loamy, Sandy, Silty
static const float SOIL_HYSTERESIS_PCT = 3.0f;
// MLP regression: cible entrainement ~0.12–1.8 L (sqrt compression); plafond coherence.
static const float MLP_PRED_MAX_LITERS = 1.8f;

struct Decision {
  int irrigate;
  float water_m3;
  float p;
  bool fallback_used;
  bool wind_blocked;
};

struct IrrigationDoseState {
  bool active;
  float target_liters;
  float delivered_liters;
  uint32_t last_stop_ms;
};

static IrrigationDoseState g_dose{false, 0.f, 0.f, 0};

// Intervalle capteurs / decision / publication (meme cadence qu’avec l’ancien dashboard web)
static uint32_t g_last_sensor_cycle_ms = 0;

void irrigation_request_sensor_cycle() {
  g_last_sensor_cycle_ms = 0;
}

#if ENABLE_WEB_DASHBOARD
static WebServer g_web(DASHBOARD_PORT);

struct WebDashState {
  bool wifi_ok;
  char ip[20];
  uint32_t uptime_s;
  float soil_local, temp_c, rh_pct, flow_lpm;
  float wx_temp, wx_rh, wx_rain, wx_wind;
  bool wx_station_used;
  float in_soil, in_temp, in_rh, in_rain, in_age;
  int crop_idx, soil_idx, crop_age;
  char crop_name[12];
  char soil_name[12];
  bool manual_ok;
  float clf_p;
  bool fallback, wind_blk;
  int irrigate;
  float vol_model_l, soil_target_pct;
  bool valve_on;
  float dose_delivered_l, dose_target_l;
};
static WebDashState g_dash{};

static void web_send_cors() {
  g_web.sendHeader("Access-Control-Allow-Origin", "*");
}

static void handleRoot() {
  g_web.send(200, "text/html", DASHBOARD_INDEX_HTML);
}

static void handleApiState() {
  web_send_cors();
  g_web.sendHeader("Content-Type", "application/json; charset=utf-8");
  StaticJsonDocument<1536> doc;
  auto jf = [](float v) { return (isnan(v) || isinf(v)) ? 0.f : v; };
  doc["wifi_connected"] = g_dash.wifi_ok;
  doc["ip"] = g_dash.ip;
  doc["uptime_s"] = g_dash.uptime_s;
  JsonObject s = doc.createNestedObject("sensors");
  s["soil_pct"] = jf(g_dash.soil_local);
  s["temp_c"] = jf(g_dash.temp_c);
  s["rh_pct"] = jf(g_dash.rh_pct);
  s["flow_lpm"] = jf(g_dash.flow_lpm);
  JsonObject w = doc.createNestedObject("weather");
  w["temp_c"] = jf(g_dash.wx_temp);
  w["rh_pct"] = jf(g_dash.wx_rh);
  w["rain_mm"] = jf(g_dash.wx_rain);
  w["wind_ms"] = jf(g_dash.wx_wind);
  w["station_used"] = g_dash.wx_station_used;
  JsonObject m = doc.createNestedObject("model_inputs");
  m["soil_pct"] = jf(g_dash.in_soil);
  m["temp_c"] = jf(g_dash.in_temp);
  m["rh_pct"] = jf(g_dash.in_rh);
  m["rain_mm"] = jf(g_dash.in_rain);
  if (g_dash.manual_ok) {
    m["age_days"] = jf(g_dash.in_age);
    m["crop"] = g_dash.crop_name;
    m["soil"] = g_dash.soil_name;
    m["crop_idx"] = g_dash.crop_idx;
    m["soil_idx"] = g_dash.soil_idx;
  } else {
    m["age_days"] = nullptr;
    m["crop"] = "";
    m["soil"] = "";
    m["crop_idx"] = nullptr;
    m["soil_idx"] = nullptr;
  }
  JsonObject d = doc.createNestedObject("decision");
  d["prediction_active"] = g_dash.manual_ok;
  d["p"] = jf(g_dash.clf_p);
  d["fallback"] = g_dash.fallback;
  d["wind_block"] = g_dash.wind_blk;
  d["irrigate"] = g_dash.irrigate;
  d["volume_model_l"] = jf(g_dash.vol_model_l);
  d["soil_target_pct"] = jf(g_dash.soil_target_pct);
  d["valve_on"] = g_dash.valve_on;
  d["dose_delivered_l"] = jf(g_dash.dose_delivered_l);
  d["dose_target_l"] = jf(g_dash.dose_target_l);
  JsonObject man = doc.createNestedObject("manual");
  man["confirmed"] = g_dash.manual_ok;
  if (g_dash.manual_ok) {
    man["crop_age_days"] = g_dash.crop_age;
    man["crop_idx"] = g_dash.crop_idx;
    man["soil_idx"] = g_dash.soil_idx;
  } else {
    man["crop_age_days"] = nullptr;
    man["crop_idx"] = nullptr;
    man["soil_idx"] = nullptr;
  }
  String out;
  out.reserve(measureJson(doc) + 32);
  serializeJson(doc, out);
  if (doc.overflowed()) {
    Serial.println(F("[WEB] JSON overflow /api/state"));
  }
  g_web.send(200, "application/json", out);
}

static void handleApiManual() {
  web_send_cors();
  if (g_web.method() != HTTP_POST) {
    g_web.send(405, "application/json", "{\"error\":\"POST only\"}");
    return;
  }
  int age = g_web.arg("crop_age_days").toInt();
  int ci = g_web.arg("crop_idx").toInt();
  int si = g_web.arg("soil_idx").toInt();
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
  Serial.println(F("[MANUEL] (dashboard) saisie confirmee ->"));
  Serial.print(F("  age_jours="));
  Serial.print(g_crop_age_days);
  Serial.print(F("  culture_idx="));
  Serial.print(g_crop_idx);
  Serial.print(F(" ("));
  Serial.print(kCropNames[g_crop_idx]);
  Serial.print(F(")  sol_idx="));
  Serial.print(g_soil_idx);
  Serial.print(F(" ("));
  Serial.print(kSoilNames[g_soil_idx]);
  Serial.println(F(")"));
  g_web.send(200, "application/json", "{\"ok\":true}");
}

static void web_begin_if_wifi() {
  static bool started = false;
  if (started || WiFi.status() != WL_CONNECTED) return;
  started = true;
  g_web.on("/", HTTP_GET, handleRoot);
  g_web.on("/api/state", HTTP_GET, handleApiState);
  g_web.on("/api/manual", HTTP_POST, handleApiManual);
#if ENABLE_DATASET_LOG
  dataset_log_register_routes(g_web);
#endif
  g_web.begin();
  Serial.print(F("Dashboard web: http://"));
  Serial.print(WiFi.localIP());
  Serial.print(F(":"));
  Serial.print(DASHBOARD_PORT);
  Serial.println(F("/"));
}

static void web_update_snapshot(
    float soil_local, float t_local, float h_local, float flow_lpm, bool station_used,
    const float raw[IRR_NUM_FEATURES], const Decision& d, float soil_target, float model_liters,
    bool valve_on, float dose_d, float dose_t) {
  g_dash.wifi_ok = (WiFi.status() == WL_CONNECTED);
  if (g_dash.wifi_ok) {
    WiFi.localIP().toString().toCharArray(g_dash.ip, sizeof(g_dash.ip));
  } else {
    g_dash.ip[0] = '\0';
  }
  g_dash.uptime_s = millis() / 1000UL;
  g_dash.soil_local = soil_local;
  g_dash.temp_c = t_local;
  g_dash.rh_pct = h_local;
  g_dash.flow_lpm = flow_lpm;
  g_dash.wx_temp = isnan(g_wx_temp) ? 0.f : g_wx_temp;
  g_dash.wx_rh = isnan(g_wx_hum) ? 0.f : g_wx_hum;
  g_dash.wx_rain = isnan(g_wx_rain_mm) ? 0.f : g_wx_rain_mm;
  g_dash.wx_wind = isnan(g_wx_wind_ms) ? 0.f : g_wx_wind_ms;
  g_dash.wx_station_used = station_used;
  g_dash.in_soil = raw[0];
  g_dash.in_temp = raw[1];
  g_dash.in_rh = raw[2];
  g_dash.in_rain = raw[3];
  g_dash.in_age = raw[4];
  g_dash.crop_idx = g_crop_idx;
  g_dash.soil_idx = g_soil_idx;
  g_dash.crop_age = g_crop_age_days;
  if (g_manual_ready) {
    snprintf(g_dash.crop_name, sizeof(g_dash.crop_name), "%s", kCropNames[g_crop_idx]);
    snprintf(g_dash.soil_name, sizeof(g_dash.soil_name), "%s", kSoilNames[g_soil_idx]);
  } else {
    snprintf(g_dash.crop_name, sizeof(g_dash.crop_name), "%s", "");
    snprintf(g_dash.soil_name, sizeof(g_dash.soil_name), "%s", "");
  }
  g_dash.manual_ok = g_manual_ready;
  g_dash.clf_p = d.p;
  g_dash.fallback = d.fallback_used;
  g_dash.wind_blk = d.wind_blocked;
  g_dash.irrigate = d.irrigate;
  g_dash.vol_model_l = model_liters;
  g_dash.soil_target_pct = soil_target;
  g_dash.valve_on = valve_on;
  g_dash.dose_delivered_l = dose_d;
  g_dash.dose_target_l = dose_t;
}
#endif

static void IRAM_ATTR flow_isr() {
  uint32_t now = micros();
  if (now - g_flow_last_us < 80) return;
  g_flow_last_us = now;
  g_flow_pulses++;
}

static float relu(float x) { return x > 0.f ? x : 0.f; }
static float sigmoid(float x) {
  if (x > 35.f) return 1.f;
  if (x < -35.f) return 0.f;
  return 1.f / (1.f + expf(-x));
}

static float soil_pct_from_adc(int adc) {
  if (SOIL_ADC_DRY == SOIL_ADC_WET) return 0.f;
  float t = (float)(SOIL_ADC_DRY - adc) / (float)(SOIL_ADC_DRY - SOIL_ADC_WET);
  if (t < 0.f) t = 0.f;
  if (t > 1.f) t = 1.f;
  return t * 100.f;
}

static float clampf(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

static float soil_target_pct(int soil_idx) {
  if (soil_idx < 0 || soil_idx > 3) return 36.f;
  return SOIL_TARGET_PCT_BY_TYPE[soil_idx];
}

static void poll_user_serial() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  char c0 = line.charAt(0);
  char c = (c0 >= 'a' && c0 <= 'z') ? (char)(c0 - 'a' + 'A') : c0;
  if (c == 'H') {
    Serial.println(F("Commandes: A <jours> | C <0-3> | S <0-3> | M | H"));
    Serial.println(F("Exemples: A 60  | C 2 | S 1"));
    Serial.println(F("  M = test publish MQTT telemetry (HiveMQ)"));
    Serial.println(F("Formats acceptes: A60 / A=60 / A 60"));
    return;
  }
#if ENABLE_MQTT
  if (c == 'M') {
    mqtt_irrigation_test_publish();
    irrigation_request_sensor_cycle();
    return;
  }
#endif

  // Accepte "A 60", "A60", "A=60", "A:60"
  String arg = line.substring(1);
  arg.trim();
  if (arg.startsWith("=") || arg.startsWith(":")) {
    arg = arg.substring(1);
    arg.trim();
  }
  if (arg.length() == 0) {
    Serial.println(F("Commande incomplete. Tapez H pour aide."));
    return;
  }

  int v = arg.toInt();
  if (c == 'A') {
    if (v < 1) v = 1;
    if (v > 120) v = 120;
    g_crop_age_days = v;
    Serial.print(F("OK age="));
    Serial.println(g_crop_age_days);
    g_manual_ready = true;
  } else if (c == 'C') {
    if (v < 0) v = 0;
    if (v > 3) v = 3;
    g_crop_idx = v;
    Serial.print(F("OK crop="));
    Serial.print(g_crop_idx);
    Serial.print(F(" ("));
    Serial.print(kCropNames[g_crop_idx]);
    Serial.println(F(")"));
    g_manual_ready = true;
  } else if (c == 'S') {
    if (v < 0) v = 0;
    if (v > 3) v = 3;
    g_soil_idx = v;
    Serial.print(F("OK soil="));
    Serial.print(g_soil_idx);
    Serial.print(F(" ("));
    Serial.print(kSoilNames[g_soil_idx]);
    Serial.println(F(")"));
    g_manual_ready = true;
  } else {
    Serial.println(F("Commande inconnue. Tapez H."));
    return;
  }
  Serial.println(F("[MANUEL] (serie) saisie confirmee ->"));
  Serial.print(F("  age_jours="));
  Serial.print(g_crop_age_days);
  Serial.print(F("  culture_idx="));
  Serial.print(g_crop_idx);
  Serial.print(F(" ("));
  Serial.print(kCropNames[g_crop_idx]);
  Serial.print(F(")  sol_idx="));
  Serial.print(g_soil_idx);
  Serial.print(F(" ("));
  Serial.print(kSoilNames[g_soil_idx]);
  Serial.println(F(")"));
}

static bool fetch_geo_ip() {
  if (WiFi.status() != WL_CONNECTED) return false;
  // 1) Service principal: ip-api (HTTP)
  {
    HTTPClient http;
    if (http.begin("http://ip-api.com/json/?fields=status,lat,lon")) {
      http.setTimeout(10000);
      int code = http.GET();
      if (code == 200) {
        String body = http.getString();
        StaticJsonDocument<384> doc;
        DeserializationError err = deserializeJson(doc, body);
        if (!err) {
          const char* st = doc["status"] | "";
          if (strcmp(st, "success") == 0) {
            g_lat = doc["lat"] | 0.f;
            g_lon = doc["lon"] | 0.f;
            g_geo_ok = !(g_lat == 0.f && g_lon == 0.f);
            if (g_geo_ok) {
              Serial.print(F("[GEO] ip-api lat="));
              Serial.print(g_lat, 4);
              Serial.print(F(" lon="));
              Serial.println(g_lon, 4);
              http.end();
              return true;
            }
          }
        } else {
          Serial.print(F("[GEO] ip-api JSON invalide: "));
          Serial.println(err.c_str());
        }
      } else {
        Serial.print(F("[GEO] ip-api HTTP code="));
        Serial.print(code);
        if (code < 0) {
          Serial.print(F(" err="));
          Serial.print(HTTPClient::errorToString(code));
        }
        Serial.println();
      }
      http.end();
    }
  }

  // 2) Fallback: ipwho.is (HTTPS)
  {
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    if (http.begin(client, "https://ipwho.is/")) {
      http.setTimeout(12000);
      int code = http.GET();
      if (code == 200) {
        String body = http.getString();
        StaticJsonDocument<512> doc;
        DeserializationError err = deserializeJson(doc, body);
        if (!err) {
          bool success = doc["success"] | false;
          if (success) {
            g_lat = doc["latitude"] | 0.f;
            g_lon = doc["longitude"] | 0.f;
            g_geo_ok = !(g_lat == 0.f && g_lon == 0.f);
            if (g_geo_ok) {
              Serial.print(F("[GEO] ipwho.is lat="));
              Serial.print(g_lat, 4);
              Serial.print(F(" lon="));
              Serial.println(g_lon, 4);
              http.end();
              return true;
            }
          } else {
            Serial.println(F("[GEO] ipwho.is success=false"));
          }
        } else {
          Serial.print(F("[GEO] ipwho.is JSON invalide: "));
          Serial.println(err.c_str());
        }
      } else {
        Serial.print(F("[GEO] ipwho.is HTTP code="));
        Serial.print(code);
        if (code < 0) {
          Serial.print(F(" err="));
          Serial.print(HTTPClient::errorToString(code));
        }
        Serial.println();
      }
      http.end();
    } else {
      Serial.println(F("[GEO] Echec init HTTPS ipwho.is"));
    }
  }

  // 3) Fallback final: coordonnees fixes
  #if WEATHER_HAS_FIXED_GEO
  g_lat = WEATHER_FIXED_LAT;
  g_lon = WEATHER_FIXED_LON;
  g_geo_ok = !(g_lat == 0.f && g_lon == 0.f);
  if (g_geo_ok) {
    Serial.print(F("[GEO] Coordonnees fixes lat="));
    Serial.print(g_lat, 4);
    Serial.print(F(" lon="));
    Serial.println(g_lon, 4);
    return true;
  }
  #endif

  Serial.println(F("[GEO] Aucun service geoloc disponible"));
  return false;
}

static bool fetch_open_meteo() {
  if (WiFi.status() != WL_CONNECTED || !g_geo_ok) return false;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  String url = "https://api.open-meteo.com/v1/forecast?latitude=" + String(g_lat, 5) +
               "&longitude=" + String(g_lon, 5) +
               "&current=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m&windspeed_unit=ms&timezone=auto";
  if (!http.begin(client, url)) {
    Serial.println(F("[METEO] Echec init HTTPS Open-Meteo"));
    return false;
  }
  http.setTimeout(12000);
  int code = http.GET();
  if (code != 200) {
    Serial.print(F("[METEO] Open-Meteo HTTP code="));
    Serial.print(code);
    if (code < 0) {
      Serial.print(F(" err="));
      Serial.print(HTTPClient::errorToString(code));
    }
    Serial.println();
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

 
  StaticJsonDocument<1536> doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.print(F("[METEO] JSON invalide: "));
    Serial.println(err.c_str());
    return false;
  }
  JsonObject cur = doc["current"];
  if (cur.isNull()) {
    Serial.println(F("[METEO] Champ 'current' absent"));
    return false;
  }

  g_wx_temp = cur["temperature_2m"] | NAN;
  g_wx_hum = cur["relative_humidity_2m"] | NAN;
  g_wx_rain_mm = cur["precipitation"] | 0.f;
  // Certaines variantes d'API utilisent "windspeed_10m" au lieu de "wind_speed_10m".
  g_wx_wind_ms = cur["wind_speed_10m"] | (cur["windspeed_10m"] | NAN);
  g_last_weather_ms = millis();
  Serial.print(F("[METEO] T="));
  Serial.print(g_wx_temp, 1);
  Serial.print(F("C RH="));
  Serial.print(g_wx_hum, 1);
  Serial.print(F("% Pluie="));
  Serial.print(g_wx_rain_mm, 2);
  Serial.print(F("mm Vent="));
  Serial.print(isnan(g_wx_wind_ms) ? 0.f : g_wx_wind_ms, 2);
  Serial.println(F("m/s"));
  return true;
}

static float mlp_clf_logit(const float x[IRR_INPUT_DIM]) {
  float h[8];
  for (int j = 0; j < 8; j++) {
    float s = SMALLNN_CLF_B0[j];
    for (int i = 0; i < IRR_INPUT_DIM; i++) s += x[i] * SMALLNN_CLF_W0[i * 8 + j];
    h[j] = relu(s);
  }
  float y = SMALLNN_CLF_B1[0];
  for (int j = 0; j < 8; j++) y += h[j] * SMALLNN_CLF_W1[j];
  return y;
}

static float mlp_reg_volume(const float x[IRR_INPUT_DIM]) {
  float h[8];
  for (int j = 0; j < 8; j++) {
    float s = SMALLNN_REG_B0[j];
    for (int i = 0; i < IRR_INPUT_DIM; i++) s += x[i] * SMALLNN_REG_W0[i * 8 + j];
    h[j] = relu(s);
  }
  float y = SMALLNN_REG_B1[0];
  for (int j = 0; j < 8; j++) y += h[j] * SMALLNN_REG_W1[j];
  return y;
}

static float mlp_predict_irrigation_liters(const float x[IRR_INPUT_DIM], float clf_prob) {
  float lit = mlp_reg_volume(x);
  if (lit < 0.f) lit = 0.f;
  lit = fminf(lit, MLP_PRED_MAX_LITERS);
  float confidence = clampf(fabsf(clf_prob - 0.5f) * 2.0f, 0.f, 1.f);
  float confidence_factor = 0.65f + 0.35f * confidence;
  lit *= confidence_factor;
  return lit;
}

static void make_input(const float raw[IRR_NUM_FEATURES], float x[IRR_INPUT_DIM]) {
  for (int i = 0; i < IRR_NUM_FEATURES; i++) x[i] = raw[i] * IRR_NUM_SCALE[i] + IRR_NUM_MIN[i];
  for (int i = 0; i < IRR_CROP_CLASSES; i++) x[IRR_NUM_FEATURES + i] = (i == g_crop_idx) ? 1.f : 0.f;
  for (int i = 0; i < IRR_SOIL_CLASSES; i++) x[IRR_NUM_FEATURES + IRR_CROP_CLASSES + i] = (i == g_soil_idx) ? 1.f : 0.f;
}

static Decision decide(const float x[IRR_INPUT_DIM], const float raw[IRR_NUM_FEATURES], float wind_ms) {
  Decision d{};
  d.p = sigmoid(mlp_clf_logit(x));
  d.fallback_used = false;
  d.wind_blocked = false;

  if (fabsf(d.p - 0.5f) >= MLP_CONFIDENCE_MARGIN) {
    d.irrigate = (d.p >= 0.5f) ? 1 : 0;
  } else {
    // Repli simple (secondaire) si le MLP est incertain
    d.fallback_used = true;
    d.irrigate = (raw[0] < 30.f && raw[3] < 1.0f) ? 1 : 0;
  }
  if (d.irrigate) {
    d.water_m3 = mlp_predict_irrigation_liters(x, d.p) / 1000.f;
  } else {
    d.water_m3 = 0.f;
  }

  // Securite operationnelle: ne pas arroser si vent fort.
  if (wind_ms >= WIND_BLOCK_THRESHOLD_MS) {
    d.wind_blocked = true;
    d.irrigate = 0;
    d.water_m3 = 0.f;
  }
  return d;
}

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(PIN_SOIL_ADC, INPUT);
  pinMode(PIN_FLOW, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_FLOW), flow_isr, FALLING);
  pinMode(PIN_VALVE, OUTPUT);
  digitalWrite(PIN_VALVE, LOW);
  dht.begin();
  g_prev_ms = millis();

  Serial.println(F("=== Irrigation ESP32: capteurs + station meteo + decision ==="));
  Serial.println(F("Capteurs: DHT22(GPIO4), Sol B42(GPIO34), YF-S201(GPIO5), Vanne(GPIO18)"));
  Serial.println(F("Saisie: A <jours> | C <0-3> | S <0-3> | M (test MQTT) | H"));
  Serial.println(F("Prediction MLP desactivee tant que la saisie n'est pas confirmee (MQTT depuis le PC, formulaire du dashboard local, ou A/C/S au moniteur serie)."));

  device_config_begin();
  if (wifi_provision_connect()) {
    if (fetch_geo_ip()) {
      Serial.print(F("Zone IP lat="));
      Serial.print(g_lat, 4);
      Serial.print(F(" lon="));
      Serial.println(g_lon, 4);
      fetch_open_meteo();
    }
#if ENABLE_WEB_DASHBOARD
    web_begin_if_wifi();
#endif
  } else if (wifi_provision_portal_active()) {
    Serial.println(F("Portail WiFi actif : connectez le telephone a Irrigation-Setup puis 192.168.4.1"));
  } else {
    Serial.println(F("WiFi non connecte : configurez weather_secrets.h ou le portail"));
  }
#if ENABLE_MQTT
  mqtt_irrigation_begin();
#endif
#if ENABLE_DATASET_LOG
  dataset_log_begin_fs();
#endif
}

void loop() {
  poll_user_serial();
  wifi_provision_loop();
  if (wifi_provision_portal_active()) {
    delay(10);
    return;
  }

#if ENABLE_DATASET_LOG
  if (WiFi.status() == WL_CONNECTED) dataset_log_poll_ntp();
#endif

#if ENABLE_WEB_DASHBOARD
  g_web.handleClient();
#endif
#if ENABLE_MQTT
  mqtt_irrigation_loop();
#endif

  uint32_t cycle_gate = millis();
  if (g_last_sensor_cycle_ms != 0 && cycle_gate - g_last_sensor_cycle_ms < 5000) {
    delay(10);
    return;
  }
  g_last_sensor_cycle_ms = cycle_gate;

  uint32_t now = millis();
  float dt_s = (now - g_prev_ms) / 1000.f;
  if (dt_s < 0.2f) dt_s = 0.2f;
  g_prev_ms = now;

  float raw[IRR_NUM_FEATURES] = {0.f, 0.f, 0.f, 0.f, 0.f};

  // Capteurs locaux
  int adc = analogRead(PIN_SOIL_ADC);
  float soil_local = soil_pct_from_adc(adc);
  float t_local = dht.readTemperature();
  float h_local = dht.readHumidity();
  raw[0] = soil_local;
  raw[1] = isnan(t_local) ? 0.f : t_local;
  raw[2] = isnan(h_local) ? 0.f : h_local;
  raw[4] = g_manual_ready ? (float)g_crop_age_days : 0.f;

  noInterrupts();
  uint32_t pulses = g_flow_pulses;
  g_flow_pulses = 0;
  interrupts();
  float hz = pulses / dt_s;
  g_flow_lpm = hz / FLOW_PULSES_PER_LPM;

  // Meteo station (complete uniquement les valeurs manquantes)
  bool station_used = false;
  if (WiFi.status() == WL_CONNECTED) {
    if (!g_geo_ok) fetch_geo_ip();
    if (g_geo_ok && (g_last_weather_ms == 0 || now - g_last_weather_ms >= WEATHER_FETCH_MS)) {
      fetch_open_meteo();
    }
    // temperature_C: toujours capteur local (jamais remplacee par la station)
    if (isnan(h_local) && !isnan(g_wx_hum)) { raw[2] = g_wx_hum; station_used = true; }
    // Pas de capteur pluie local -> pluie prise depuis la station si disponible
    if (!isnan(g_wx_rain_mm)) { raw[3] = g_wx_rain_mm; station_used = true; }
  }

  float x[IRR_INPUT_DIM];
  float wind_ms = isnan(g_wx_wind_ms) ? 0.f : g_wx_wind_ms;
  Decision d{};
  float soil_target = 0.f;
  bool moisture_enough = false;

  if (g_manual_ready) {
    make_input(raw, x);
    d = decide(x, raw, wind_ms);
    soil_target = soil_target_pct(g_soil_idx);
    moisture_enough = raw[0] >= (soil_target + SOIL_HYSTERESIS_PCT);
  } else {
    d.p = 0.f;
    d.irrigate = 0;
    d.water_m3 = 0.f;
    d.fallback_used = false;
    d.wind_blocked = false;
  }

  float requested_liters = 0.f;
  float model_liters = 0.f;
  if (g_manual_ready) {
    requested_liters = d.water_m3 * 1000.f * WATER_REQUEST_SCALE;
    model_liters = requested_liters;
    if (d.irrigate) {
      requested_liters = clampf(requested_liters, MIN_DOSE_LITERS, MAX_DOSE_LITERS);
    } else {
      requested_liters = 0.f;
      model_liters = 0.f;
    }
    if (moisture_enough) {
      requested_liters = 0.f;
    }
  }

  // Suivi volume distribue a partir du debitmetre.
  if (g_dose.active) {
    float add_liters = (g_flow_lpm > 0.f) ? (g_flow_lpm * dt_s / 60.f) : 0.f;
    g_dose.delivered_liters += add_liters;
  }

  bool cooldown_done = (g_dose.last_stop_ms == 0) || (now - g_dose.last_stop_ms >= IRRIGATION_COOLDOWN_MS);
  if (!g_dose.active) {
    bool should_start = g_manual_ready && (d.irrigate && requested_liters > 0.f && !moisture_enough);
    if (should_start && cooldown_done) {
      g_dose.active = true;
      g_dose.target_liters = requested_liters;
      g_dose.delivered_liters = 0.f;
    }
  } else {
    bool should_stop = false;
    if (!d.irrigate) should_stop = true; // la decision se retracte
    if (g_dose.delivered_liters >= g_dose.target_liters) should_stop = true; // dose atteinte
    if (moisture_enough && g_dose.delivered_liters >= MIN_DOSE_LITERS) should_stop = true; // sol deja ok
    if (should_stop) {
      g_dose.active = false;
      g_dose.last_stop_ms = now;
    }
  }

  digitalWrite(PIN_VALVE, g_dose.active ? HIGH : LOW);

  Serial.print(F("Capteurs locaux -> Sol=")); Serial.print(soil_local, 1);
  Serial.print(F("% T=")); Serial.print(isnan(t_local) ? 0.f : t_local, 1);
  Serial.print(F("C RH=")); Serial.print(isnan(h_local) ? 0.f : h_local, 1);
  Serial.print(F("% Debit=")); Serial.print(g_flow_lpm, 2); Serial.println(F("L/min"));

  Serial.print(F("Station meteo -> T="));
  Serial.print(isnan(g_wx_temp) ? 0.f : g_wx_temp, 1);
  Serial.print(F("C RH="));
  Serial.print(isnan(g_wx_hum) ? 0.f : g_wx_hum, 1);
  Serial.print(F("% Pluie(mm)="));
  Serial.print(isnan(g_wx_rain_mm) ? 0.f : g_wx_rain_mm, 2);
  Serial.print(F(" Vent(m/s)="));
  Serial.print(isnan(g_wx_wind_ms) ? 0.f : g_wx_wind_ms, 2);
  Serial.print(F(" source="));
  Serial.println(station_used ? F("station") : F("local"));

  if (g_manual_ready) {
    Serial.print(F("Entrees modele -> Sol="));
    Serial.print(raw[0], 1);
    Serial.print(F("% T="));
    Serial.print(raw[1], 1);
    Serial.print(F("C RH="));
    Serial.print(raw[2], 1);
    Serial.print(F("% Pluie(mm)="));
    Serial.print(raw[3], 2);
    Serial.print(F(" Age="));
    Serial.print(raw[4], 0);
    Serial.print(F(" Crop="));
    Serial.print(kCropNames[g_crop_idx]);
    Serial.print(F(" Soil="));
    Serial.println(kSoilNames[g_soil_idx]);

    Serial.print(F("Decision -> P="));
    Serial.print(d.p, 4);
    Serial.print(F(" fallback="));
    Serial.print(d.fallback_used ? F("oui") : F("non"));
    Serial.print(F(" vent_blocage="));
    Serial.print(d.wind_blocked ? F("oui") : F("non"));
    Serial.print(F(" irriguer="));
    Serial.print(d.irrigate);
    Serial.print(F(" volume_modele_l="));
    Serial.print(model_liters, 2);
    Serial.print(F(" cible_sol="));
    Serial.print(soil_target, 1);
    Serial.print(F(" vanne="));
    Serial.print(g_dose.active ? F("ON") : F("OFF"));
    Serial.print(F(" dose_livre_l="));
    Serial.print(g_dose.delivered_liters, 2);
    Serial.print(F("/"));
    Serial.println(g_dose.target_liters, 2);
  } else {
    Serial.println(F("Prediction / decision MLP: en attente de saisie manuelle (MQTT / dashboard PC ou A/C/S au moniteur serie)."));
  }
  Serial.println(F("---"));

#if ENABLE_WEB_DASHBOARD
  web_update_snapshot(
      soil_local, isnan(t_local) ? 0.f : t_local, isnan(h_local) ? 0.f : h_local, g_flow_lpm, station_used,
      raw, d, soil_target, model_liters, g_dose.active, g_dose.delivered_liters, g_dose.target_liters);
#endif
#if ENABLE_MQTT
  if (WiFi.status() == WL_CONNECTED) {
    char ipbuf[20] = {};
    WiFi.localIP().toString().toCharArray(ipbuf, sizeof(ipbuf));
    mqtt_irrigation_publish_state(
        millis() / 1000UL,
        true,
        ipbuf,
        soil_local,
        isnan(t_local) ? 0.f : t_local,
        isnan(h_local) ? 0.f : h_local,
        g_flow_lpm,
        isnan(g_wx_temp) ? 0.f : g_wx_temp,
        isnan(g_wx_hum) ? 0.f : g_wx_hum,
        isnan(g_wx_rain_mm) ? 0.f : g_wx_rain_mm,
        isnan(g_wx_wind_ms) ? 0.f : g_wx_wind_ms,
        station_used,
        raw,
        g_crop_idx,
        g_soil_idx,
        g_crop_age_days,
        g_manual_ready,
        d.p,
        d.fallback_used,
        d.wind_blocked,
        d.irrigate,
        model_liters,
        soil_target,
        g_dose.active,
        g_dose.delivered_liters,
        g_dose.target_liters);
  }
#endif
#if ENABLE_DATASET_LOG
  {
    float wxt = isnan(g_wx_temp) ? 0.f : g_wx_temp;
    float wxh = isnan(g_wx_hum) ? 0.f : g_wx_hum;
    float wxr = isnan(g_wx_rain_mm) ? 0.f : g_wx_rain_mm;
    float wxw = isnan(g_wx_wind_ms) ? 0.f : g_wx_wind_ms;
    dataset_log_try_append(now, soil_local, raw[1], raw[2], raw[3], wind_ms, g_flow_lpm, wxt, wxh, wxr, wxw,
                           g_manual_ready, d.p, d.irrigate, model_liters, soil_target, g_dose.active,
                           g_dose.delivered_liters, g_dose.target_liters, d.wind_blocked);
  }
#endif

#if !ENABLE_WEB_DASHBOARD && !ENABLE_MQTT
  delay(5000);
#endif
}
