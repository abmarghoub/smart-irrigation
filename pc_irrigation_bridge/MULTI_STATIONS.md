# Multi-stations, auth, admin, provisioning

## Variables Render (en plus du deploiement cloud)

| Variable | Role |
|----------|------|
| `SUPABASE_URL` | URL projet Supabase |
| `SUPABASE_ANON_KEY` | Cle anon (login dashboard) |
| `SUPABASE_JWT_SECRET` | Ou JWT secret pour verifier les tokens |
| `SUPABASE_SERVICE_ROLE_KEY` | Inscription `POST /api/register-device` |
| `ADMIN_EMAILS` | Emails admin separes par virgule |
| `DEVICE_IDS` | Liste optionnelle `station01,station02` pour abonnements MQTT |
| `AUTH_DISABLED` | `1` = pas de login (dev local) |

Executer `supabase/migrations/001_multi_tenant.sql` ou laisser le pont creer les tables au demarrage.

## URLs

| URL | Role |
|-----|------|
| `/` | Dashboard client (JWT si auth active) |
| `/login` | Connexion + mot de passe oublie |
| `/admin` | Validation des demandes, liste devices |
| `/provision` | Inscription manuelle (MAC + compte) |

## Flux agriculteur (modele 2)

1. ESP : portail `Irrigation-Setup` (`ENABLE_WIFI_PROVISIONING 1`) ou flash classique.
2. Formulaire WiFi + compte → `POST /api/register-device`.
3. Admin (autre ville) : `/admin` → **Valider** → `device_id` auto + MQTT `set_device_id`.
4. ESP redemarre la config NVS et publie sur `irrigation/{device_id}/telemetry`.

## Transfert / reset

- **Reset usine** (GPIO a documenter) : `device_config_factory_reset_nvs()` — base inchangee.
- **Transferer** (dashboard) : `POST /api/device/transfer` → `status=unclaimed`.
- **Reactiver** : `POST /api/device/reactivate` avec MAC apres reset usine.

## ESP — fichiers

- `device_config.cpp` : NVS `device_id`, topics dynamiques.
- `wifi_provision.cpp` : portail AP + `BRIDGE_REGISTER_URL`.
- `weather_secrets.example.h` : `ENABLE_WIFI_PROVISIONING`, `BRIDGE_REGISTER_URL`, `DEVICE_ID_FALLBACK`.

## Tests manuels

1. Deux `device_id` → deux etats sur `/api/state?device=...`.
2. User A ne voit pas la station de B.
3. Inscription → pending → admin approve → telemetry OK.
4. CSV export filtre par `device` et date de prise de possession.
