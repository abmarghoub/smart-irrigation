# Deploiement cloud (gratuit) — HiveMQ + Render + Supabase

## 1. Variables Render (Environment)

| Variable | Exemple |
|----------|---------|
| `DATABASE_URL` | URI **Transaction pooler** copiee depuis Supabase **Connect** (port **6543**, user `postgres.hofilrtzkkpexdxshkoo`) |
| `SUPABASE_POOLER_HOST` | (Option) Hote pooler exact si vous gardez l'URI directe `db.xxx:5432` — ex. `aws-1-eu-central-1.pooler.supabase.com` |
| `SUPABASE_POOLER_PORT` | (Option) `6543` (Transaction) ou `5432` (Session) |
| `MQTT_HOST` | `d5d4693246d54f46a43cefa118dea176.s1.eu.hivemq.cloud` |
| `MQTT_PORT` | `8883` |
| `MQTT_USER` | `irrigation_station01` |
| `MQTT_PASSWORD` | *(mot de passe HiveMQ — **identique** à `weather_secrets.h`, caractère `#` tel quel, pas `%23`)* |
| `MQTT_TOPIC_TELEMETRY` | `irrigation/station01/telemetry` |
| `MQTT_TOPIC_COMMAND` | `irrigation/station01/command/manual` |
| `DEVICE_ID` | `station01` |

Mot de passe Supabase avec `#` → encoder en `%23` dans l'URI.

## 2. Render — Web Service

- **Root Directory** : `pc_irrigation_bridge`
- **Build** : `pip install -r requirements.txt`
- **Start** : `gunicorn bridge:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`

Ou lier le depot et utiliser `render.yaml` a la racine.

## 3. Git push

```powershell
cd "C:\Users\LENEVO\Desktop\11master pf"
git add .
git status
git commit -m "Cloud: HiveMQ TLS, Supabase, Render gunicorn"
git push
```

Render redéploie automatiquement.

## 4. ESP32 — weather_secrets.h (local, non commite)

1. Copier `weather_secrets.example.h` → `weather_secrets.h` si besoin.
2. Renseigner `WIFI_SSID`, `WIFI_PASSWORD`, `MQTT_PASSWORD` (HiveMQ).
3. Verifier host, port `8883`, topics `irrigation/station01/...`.
4. Flasher l'ESP (Arduino IDE).

Moniteur serie attendu : `WiFi OK`, `[MQTT] Connexion ... (TLS)`, pas d'echec `rc=-2`.

## 5. Tests

1. `https://VOTRE-APP.onrender.com/api/health` → `"postgres": true`, `"mqtt": true`
2. Dashboard `/` → donnees capteurs apres ~30 s
3. `/api/irrigation_log.csv` → export CSV (filtres : `?crop=Tomato&from=2026-01-01&to=2026-05-31`)
4. Seules les lignes **completes** (saisie manuelle + MLP, toutes colonnes) sont inserees en base
4. Telephone en **4G** (WiFi coupe) → meme URL

Premier acces apres inactivite : Render free peut mettre ~1 min a demarrer.

## 6. Depannage

| Probleme | Piste |
|----------|--------|
| MQTT `rc=-2` (ESP) | WiFi sans Internet, mauvais host/port, ou auth |
| `postgres: false` | `DATABASE_URL` invalide (encoder `#` en `%23`) |
| Dashboard vide | ESP non connecte ou topics differents Render/ESP |
| ESP `Heartbeat OK` mais `mqtt_rx_count: 0` | HiveMQ : **Subscribe** sur `irrigation/station01/telemetry` ; `git push` bridge ; health → `mqtt_loopback_ok: true` |
| Dashboard « OK » mais ESP sans `[MQTT] RX` | HiveMQ : **Publish** sur `irrigation/station01/command/relay` et `.../command/manual` ; bridge utilise QoS 1 (PUBACK) |
| CSV vide | Attendre des messages MQTT avec saisie manuelle confirmee |
