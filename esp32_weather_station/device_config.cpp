#include "device_config.h"
#include "weather_secrets.h"

#include <Preferences.h>
#include <WiFi.h>
#include <ArduinoJson.h>

static Preferences s_prefs;
static char s_device_id[48];
static char s_topic_telemetry[96];
static char s_topic_command[96];
static char s_topic_relay[96];
static char s_topic_prov_config[96];

static void rebuild_topics() {
  snprintf(s_topic_telemetry, sizeof(s_topic_telemetry), "irrigation/%s/telemetry", s_device_id);
  snprintf(s_topic_command, sizeof(s_topic_command), "irrigation/%s/command/manual", s_device_id);
  snprintf(s_topic_relay, sizeof(s_topic_relay), "irrigation/%s/command/relay", s_device_id);
  char mac_compact[13] = {};
  uint64_t mac = ESP.getEfuseMac();
  snprintf(mac_compact, sizeof(mac_compact), "%04X%08X", (uint16_t)(mac >> 32), (uint32_t)mac);
  snprintf(s_topic_prov_config, sizeof(s_topic_prov_config), "irrigation/provisioning/%s/config", mac_compact);
}

static void load_fallback_from_secrets() {
#ifdef DEVICE_ID_FALLBACK
  if (DEVICE_ID_FALLBACK[0]) {
    strncpy(s_device_id, DEVICE_ID_FALLBACK, sizeof(s_device_id) - 1);
    s_device_id[sizeof(s_device_id) - 1] = '\0';
  }
#endif
  if (!s_device_id[0]) {
#if defined(MQTT_TOPIC_TELEMETRY)
    const char* t = MQTT_TOPIC_TELEMETRY;
    const char* p = strstr(t, "irrigation/");
    if (p) {
      p += 11;
      const char* slash = strchr(p, '/');
      if (slash && slash > p) {
        size_t n = (size_t)(slash - p);
        if (n >= sizeof(s_device_id)) n = sizeof(s_device_id) - 1;
        memcpy(s_device_id, p, n);
        s_device_id[n] = '\0';
      }
    }
#endif
  }
  if (s_device_id[0]) rebuild_topics();
}

void device_config_begin() {
  s_device_id[0] = '\0';
  s_topic_telemetry[0] = s_topic_command[0] = s_topic_relay[0] = s_topic_prov_config[0] = '\0';
  if (!s_prefs.begin("irrigation", false)) {
    Serial.println(F("[CFG] NVS indisponible"));
    load_fallback_from_secrets();
    return;
  }
  String did = s_prefs.getString("device_id", "");
  if (did.length() > 0 && did.length() < sizeof(s_device_id)) {
    did.toCharArray(s_device_id, sizeof(s_device_id));
    rebuild_topics();
    Serial.print(F("[CFG] device_id NVS="));
    Serial.println(s_device_id);
  } else {
    load_fallback_from_secrets();
    if (s_device_id[0]) {
      Serial.print(F("[CFG] device_id secrets="));
      Serial.println(s_device_id);
    } else {
      Serial.println(F("[CFG] Pas de device_id — mode provisioning"));
    }
  }
}

bool device_config_has_device_id() { return s_device_id[0] != '\0'; }

const char* device_config_id() { return s_device_id; }

const char* device_config_topic_telemetry() { return s_topic_telemetry; }
const char* device_config_topic_command() { return s_topic_command; }
const char* device_config_topic_relay() { return s_topic_relay; }
const char* device_config_topic_prov_config() { return s_topic_prov_config; }

void device_config_mac_str(char* out, size_t len) {
  if (!out || len < 18) return;
  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(out, len, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

bool device_config_apply_set_device_json(const char* json) {
  if (!json) return false;
  StaticJsonDocument<384> doc;
  if (deserializeJson(doc, json)) return false;
  const char* cmd = doc["cmd"] | "";
  if (strcmp(cmd, "set_device_id") != 0) return false;
  const char* did = doc["device_id"] | "";
  if (!did || strlen(did) < 2) return false;
  strncpy(s_device_id, did, sizeof(s_device_id) - 1);
  s_device_id[sizeof(s_device_id) - 1] = '\0';
  rebuild_topics();
  if (s_prefs.begin("irrigation", false)) {
    s_prefs.putString("device_id", s_device_id);
    s_prefs.end();
  }
  Serial.print(F("[CFG] set_device_id OK : "));
  Serial.println(s_device_id);
  return true;
}

void device_config_factory_reset_nvs() {
  if (s_prefs.begin("irrigation", false)) {
    s_prefs.clear();
    s_prefs.end();
  }
  s_device_id[0] = '\0';
  s_topic_telemetry[0] = s_topic_command[0] = s_topic_relay[0] = '\0';
  Serial.println(F("[CFG] Reset usine NVS (WiFi + device_id effaces)"));
}

void device_config_clear_wifi_nvs() {
  if (s_prefs.begin("irrigation", false)) {
    s_prefs.remove("wifi_ssid");
    s_prefs.remove("wifi_pass");
    s_prefs.end();
  }
  Serial.println(F("[CFG] WiFi NVS efface (device_id conserve)"));
}

bool device_config_wifi_stored(char* ssid, size_t ssid_len, char* pass, size_t pass_len) {
  if (!ssid || !pass || ssid_len < 2 || pass_len < 2) return false;
  if (!s_prefs.begin("irrigation", true)) return false;
  String s = s_prefs.getString("wifi_ssid", "");
  String p = s_prefs.getString("wifi_pass", "");
  s_prefs.end();
  if (s.length() == 0) return false;
  s.toCharArray(ssid, ssid_len);
  p.toCharArray(pass, pass_len);
  return true;
}

void device_config_save_wifi(const char* ssid, const char* pass) {
  if (!ssid || !pass) return;
  if (s_prefs.begin("irrigation", false)) {
    s_prefs.putString("wifi_ssid", ssid);
    s_prefs.putString("wifi_pass", pass);
    s_prefs.end();
  }
}
