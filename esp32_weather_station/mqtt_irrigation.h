#ifndef MQTT_IRRIGATION_H
#define MQTT_IRRIGATION_H

#include <Arduino.h>
#include "weather_secrets.h"

#if ENABLE_MQTT

void mqtt_irrigation_begin();
void mqtt_irrigation_loop();
/** Test immediat publish telemetry (commande serie M). */
void mqtt_irrigation_test_publish();
/** Force le prochain tour capteurs/MQTT (apres commande manuelle distante). */
void irrigation_request_sensor_cycle();
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
    float dose_t);

#else

inline void mqtt_irrigation_begin() {}
inline void mqtt_irrigation_loop() {}
inline void mqtt_irrigation_test_publish() {}
inline void irrigation_request_sensor_cycle() {}
inline void mqtt_irrigation_publish_state(
    uint32_t, bool, const char*, float, float, float, float, float, float, float, float, bool, const float*, int, int, int,
    bool, float, bool, bool, int, float, float, bool, float, float) {}

#endif

#endif
