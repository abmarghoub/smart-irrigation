#ifndef DEVICE_CONFIG_H
#define DEVICE_CONFIG_H

#include <Arduino.h>

/** Charge NVS ; construit les topics MQTT. */
void device_config_begin();

bool device_config_has_device_id();
const char* device_config_id();

const char* device_config_topic_telemetry();
const char* device_config_topic_command();
const char* device_config_topic_relay();
/** Topic provisioning : irrigation/provisioning/{mac_compact}/config */
const char* device_config_topic_prov_config();

void device_config_mac_str(char* out, size_t len);

/** JSON MQTT {"cmd":"set_device_id","device_id":"..."} */
bool device_config_apply_set_device_json(const char* json);

/** Efface WiFi + device_id (reset usine local). */
void device_config_factory_reset_nvs();

/** Mode « changer WiFi seulement » : efface SSID NVS, garde device_id. */
void device_config_clear_wifi_nvs();

bool device_config_wifi_stored(char* ssid, size_t ssid_len, char* pass, size_t pass_len);
void device_config_save_wifi(const char* ssid, const char* pass);

#endif
