#include "dataset_log.h"

#if ENABLE_DATASET_LOG

#include "weather_secrets.h"
#include <LittleFS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <climits>
#include <esp_partition.h>
#include <time.h>

#ifndef DATASET_NTP_SERVER
#define DATASET_NTP_SERVER "pool.ntp.org"
#endif

static const char DATASET_LOG_PATH[] = "/irrigation_log.csv";

static WebServer* s_web = nullptr;
static bool s_fs_ok = false;
static uint32_t s_last_append_ms = 0;
static uint32_t s_last_ntp_try_ms = 0;
static bool s_ntp_started = false;

static void web_cors(WebServer& w) {
  w.sendHeader("Access-Control-Allow-Origin", "*");
}

static bool time_valid() {
  time_t t = time(nullptr);
  return t > 1700000000; // ~nov 2023
}

static void ensure_header() {
  if (!s_fs_ok) return;
  if (LittleFS.exists(DATASET_LOG_PATH)) return;
  File f = LittleFS.open(DATASET_LOG_PATH, FILE_WRITE);
  if (!f) return;
  f.print(F(
      "horodatage_unix,date_heure_utc,"
      "temperature_locale_C,humidite_air_locale_pct,pluie_modele_mm,vent_m_s,humidite_sol_pct,debit_L_min,"
      "meteo_temperature_C,meteo_humidite_pct,meteo_pluie_mm,meteo_vent_m_s,"
      "prediction_mlp_activee,probabilite_irrigation,demande_irrigation,volume_modele_litres,cible_humidite_sol_pct,"
      "vanne_ouverte,dose_livree_L,dose_cible_L,vent_bloque_arrosage\r\n"));
  f.close();
}

static void maybe_rotate() {
  if (!s_fs_ok || !LittleFS.exists(DATASET_LOG_PATH)) return;
  File f = LittleFS.open(DATASET_LOG_PATH, FILE_READ);
  if (!f) return;
  size_t sz = f.size();
  f.close();
  if (sz < DATASET_LOG_MAX_BYTES) return;
  LittleFS.remove(DATASET_LOG_PATH);
  ensure_header();
  Serial.println(F("[DATASET] Fichier log plein: rotation (fichier efface, nouvel en-tete)."));
}

#ifndef DATASET_LITTLEFS_PARTITION_ALT
#define DATASET_LITTLEFS_PARTITION_ALT "spiffs"
#endif

static bool mount_littlefs(bool formatOnFail, const char* partitionLabel) {
  if (partitionLabel == nullptr) return LittleFS.begin(formatOnFail);
  return LittleFS.begin(formatOnFail, "/littlefs", 10, partitionLabel);
}

/** Efface toute la partition DATA nommee (secteurs flash). Utile quand LittleFS renvoie -84 sans pouvoir monter. */
static bool erase_labeled_data_partition(const char* label) {
  const esp_partition_t* p =
      esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, label);
  if (!p) return false;
  Serial.printf("[DATASET] Effacement physique partition \"%s\" (%u octets)...\n", label, (unsigned)p->size);
  esp_err_t e = esp_partition_erase_range(p, 0, p->size);
  if (e != ESP_OK) {
    Serial.printf("[DATASET] esp_partition_erase_range -> %d\n", (int)e);
    return false;
  }
  Serial.println(F("[DATASET] Effacement physique termine."));
  return true;
}

static bool erase_any_dataset_partition() {
  static const char kNames[][10] = {"spiffs", "littlefs", "storage"};
  for (unsigned i = 0; i < sizeof(kNames) / sizeof(kNames[0]); i++) {
    if (erase_labeled_data_partition(kNames[i])) return true;
  }
  const esp_partition_t* p =
      esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_LITTLEFS, nullptr);
  if (!p) p = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, nullptr);
  if (p && p->label[0] != '\0') return erase_labeled_data_partition(p->label);
  Serial.println(F("[DATASET] Aucune partition data reconnue pour effacement physique."));
  return false;
}

static bool try_labels(bool formatOnFail, bool print_alt_hint) {
  const char* labels[] = {nullptr, DATASET_LITTLEFS_PARTITION_ALT};
  for (int attempt = 0; attempt < 2; attempt++) {
    if (attempt > 0) LittleFS.end();
    if (mount_littlefs(formatOnFail, labels[attempt])) return true;
    if (print_alt_hint && !formatOnFail && attempt == 0)
      Serial.println(F("[DATASET] Essai label partition alternatif (spiffs)..."));
  }
  return false;
}

void dataset_log_begin_fs() {
  Serial.println(F("[DATASET] Init stockage v4 (LittleFS + effacement physique si -84)..."));
  bool did_wipe = false;
  LittleFS.end();

  s_fs_ok = try_labels(false, true);
  if (!s_fs_ok) {
    Serial.println(
        F("[DATASET] Montage impossible (ex. -84) — effacement brut de la partition data, puis remontage..."));
    LittleFS.end();
    if (erase_any_dataset_partition()) did_wipe = true;
    LittleFS.end();
    s_fs_ok = try_labels(false, false);
  }
  if (!s_fs_ok) {
    Serial.println(F("[DATASET] Tentative format logiciel (begin true)..."));
    LittleFS.end();
    s_fs_ok = try_labels(true, false);
    if (s_fs_ok) did_wipe = true;
  }

  if (!s_fs_ok) {
    Serial.println(F("[DATASET] LittleFS toujours indisponible. Verifiez:"));
    Serial.println(F("  - Outils -> Partition Scheme avec partition DATA (ex. \"Default 4MB with spiffs\")."));
    Serial.println(F("  - Erase All Flash Before Sketch Upload: Enabled (1 seul upload) puis Disabled."));
    return;
  }
  if (did_wipe) {
    Serial.println(F("[DATASET] Zone data reinitialisee (effacement flash et/ou format LittleFS)."));
  }
  ensure_header();
  Serial.println(F("[DATASET] Journal CSV sur LittleFS: /irrigation_log.csv"));
}

void dataset_log_poll_ntp() {
  if (WiFi.status() != WL_CONNECTED) return;
  uint32_t ms = millis();
  if (s_ntp_started) {
    if (time_valid()) return;
    if (ms - s_last_ntp_try_ms < 60000UL) return;
  }
  s_last_ntp_try_ms = ms;
  s_ntp_started = true;
  setenv("TZ", "UTC0", 1);
  tzset();
  configTime(0, 0, DATASET_NTP_SERVER, "time.nist.gov");
  Serial.println(F("[DATASET] NTP synchronisation demandee (UTC)."));
}

static void append_csv_line(const char* line) {
  maybe_rotate();
  if (!s_fs_ok) return;
  ensure_header();
  File f = LittleFS.open(DATASET_LOG_PATH, FILE_APPEND);
  if (!f) return;
  f.print(line);
  f.close();
}

static void fmt_iso_utc(char* buf, size_t buflen, time_t t) {
  if (!buf || buflen < 22) return;
  struct tm tmv;
  if (gmtime_r(&t, &tmv) == nullptr) {
    snprintf(buf, buflen, "invalid");
    return;
  }
  strftime(buf, buflen, "%Y-%m-%dT%H:%M:%SZ", &tmv);
}

void dataset_log_try_append(
    uint32_t now_ms,
    float soil_pct,
    float temp_c,
    float rh_pct,
    float rain_mm,
    float wind_ms,
    float flow_lpm,
    float wx_temp,
    float wx_rh,
    float wx_rain,
    float wx_wind,
    bool manual_ok,
    float p,
    int irrigate,
    float volume_model_l,
    float soil_target_pct,
    bool valve_on,
    float dose_delivered_l,
    float dose_target_l,
    bool wind_block) {
  if (!s_fs_ok) return;
  if (now_ms - s_last_append_ms < DATASET_LOG_INTERVAL_MS) return;
  s_last_append_ms = now_ms;

  time_t ts = time(nullptr);
  if (!time_valid()) ts = 0;

  char iso[28];
  if (ts > 0)
    fmt_iso_utc(iso, sizeof(iso), ts);
  else
    snprintf(iso, sizeof(iso), "NO_NTP");

  char line[512];
  int n = snprintf(
      line, sizeof(line),
      "%lld,%s,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%d,%.5f,%d,%.4f,%.3f,%d,%.4f,%.4f,%d\r\n",
      (long long)ts,
      iso,
      (double)temp_c,
      (double)rh_pct,
      (double)rain_mm,
      (double)wind_ms,
      (double)soil_pct,
      (double)flow_lpm,
      (double)wx_temp,
      (double)wx_rh,
      (double)wx_rain,
      (double)wx_wind,
      manual_ok ? 1 : 0,
      (double)p,
      irrigate ? 1 : 0,
      (double)volume_model_l,
      (double)soil_target_pct,
      valve_on ? 1 : 0,
      (double)dose_delivered_l,
      (double)dose_target_l,
      wind_block ? 1 : 0);
  if (n > 0 && (size_t)n < sizeof(line)) append_csv_line(line);
}

static bool parse_first_field_uint64(const char* line, unsigned long long* out) {
  if (!line || !out) return false;
  char buf[24];
  size_t i = 0;
  while (line[i] && line[i] != ',' && i < sizeof(buf) - 1) {
    buf[i] = line[i];
    i++;
  }
  buf[i] = '\0';
  if (i == 0) return false;
  *out = strtoull(buf, nullptr, 10);
  return true;
}

static void handle_dataset_csv() {
  if (!s_web) return;
  web_cors(*s_web);
  if (!s_fs_ok || !LittleFS.exists(DATASET_LOG_PATH)) {
    s_web->send(404, "text/plain; charset=utf-8", "Aucun journal (LittleFS non monte ou fichier absent).");
    return;
  }

  unsigned long long t0 = 0;
  unsigned long long t1 = ULLONG_MAX;
  bool has_t0 = s_web->hasArg("start") && s_web->arg("start").length() > 0;
  bool has_t1 = s_web->hasArg("end") && s_web->arg("end").length() > 0;
  if (has_t0) t0 = strtoull(s_web->arg("start").c_str(), nullptr, 10);
  if (has_t1) t1 = strtoull(s_web->arg("end").c_str(), nullptr, 10);

  File f = LittleFS.open(DATASET_LOG_PATH, FILE_READ);
  if (!f) {
    s_web->send(500, "text/plain; charset=utf-8", "Lecture impossible.");
    return;
  }

  if (!has_t0 && !has_t1 && f.size() < (400UL * 1024UL)) {
    s_web->sendHeader("Content-Disposition", "attachment; filename=\"irrigation_log.csv\"");
    s_web->sendHeader("Cache-Control", "no-store");
    s_web->streamFile(f, "text/csv; charset=utf-8");
    return;
  }

  s_web->setContentLength(CONTENT_LENGTH_UNKNOWN);
  s_web->sendHeader("Content-Disposition", "attachment; filename=\"irrigation_log.csv\"");
  s_web->sendHeader("Cache-Control", "no-store");
  s_web->send(200, "text/csv; charset=utf-8", "");

  String header = f.readStringUntil('\n');
  header.trim();
  if (header.length() > 0) {
    s_web->sendContent(header.c_str());
    s_web->sendContent("\r\n");
  }

  while (f.available()) {
    String row = f.readStringUntil('\n');
    row.trim();
    if (row.length() == 0) continue;
    if (row.startsWith("horodatage_unix") || row.startsWith("ts_unix")) continue;
    unsigned long long ts = 0;
    if (!parse_first_field_uint64(row.c_str(), &ts)) {
      s_web->sendContent(row.c_str());
      s_web->sendContent("\r\n");
      continue;
    }
    if (has_t0 && ts < t0) continue;
    if (has_t1 && ts > t1) continue;
    s_web->sendContent(row.c_str());
    s_web->sendContent("\r\n");
  }
  f.close();
  s_web->sendContent("");
}

void dataset_log_register_routes(WebServer& web) {
  s_web = &web;
  web.on("/api/dataset.csv", HTTP_GET, handle_dataset_csv);
}

#endif
