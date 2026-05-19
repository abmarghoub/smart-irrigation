#ifndef WIFI_PROVISION_H
#define WIFI_PROVISION_H

/** Connexion WiFi : NVS, puis secrets.h, puis portail AP si ENABLE_WIFI_PROVISIONING. */
bool wifi_provision_connect();

/** Boucle serveur web du portail (appeler dans loop si actif). */
void wifi_provision_loop();

bool wifi_provision_portal_active();

#endif
