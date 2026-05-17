"""
Pont : MQTT (HiveMQ Cloud) -> Supabase/Postgres + export CSV, dashboard Flask.

Variables d'environnement (Render) :
  DATABASE_URL, MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD,
  MQTT_TOPIC_TELEMETRY, MQTT_TOPIC_COMMAND, DEVICE_ID, PORT
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
import paho.mqtt.client as mqtt

from db_mysql import MySQLStore, TelemetryData, extract_telemetry_from_mqtt, telemetry_to_csv_row
from db_postgres import PostgresStore, postgres_store_from_env

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
_pg_store: PostgresStore | None = None
_mysql_store: MySQLStore | None = None
_bootstrap_lock = threading.Lock()
_bootstrapped = False


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key, "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _default_topics(device_id: str) -> tuple[str, str]:
    d = device_id or "station01"
    return (f"irrigation/{d}/telemetry", f"irrigation/{d}/command/manual")


def build_namespace(argv: list[str] | None = None) -> argparse.Namespace:
    device_id = _env("DEVICE_ID", "station01")
    tel_def, cmd_def = _default_topics(device_id)
    mqtt_port = _env_int("MQTT_PORT", 8883)
    mqtt_tls = _env_bool("MQTT_TLS", mqtt_port == 8883)

    p = argparse.ArgumentParser(description="Dashboard irrigation via MQTT + Postgres/CSV")
    p.add_argument("--mqtt-host", default=_env("MQTT_HOST") or _env("MQTT_BROKER_HOST") or "127.0.0.1")
    p.add_argument("--mqtt-port", type=int, default=mqtt_port)
    p.add_argument("--mqtt-user", default=_env("MQTT_USER"))
    p.add_argument("--mqtt-password", default=_env("MQTT_PASSWORD"))
    p.add_argument("--mqtt-client-id", default=_env("MQTT_CLIENT_ID", "irrigation_pc_bridge"))
    p.add_argument("--mqtt-tls", action=argparse.BooleanOptionalAction, default=mqtt_tls)
    p.add_argument("--topic-telemetry", default=_env("MQTT_TOPIC_TELEMETRY", tel_def))
    p.add_argument("--topic-command", default=_env("MQTT_TOPIC_COMMAND", cmd_def))
    p.add_argument("--device-id", default=device_id)
    p.add_argument("--csv-path", default=_env("CSV_PATH", "data/irrigation_log.csv"))
    p.add_argument("--reset-csv", action="store_true")
    p.add_argument("--http-host", default=_env("HTTP_HOST", "0.0.0.0"))
    p.add_argument("--http-port", type=int, default=_env_int("PORT", _env_int("HTTP_PORT", 8765)))
    p.add_argument("--no-mysql", action="store_true", default=bool(_env("DATABASE_URL")))
    p.add_argument("--no-postgres", action="store_true")
    p.add_argument("--pg-table", default=_env("PG_TABLE", "irrigation_telemetry"))
    p.add_argument("--mysql-host", default=_env("MYSQL_HOST", "127.0.0.1"))
    p.add_argument("--mysql-port", type=int, default=_env_int("MYSQL_PORT", 3306))
    p.add_argument("--mysql-user", default=_env("MYSQL_USER", "root"))
    p.add_argument("--mysql-password", default=_env("MYSQL_PASSWORD", ""))
    p.add_argument("--mysql-database", default=_env("MYSQL_DATABASE", "irrigation"))
    p.add_argument("--mysql-table", default=_env("MYSQL_TABLE", "irrigation_telemetry"))
    return p.parse_args(argv if argv is not None else [])


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
    if force_reset:
        _backup_and_remove(path)
        _write_header_only(path)
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
        return
    raise ValueError(f"En-tete CSV inattendu dans {path}")


def _append_csv_row(path: Path, row: TelemetryData) -> None:
    _ensure_csv_schema(path, force_reset=False, auto_fix_header=True)
    line = telemetry_to_csv_row(row, CSV_HEADER)
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(line)


def _csv_response_from_rows(rows: list[list[Any]]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for line in rows:
        w.writerow(line)
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=irrigation_log.csv"},
    )


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

    if _pg_store is not None:
        try:
            _pg_store.insert(row)
        except Exception as e:
            print("[bridge] Postgres insert:", e)

    if _mysql_store is not None:
        try:
            _mysql_store.insert(row)
        except Exception as e:
            print("[bridge] MySQL insert:", e)

    try:
        _append_csv_row(Path(_args_ns.csv_path), row)
    except OSError as e:
        print("[bridge] ecriture CSV locale:", e)


def _start_mqtt(ns: argparse.Namespace) -> mqtt.Client:
    global _mqtt_client, _args_ns
    _args_ns = ns

    def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc == 0:
            client.subscribe(ns.topic_telemetry, qos=0)
            print(f"[MQTT] connecte, abonne a {ns.topic_telemetry!r}")
        else:
            print(f"[MQTT] connexion refusee, code {rc}")

    cid = f"{ns.mqtt_client_id}_{int(time.time()) % 100000}"
    client = mqtt.Client(client_id=cid)
    client.on_connect = on_connect
    client.on_message = _on_mqtt_message
    if ns.mqtt_user:
        client.username_pw_set(ns.mqtt_user, ns.mqtt_password or None)
    if ns.mqtt_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.connect(ns.mqtt_host, ns.mqtt_port, keepalive=30)
    client.loop_start()
    _mqtt_client = client
    return client


def _init_storage(ns: argparse.Namespace) -> None:
    global _pg_store, _mysql_store

    _pg_store = None
    if not ns.no_postgres:
        try:
            _pg_store = postgres_store_from_env()
            if _pg_store is not None:
                _pg_store.ensure_table()
                print(f"[bridge] Postgres pret : table `{ns.pg_table}` device={ns.device_id!r}")
        except Exception as e:
            print("[bridge] Postgres desactive:", e)
            _pg_store = None

    _mysql_store = None
    if not ns.no_mysql and _env("MYSQL_HOST"):
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
            print(f"[bridge] MySQL pret : `{ns.mysql_database}`.`{ns.mysql_table}`")
        except Exception as e:
            print("[bridge] MySQL desactive:", e)
            _mysql_store = None

    if not _env("DATABASE_URL"):
        csv_path = Path(ns.csv_path).resolve()
        print(f"[bridge] CSV local : {csv_path}")
        _ensure_csv_schema(csv_path, force_reset=ns.reset_csv, auto_fix_header=not ns.reset_csv)


def bootstrap(argv: list[str] | None = None) -> argparse.Namespace:
    global _bootstrapped
    with _bootstrap_lock:
        if _bootstrapped and _args_ns is not None:
            return _args_ns
        ns = build_namespace(argv)
        _init_storage(ns)
        _start_mqtt(ns)
        _bootstrapped = True
        return ns


@app.route("/api/irrigation_log.csv", methods=["GET"])
def api_irrigation_log_csv() -> Any:
    if _pg_store is not None:
        try:
            rows = _pg_store.fetch_all_csv_rows(CSV_HEADER)
            return _csv_response_from_rows(rows)
        except Exception as e:
            return jsonify({"error": f"Lecture base: {e}"}), 500

    if not _args_ns:
        return jsonify({"error": "Pont non initialise"}), 503
    path = Path(_args_ns.csv_path).resolve()
    if not path.is_file():
        return jsonify({"error": "Fichier CSV introuvable"}), 404
    return Response(
        path.read_text(encoding="utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=irrigation_log.csv"},
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


@app.route("/api/health", methods=["GET"])
def api_health() -> Any:
    mqtt_ok = False
    if _mqtt_client is not None:
        try:
            mqtt_ok = bool(_mqtt_client.is_connected())
        except Exception:
            mqtt_ok = False
    return jsonify({"ok": True, "mqtt": mqtt_ok, "postgres": _pg_store is not None})


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
    ns = bootstrap(sys.argv[1:])
    print(f"[bridge] Dashboard : http://{ns.http_host}:{ns.http_port}/")
    print(f"[bridge] MQTT {ns.mqtt_host}:{ns.mqtt_port} tls={ns.mqtt_tls}")
    app.run(host=ns.http_host, port=ns.http_port, debug=False, threaded=True)


# Gunicorn (Render) importe ce module : ne pas lire sys.argv (contient "bridge:app", --bind, etc.)
if __name__ != "__main__":
    if _env("MQTT_HOST") or _env("MQTT_BROKER_HOST") or _env("DATABASE_URL"):
        try:
            bootstrap([])
        except Exception as e:
            print("[bridge] bootstrap au chargement:", e)


if __name__ == "__main__":
    main()
