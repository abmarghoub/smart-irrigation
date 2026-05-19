#include "wifi_provision.h"
#include "device_config.h"
#include "weather_secrets.h"

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

#ifndef WIFI_CONFIG_AP_NAME
#define WIFI_CONFIG_AP_NAME "Irrigation-Setup"
#endif

#ifndef ENABLE_WIFI_PROVISIONING
#define ENABLE_WIFI_PROVISIONING 0
#endif

static WebServer s_server(80);
static bool s_portal_active = false;

static const char* kPortalHtml = R"HTML(
<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Configuration station</title>
<style>body{font-family:system-ui,sans-serif;margin:1rem;max-width:420px}label{display:block;margin:.5rem 0 .2rem}input{width:100%;padding:.5rem}button{margin-top:1rem;padding:.6rem 1rem;width:100%}</style>
</head><body>
<h1>Station irrigation</h1>
<p>MAC : <strong id="mac">—</strong></p>
<form method="POST" action="/save">
<label>WiFi maison (SSID)</label><input name="wifi_ssid" required/>
<label>Mot de passe WiFi</label><input name="wifi_pass" type="password" required/>
<label>Prenom</label><input name="first_name" required/>
<label>Nom</label><input name="last_name" required/>
<label>Email (compte)</label><input name="email" type="email" required/>
<label>Mot de passe compte (6+)</label><input name="password" type="password" minlength="6" required/>
<button type="submit">Enregistrer</button>
</form>
<p><small>Apres envoi : connexion au WiFi puis inscription cloud. Validez sur le dashboard admin.</small></p>
</body></html>
)HTML";

static bool wifi_try_sta(const char* ssid, const char* pass, uint32_t timeout_ms) {
  if (!ssid || !ssid[0]) return false;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, pass);
  Serial.print(F("[WiFi] Connexion "));
  Serial.println(ssid);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeout_ms) delay(300);
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print(F("[WiFi] OK IP="));
    Serial.println(WiFi.localIP());
    return true;
  }
  return false;
}

static bool wifi_post_register(const char* email, const char* password, const char* first, const char* last) {
#if defined(BRIDGE_REGISTER_URL) && defined(ENABLE_WIFI_PROVISIONING) && ENABLE_WIFI_PROVISIONING
  if (strlen(BRIDGE_REGISTER_URL) < 12) return false;
  char mac[20];
  device_config_mac_str(mac, sizeof(mac));
  StaticJsonDocument<512> doc;
  doc["mac"] = mac;
  doc["email"] = email;
  doc["password"] = password;
  doc["first_name"] = first;
  doc["last_name"] = last;
  String body;
  serializeJson(doc, body);
  HTTPClient http;
  http.begin(BRIDGE_REGISTER_URL);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  Serial.print(F("[WiFi] register-device HTTP "));
  Serial.println(code);
  http.end();
  return code >= 200 && code < 300;
#else
  (void)email;
  (void)password;
  (void)first;
  (void)last;
  return false;
#endif
}

static void handle_root() {
  String page = kPortalHtml;
  char mac[20];
  device_config_mac_str(mac, sizeof(mac));
  page.replace("—", mac);
  s_server.send(200, "text/html; charset=utf-8", page);
}

static void handle_save() {
  String ssid = s_server.arg("wifi_ssid");
  String pass = s_server.arg("wifi_pass");
  String email = s_server.arg("email");
  String pwd = s_server.arg("password");
  String fn = s_server.arg("first_name");
  String ln = s_server.arg("last_name");
  if (ssid.length() == 0 || email.length() == 0 || pwd.length() < 6) {
    s_server.send(400, "text/plain", "Champs incomplets");
    return;
  }
  device_config_save_wifi(ssid.c_str(), pass.c_str());
  s_server.send(200, "text/html; charset=utf-8",
                 "<p>Enregistrement recu. Connexion au WiFi...</p><p>Redemarrez si bloque.</p>");
  s_portal_active = false;
  s_server.stop();
  WiFi.softAPdisconnect(true);
  if (wifi_try_sta(ssid.c_str(), pass.c_str(), 20000)) {
    wifi_post_register(email.c_str(), pwd.c_str(), fn.c_str(), ln.c_str());
  }
}

static void start_portal() {
  Serial.println(F("[WiFi] Portail Irrigation-Setup (192.168.4.1)"));
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(WIFI_CONFIG_AP_NAME);
  delay(300);
  s_server.on("/", HTTP_GET, handle_root);
  s_server.on("/save", HTTP_POST, handle_save);
  s_server.begin();
  s_portal_active = true;
}

bool wifi_provision_portal_active() { return s_portal_active; }

void wifi_provision_loop() {
  if (s_portal_active) s_server.handleClient();
}

bool wifi_provision_connect() {
  char ssid[64] = {};
  char pass[64] = {};
  if (device_config_wifi_stored(ssid, sizeof(ssid), pass, sizeof(pass))) {
    if (wifi_try_sta(ssid, pass, 15000)) return true;
  }
#if defined(WIFI_SSID) && defined(WIFI_PASSWORD)
  if (strlen(WIFI_SSID) > 0) {
    if (wifi_try_sta(WIFI_SSID, WIFI_PASSWORD, 15000)) return true;
  }
#endif
#if ENABLE_WIFI_PROVISIONING
  start_portal();
  return false;
#else
  Serial.println(F("[WiFi] Echec connexion (provisioning desactive)"));
  return false;
#endif
}
