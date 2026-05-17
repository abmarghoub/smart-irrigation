#ifndef DATASET_LOG_H
#define DATASET_LOG_H

#include <Arduino.h>
#include <WebServer.h>

#ifndef ENABLE_DATASET_LOG
#define ENABLE_DATASET_LOG 1
#endif

#ifndef DATASET_LOG_INTERVAL_MS
#define DATASET_LOG_INTERVAL_MS (30000UL)
#endif

#ifndef DATASET_LOG_MAX_BYTES
#define DATASET_LOG_MAX_BYTES (280UL * 1024UL)
#endif

#if ENABLE_DATASET_LOG

// Monte LittleFS, demarre NTP (UTC), enregistre les routes /api/dataset.csv
void dataset_log_begin_fs();
void dataset_log_register_routes(WebServer& web);
void dataset_log_poll_ntp();

// Journal CSV : capteurs locaux + station meteo + sorties decision (sans saisie manuelle ni repli).
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
    bool wind_block);

#else

inline void dataset_log_begin_fs() {}
inline void dataset_log_register_routes(WebServer&) {}
inline void dataset_log_poll_ntp() {}
inline void dataset_log_try_append(
    uint32_t, float, float, float, float, float, float, float, float, float, float, bool, float, int, float, float,
    bool, float, float, bool) {}

#endif

#endif
