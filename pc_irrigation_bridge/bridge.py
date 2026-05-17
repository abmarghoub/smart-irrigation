"""
Pont PC : souscrit aux messages MQTT de l'ESP32, enregistre le CSV sur disque,
optionnellement MySQL, sert le dashboard local (Flask) et relaie la saisie manuelle vers l'ESP32.

Le journal CSV ne contient que :
  - saisie manuelle (culture, sol, age) ;
  - grandeurs mesurees (temperature, humidite air, pluie station, vent, humidite sol), valeurs avec 2 decimales ;
  - sorties du modele (p_fraction, irrigate, irrigation_litres) — volume en litres, 2 decimales.

MySQL : meme jeu de champs (+ id, recorded_at). La base indiquee est creee automatiquement si le compte a le droit CREATE.
Variables d'environnement MYSQL_* ou options en ligne de commande.

Prérequis : broker MQTT (ex. Mosquitto) sur le PC ou le LAN.

Usage :
  cd pc_irrigation_bridge
  pip install -r requirements.txt
  python bridge.py --mqtt-host 127.0.0.1 --http-port 8765
  python bridge.py --reset-csv
  python bridge.py --no-mysql   # CSV uniquement
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_file
import paho.mqtt.client as mqtt

from db_mysql import MySQLStore, TelemetryData, extract_telemetry_from_mqtt, telemetry_to_csv_row

# Colonnes utiles uniquement (pas de champs sans capteur / sans saisie / sans inference).
CSV_HEADER = [
    "crop_name",
    "soil_type",
    "crop_age_days",
    "temperature_C",
    "humidity_%",
    "rainfall_mm",
    "wind_speed_m_s",
    "soil_moisture_%",
    "p_fraction",
    "irrigate",
    "irrigation_litres",
]

app = Flask(__name__)

_state_lock = threading.Lock()
_last_state: dict[str, Any] = {}

_mqtt_client: mqtt.Client | None = None
_args_ns: argparse.Namespace | None = None
_mysql_store: MySQLStore | None = None


def _write_header_only(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(CSV_HEADER)


def _backup_and_remove(path: Path) -> None:
    if not path.is_file():
        return
    bak = path.with_name(f"{path.name}.bak.{int(time.time())}")
    path.rename(bak)
    print(f"[bridge] Ancien CSV deplace vers : {bak}")


def _ensure_csv_schema(path: Path, *, force_reset: bool, auto_fix_header: bool) -> None:
    """Cree le fichier avec l'en-tete reduit ; reset ou corrige si mauvais en-tete."""
    if force_reset:
        _backup_and_remove(path)
        _write_header_only(path)
        print("[bridge] CSV reinitialise (--reset-csv), en-tete journal reduit.")
        return

    if not path.is_file() or path.stat().st_size == 0:
        _write_header_only(path)
        return

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        first = next(csv.reader(f), None)
    if not first:
        _write_header_only(path)
        return

    if [c.strip() for c in first] == CSV_HEADER:
        return

    if auto_fix_header:
        _backup_and_remove(path)
        _write_header_only(path)
        print("[bridge] En-tete CSV obsolete : fichier sauvegarde et reinitialise (colonnes reduites).")
        return

    raise ValueError(
        f"En-tete CSV inattendu dans {path}. Lancez avec --reset-csv ou supprimez le fichier."
    )


def _append_csv_row(path: Path, row: TelemetryData) -> None:
    _ensure_csv_schema(path, force_reset=False, auto_fix_header=True)
    line = telemetry_to_csv_row(row, CSV_HEADER)
    if len(line) != len(CSV_HEADER):
        raise RuntimeError("internal: row/columns mismatch")
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(line)


def _on_mqtt_message(_client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
    global _last_state
    if not _args_ns:
        return
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return

    with _state_lock:
        _last_state = payload

    row = extract_telemetry_from_mqtt(payload)

    try:
        _append_csv_row(Path(_args_ns.csv_path), row)
    except OSError as e:
        print("[bridge] ecriture CSV:", e)

    if _mysql_store is not None:
        try:
            _mysql_store.insert(row)
        except Exception as e:
            print("[bridge] MySQL insert:", e)


def _start_mqtt(ns: argparse.Namespace) -> mqtt.Client:
    global _mqtt_client, _args_ns
    _args_ns = ns

    def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc == 0:
            client.subscribe(ns.topic_telemetry, qos=0)
            print(f"[MQTT] connecte, abonne a {ns.topic_telemetry!r}")
        else:
            print(f"[MQTT] connexion refusee, code {rc}")

    cid = ns.mqtt_client_id + "_bridge"
    client = mqtt.Client(client_id=cid)
    client.on_connect = on_connect
    client.on_message = _on_mqtt_message
    if ns.mqtt_user:
        client.username_pw_set(ns.mqtt_user, ns.mqtt_password or None)
    client.connect(ns.mqtt_host, ns.mqtt_port, keepalive=30)
    client.loop_start()
    _mqtt_client = client
    return client


@app.route("/api/irrigation_log.csv", methods=["GET"])
def api_irrigation_log_csv() -> Any:
    """Telechargement du journal CSV (chemin configure par --csv-path)."""
    if not _args_ns:
        return jsonify({"error": "Pont non initialise"}), 503
    path = Path(_args_ns.csv_path).resolve()
    if not path.is_file():
        return jsonify({"error": "Fichier CSV introuvable"}), 404
    return send_file(
        path,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="irrigation_log.csv",
    )


@app.route("/")
def index() -> Response:
    html_path = Path(__file__).resolve().parent / "dashboard.html"
    return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")


@app.route("/api/state", methods=["GET"])
def api_state() -> Any:
    with _state_lock:
        if not _last_state:
            return jsonify(
                {
                    "wifi_connected": False,
                    "ip": "",
                    "uptime_s": 0,
                    "sensors": {},
                    "weather": {},
                    "model_inputs": {},
                    "decision": {"prediction_active": False},
                    "manual": {"confirmed": False},
                    "note": "En attente du premier message MQTT (telemetrie).",
                }
            )
        return jsonify(_last_state)


@app.route("/api/manual", methods=["POST"])
def api_manual() -> Any:
    if not _mqtt_client or not _args_ns:
        return jsonify({"ok": False, "error": "MQTT non initialise"}), 503
    age = int(request.form.get("crop_age_days", 0))
    ci = int(request.form.get("crop_idx", 0))
    si = int(request.form.get("soil_idx", 0))
    body = json.dumps({"crop_age_days": age, "crop_idx": ci, "soil_idx": si})
    ok = _mqtt_client.publish(_args_ns.topic_command, body, qos=1).rc == 0
    return jsonify({"ok": bool(ok)})


def main() -> None:
    global _mysql_store

    p = argparse.ArgumentParser(description="Dashboard local + journal CSV via MQTT (+ MySQL optionnel)")
    p.add_argument("--mqtt-host", default="127.0.0.1", help="Adresse du broker (souvent le PC local)")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--mqtt-user", default="", help="Optionnel")
    p.add_argument("--mqtt-password", default="", help="Optionnel")
    p.add_argument("--mqtt-client-id", default="irrigation_pc_bridge")
    p.add_argument(
        "--topic-telemetry",
        default="irrigation/station/telemetry",
        help="Doit correspondre a MQTT_TOPIC_TELEMETRY sur l'ESP32",
    )
    p.add_argument(
        "--topic-command",
        default="irrigation/command/manual",
        help="Doit correspondre a MQTT_TOPIC_COMMAND sur l'ESP32",
    )
    p.add_argument("--csv-path", default="data/irrigation_log.csv", help="Fichier CSV sur le PC")
    p.add_argument(
        "--reset-csv",
        action="store_true",
        help="Sauvegarde le journal actuel (.bak.<timestamp>) et repart avec l'en-tete reduit (capteurs + manuel + modele)",
    )
    p.add_argument("--http-host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8765)

    p.add_argument(
        "--no-mysql",
        action="store_true",
        help="Ne pas ecrire en base MySQL (CSV seul)",
    )
    p.add_argument("--mysql-host", default=os.environ.get("MYSQL_HOST", "127.0.0.1"))
    p.add_argument("--mysql-port", type=int, default=int(os.environ.get("MYSQL_PORT", "3306")))
    p.add_argument("--mysql-user", default=os.environ.get("MYSQL_USER", "root"))
    p.add_argument("--mysql-password", default=os.environ.get("MYSQL_PASSWORD", ""))
    p.add_argument("--mysql-database", default=os.environ.get("MYSQL_DATABASE", "irrigation"))
    p.add_argument(
        "--mysql-table",
        default=os.environ.get("MYSQL_TABLE", "irrigation_telemetry"),
        help="Nom de table (lettres, chiffres, underscore)",
    )

    ns = p.parse_args()

    csv_path = Path(ns.csv_path).resolve()
    print(f"[bridge] CSV : {csv_path}")
    _ensure_csv_schema(csv_path, force_reset=ns.reset_csv, auto_fix_header=not ns.reset_csv)

    _mysql_store = None
    if not ns.no_mysql:
        try:
            _mysql_store = MySQLStore(
                host=ns.mysql_host,
                port=ns.mysql_port,
                user=ns.mysql_user,
                password=ns.mysql_password,
                database=ns.mysql_database,
                table=ns.mysql_table,
            )
            _mysql_store.ensure_table()
            print(f"[bridge] MySQL pret (base + table) : `{ns.mysql_database}`.`{ns.mysql_table}`")
        except Exception as e:
            print(f"[bridge] MySQL desactive (erreur connexion ou DDL): {e}")
            _mysql_store = None

    _start_mqtt(ns)
    print(f"[bridge] Dashboard : http://{ns.http_host}:{ns.http_port}/")
    app.run(host=ns.http_host, port=ns.http_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
