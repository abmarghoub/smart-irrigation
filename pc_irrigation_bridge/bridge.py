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
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
import paho.mqtt.client as mqtt

from db_mysql import (
    VALID_CROP_NAMES,
    MySQLStore,
    TelemetryData,
    extract_telemetry_from_mqtt,
    is_row_complete_for_db,
    telemetry_to_csv_row,
)
from db_postgres import PostgresStore, postgres_store_from_env
from multi_station import (
    collect_subscribe_topics,
    parse_device_id_from_payload,
    parse_device_id_from_topic,
    provisioning_config_topic,
    record_provisioning_hello,
    register_multi_routes,
)
from tenant_db import tenant_store_from_env

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

CSV_EXPORT_HEADER = ["recorded_at", *CSV_HEADER]

app = Flask(__name__)

_state_lock = threading.Lock()
_last_state: dict[str, Any] = {}
_device_states: dict[str, dict[str, Any]] = {}
_device_rx_at: dict[str, int] = {}

_mqtt_client: mqtt.Client | None = None
_args_ns: argparse.Namespace | None = None
_pg_store: PostgresStore | None = None
_mysql_store: MySQLStore | None = None
_bootstrap_lock = threading.Lock()
_bootstrapped = False
_bootstrap_notes: list[str] = []
_last_manual_publish: dict[str, Any] = {}
_last_mqtt_rx_at: int = 0
_mqtt_rx_count: int = 0
_last_mqtt_resubscribe_at: int = 0
_last_mqtt_loopback_at: int = 0
_last_mqtt_loopback_try_at: int = 0
_last_mqtt_restart_at: int = 0
_mqtt_sub_errors: list[str] = []
_mqtt_sub_ok: list[str] = []
_mqtt_thread: threading.Thread | None = None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _sanitize_mqtt_host(host: str) -> str:
    h = host.strip()
    for prefix in ("mqtts://", "mqtt://", "https://", "http://"):
        if h.lower().startswith(prefix):
            h = h[len(prefix) :]
    if "/" in h:
        h = h.split("/")[0]
    if ":" in h:
        h = h.rsplit(":", 1)[0]
    return h.strip()


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
    raw_host = _env("MQTT_HOST") or _env("MQTT_BROKER_HOST") or "127.0.0.1"
    p.add_argument("--mqtt-host", default=_sanitize_mqtt_host(raw_host))
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


def _csv_response_from_rows(
    rows: list[list[Any]],
    *,
    header: list[str] | None = None,
    filename: str = "irrigation_log.csv",
) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header or CSV_HEADER)
    for line in rows:
        w.writerow(line)
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _is_telemetry_topic(topic: str) -> bool:
    if not topic:
        return False
    if topic.endswith("/telemetry"):
        return True
    if _args_ns and topic == _args_ns.topic_telemetry:
        return True
    return False


def _mqtt_subscribe_all(client: mqtt.Client, ns: argparse.Namespace) -> None:
    """Topics exacts seulement (HiveMQ refuse souvent les wildcards + et #)."""
    tenant = tenant_store_from_env()
    tel_topics = collect_subscribe_topics(ns.device_id, ns.topic_telemetry, tenant)
    topics: list[tuple[str, int]] = [(t, 0) for t in tel_topics]
    legacy = _env("MQTT_TOPIC_COMMAND_LEGACY", "irrigation/command/manual")
    if legacy:
        topics.append((legacy, 1))
    seen: set[str] = set()
    for topic, qos in topics:
        if topic in seen:
            continue
        seen.add(topic)
        client.subscribe(topic, qos=qos)
    print(f"[MQTT] abonnements demandes: {sorted(seen)}")


def _on_mqtt_message(_client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
    global _last_state, _last_mqtt_rx_at, _mqtt_rx_count, _device_states, _device_rx_at
    if not _args_ns:
        return
    topic = msg.topic or ""

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if _is_telemetry_topic(topic):
            print(f"[MQTT] telemetry JSON invalide sur {topic!r}")
        return

    if not isinstance(payload, dict):
        return

    if topic == "irrigation/provisioning/hello" or payload.get("cmd") == "hello":
        mac = str(payload.get("mac", "") or "").strip()
        print(f"[MQTT] provisioning hello mac={mac!r}")
        if mac:
            record_provisioning_hello(mac, ip=str(payload.get("ip", "")))
        return

    if payload.get("_bridge_ping"):
        global _last_mqtt_loopback_at, _last_mqtt_loopback_try_at
        _last_mqtt_loopback_at = int(time.time())
        _last_mqtt_loopback_try_at = 0
        print(f"[MQTT] loopback OK sur {topic!r}")
        return
    if payload.get("cmd") == "manual":
        print(f"[MQTT] ignore relay manuel sur {topic!r}")
        return
    if topic in (_args_ns.topic_command, _env("MQTT_TOPIC_COMMAND_LEGACY", "irrigation/command/manual")):
        print(f"[MQTT] echo commande sur {topic!r}")
        return

    if _is_telemetry_topic(topic):
        _last_mqtt_rx_at = int(time.time())
        _mqtt_rx_count += 1
        device_id = parse_device_id_from_payload(payload, topic)
        _device_rx_at[device_id] = _last_mqtt_rx_at
        if _mqtt_rx_count <= 3 or _mqtt_rx_count % 30 == 0:
            print(
                f"[MQTT] RX telemetry #{_mqtt_rx_count} device={device_id!r} "
                f"topic={topic!r} len={len(msg.payload)}"
            )
    else:
        return

    if "sensors" not in payload:
        return

    device_id = parse_device_id_from_payload(payload, topic)
    with _state_lock:
        prev = _device_states.get(device_id, {})
        merged = dict(payload)
        merged["device_id"] = device_id
        for key in ("decision", "manual", "model_inputs"):
            if key not in merged and key in prev:
                merged[key] = prev[key]
        if "decision" not in payload and "sensors" in payload:
            print(f"[MQTT] telemetry sans bloc decision device={device_id!r}")
        _device_states[device_id] = merged
        _last_state = merged
        payload = merged

    row = extract_telemetry_from_mqtt(payload)

    if not is_row_complete_for_db(row):
        return

    if _pg_store is not None:
        try:
            _pg_store.insert(row, device_id=device_id)
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


def _mqtt_configured() -> bool:
    return bool(_env("MQTT_HOST") or _env("MQTT_BROKER_HOST"))


def _mqtt_try_loopback() -> bool:
    """Publie un ping sur le topic telemetry pour verifier broker + abonnement Render."""
    global _last_mqtt_loopback_try_at
    if _mqtt_client is None or _args_ns is None:
        return False
    try:
        if not _mqtt_client.is_connected():
            return False
    except Exception:
        return False
    topic = _args_ns.topic_telemetry
    body = json.dumps({"_bridge_ping": 1, "t": int(time.time())})
    info = _mqtt_client.publish(topic, body, qos=0, retain=False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        return False
    _last_mqtt_loopback_try_at = int(time.time())
    deadline = time.time() + 8.0
    while time.time() < deadline:
        time.sleep(0.2)
        if _last_mqtt_loopback_at >= _last_mqtt_loopback_try_at:
            return True
    return False


def _mqtt_stop() -> None:
    global _mqtt_client, _mqtt_thread
    if _mqtt_client is not None:
        try:
            _mqtt_client.disconnect()
        except Exception:
            pass
        try:
            _mqtt_client.loop_stop()
        except Exception:
            pass
        _mqtt_client = None
    _mqtt_thread = None


def _mqtt_loop_forever(client: mqtt.Client) -> None:
    try:
        client.loop_forever(retry_first_connection=True)
    except Exception as e:
        print("[MQTT] loop_forever termine:", e)


def _mqtt_restart(ns: argparse.Namespace) -> mqtt.Client | None:
    global _last_mqtt_restart_at
    _last_mqtt_restart_at = int(time.time())
    print("[MQTT] redemarrage client...")
    _mqtt_stop()
    time.sleep(0.5)
    return _start_mqtt(ns)


def _wait_mqtt_connected(client: mqtt.Client, timeout_s: float = 12.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if client.is_connected():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _start_mqtt(ns: argparse.Namespace) -> mqtt.Client | None:
    global _mqtt_client, _args_ns, _mqtt_thread
    _args_ns = ns

    if not _mqtt_configured():
        msg = "MQTT ignore: definissez MQTT_HOST sur Render (HiveMQ)."
        print(f"[bridge] {msg}")
        _bootstrap_notes.append(msg)
        return None

    def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc == 0:
            _mqtt_subscribe_all(client, ns)
            print(f"[MQTT] connecte broker {ns.mqtt_host}:{ns.mqtt_port}")
        else:
            print(f"[MQTT] connexion refusee, code {rc}")
            _bootstrap_notes.append(f"MQTT on_connect rc={rc}")

    def on_subscribe(_client: mqtt.Client, _userdata: Any, mid: int, granted_qos: list[int]) -> None:
        global _mqtt_sub_errors, _mqtt_sub_ok
        print(f"[MQTT] on_subscribe mid={mid} qos={granted_qos}")
        for q in granted_qos:
            if q == 0x80:
                err = f"abonnement refuse (ACL HiveMQ?) mid={mid}"
                print(f"[MQTT] {err}")
                _mqtt_sub_errors.append(err)
                if len(_mqtt_sub_errors) > 10:
                    del _mqtt_sub_errors[0]
            else:
                _mqtt_sub_ok.append(f"mid={mid} qos={q}")
                if len(_mqtt_sub_ok) > 10:
                    del _mqtt_sub_ok[0]

    cid = f"{ns.mqtt_client_id}_{int(time.time()) % 100000}"
    client = mqtt.Client(client_id=cid, clean_session=True)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = _on_mqtt_message
    if ns.mqtt_user:
        client.username_pw_set(ns.mqtt_user, ns.mqtt_password or None)
    if ns.mqtt_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    try:
        client.connect(ns.mqtt_host, ns.mqtt_port, keepalive=60)
    except Exception as e:
        msg = f"MQTT connect exception: {e}"
        print(f"[bridge] {msg}")
        _bootstrap_notes.append(msg)
        return None
    _mqtt_thread = threading.Thread(
        target=_mqtt_loop_forever,
        args=(client,),
        daemon=True,
        name="mqtt-loop",
    )
    _mqtt_thread.start()
    if not _wait_mqtt_connected(client):
        msg = f"MQTT timeout vers {ns.mqtt_host}:{ns.mqtt_port} (verifier MQTT_USER/PASSWORD)"
        print(f"[bridge] {msg}")
        _bootstrap_notes.append(msg)
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
            msg = f"Postgres: {e}"
            print(f"[bridge] {msg}")
            _bootstrap_notes.append(msg)
            _pg_store = None
    elif not _env("DATABASE_URL"):
        _bootstrap_notes.append("DATABASE_URL non defini sur Render.")

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
        tenant = tenant_store_from_env()
        if tenant is not None:
            try:
                tenant.ensure_schema()
                print("[bridge] Schema multi-tenant OK")
            except Exception as e:
                print("[bridge] Schema multi-tenant:", e)
                _bootstrap_notes.append(f"tenant schema: {e}")
        _init_storage(ns)
        _start_mqtt(ns)
        _bootstrapped = True
        return ns


def _get_pg_store() -> PostgresStore | None:
    """Store Postgres (Supabase), avec reconnexion si le bootstrap initial a echoue."""
    global _pg_store
    if _pg_store is not None:
        return _pg_store
    if not _env("DATABASE_URL"):
        return None
    try:
        _pg_store = postgres_store_from_env()
        if _pg_store is not None:
            _pg_store.ensure_table()
    except Exception as e:
        print("[bridge] Postgres (lazy):", e)
        _pg_store = None
    return _pg_store


def _parse_export_date(raw: str | None) -> date | None:
    if not raw or not str(raw).strip():
        return None
    try:
        return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Date invalide : {raw!r} (attendu AAAA-MM-JJ)")


def _csv_export_filename(crop: str | None, date_from: date | None, date_to: date | None) -> str:
    if not crop and not date_from and not date_to:
        return "irrigation_log.csv"
    parts = ["irrigation_log"]
    if crop:
        parts.append(crop.lower())
    if date_from:
        parts.append(f"from-{date_from.isoformat()}")
    if date_to:
        parts.append(f"to-{date_to.isoformat()}")
    return "_".join(parts) + ".csv"


@app.route("/")
def index() -> Response:
    html_path = Path(__file__).resolve().parent / "dashboard.html"
    return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")


@app.route("/api/health", methods=["GET"])
def api_health() -> Any:
    global _bootstrapped
    if not _bootstrapped:
        try:
            bootstrap([])
        except Exception as e:
            _bootstrap_notes.append(f"bootstrap: {e}")

    pg = _get_pg_store()
    mqtt_ok = False
    if _mqtt_client is not None:
        try:
            mqtt_ok = bool(_mqtt_client.is_connected())
        except Exception:
            mqtt_ok = False

    ns = _args_ns
    now = int(time.time())
    loopback_ok = _last_mqtt_loopback_at > 0
    if mqtt_ok and _mqtt_client is not None and ns and _last_mqtt_rx_at == 0:
        global _last_mqtt_resubscribe_at, _last_mqtt_loopback_try_at, _last_mqtt_restart_at
        if now - _last_mqtt_resubscribe_at >= 45:
            _last_mqtt_resubscribe_at = now
            try:
                _mqtt_subscribe_all(_mqtt_client, ns)
                print("[MQTT] re-abonnement telemetry")
            except Exception as e:
                print("[MQTT] re-abonnement echoue:", e)
        if _last_mqtt_loopback_try_at == 0 or now - _last_mqtt_loopback_try_at >= 90:
            loopback_ok = _mqtt_try_loopback() or (_last_mqtt_loopback_at > 0)
        if not loopback_ok and (now - _last_mqtt_restart_at >= 120 or _last_mqtt_restart_at == 0):
            _mqtt_restart(ns)
            mqtt_ok = bool(_mqtt_client and _mqtt_client.is_connected())
            if _last_mqtt_loopback_try_at == 0 or now - _last_mqtt_loopback_try_at >= 90:
                loopback_ok = _mqtt_try_loopback() or (_last_mqtt_loopback_at > 0)

    rx_hint = ""
    if mqtt_ok and _last_mqtt_rx_at == 0:
        if loopback_ok:
            rx_hint = (
                "Render recoit MQTT (loopback OK) mais pas encore l'ESP — attendez le prochain "
                "[MQTT] OK publie ou redemarrez l'ESP."
            )
        else:
            rx_hint = (
                "Render connecte a HiveMQ mais n'ecoute pas la telemetry (abonnement). "
                "HiveMQ: autoriser SUBSCRIBE sur irrigation/station01/telemetry pour le meme "
                "utilisateur que l'ESP. Puis redeployez le bridge (git push)."
            )
    elif _last_mqtt_rx_at > 0:
        rx_hint = f"Derniere telemetrie il y a {now - _last_mqtt_rx_at}s."

    return jsonify(
        {
            "ok": True,
            "mqtt": mqtt_ok,
            "postgres": pg is not None,
            "env": {
                "DATABASE_URL": bool(_env("DATABASE_URL")),
                "MQTT_HOST": bool(_env("MQTT_HOST") or _env("MQTT_BROKER_HOST")),
                "MQTT_USER": bool(_env("MQTT_USER")),
                "MQTT_PASSWORD": bool(_env("MQTT_PASSWORD")),
                "DEVICE_ID": _env("DEVICE_ID", "station01"),
            },
            "mqtt_loopback_ok": loopback_ok,
            "mqtt_sub_errors": _mqtt_sub_errors[-3:],
            "mqtt_sub_ok": _mqtt_sub_ok[-3:],
            "mqtt_user": (_env("MQTT_USER")[:20] + "...") if len(_env("MQTT_USER")) > 20 else _env("MQTT_USER"),
            "mqtt_target": f"{ns.mqtt_host}:{ns.mqtt_port}" if ns else "",
            "topic_telemetry": ns.topic_telemetry if ns else "",
            "topic_command": ns.topic_command if ns else "",
            "topic_command_legacy": _env("MQTT_TOPIC_COMMAND_LEGACY", "irrigation/command/manual"),
            "topic_relay": _relay_topic(ns) if ns else "",
            "last_mqtt_rx_at": _last_mqtt_rx_at,
            "mqtt_rx_count": _mqtt_rx_count,
            "mqtt_rx_hint": rx_hint,
            "last_manual_publish": _last_manual_publish,
            "notes": _bootstrap_notes[-5:],
        }
    )


def _relay_topic(ns: argparse.Namespace, device_id: str | None = None) -> str:
    t = _env("MQTT_TOPIC_RELAY", "").strip()
    if t and not device_id:
        return t
    did = device_id or ns.device_id
    return f"irrigation/{did}/command/relay"


def _command_topics_for_device(ns: argparse.Namespace, device_id: str) -> list[str]:
    topics = [f"irrigation/{device_id}/command/manual"]
    legacy = _env("MQTT_TOPIC_COMMAND_LEGACY", "irrigation/command/manual")
    if legacy and legacy not in topics:
        topics.append(legacy)
    if device_id == ns.device_id and ns.topic_command not in topics:
        topics.insert(0, ns.topic_command)
    return topics


def _command_topics(ns: argparse.Namespace) -> list[str]:
    """Topic principal + ancien topic (compatibilite firmware)."""
    topics = [ns.topic_command]
    legacy = _env("MQTT_TOPIC_COMMAND_LEGACY", "irrigation/command/manual")
    if legacy and legacy not in topics:
        topics.append(legacy)
    return topics


def _mqtt_publish_one(topic: str, body: str, qos: int = 1) -> dict[str, Any]:
    """Publie et attend le PUBACK (QoS 1) — QoS 0 ment souvent si HiveMQ refuse (ACL)."""
    if not _mqtt_client:
        return {"topic": topic, "mqtt_rc": -1, "published": False, "qos": qos}
    try:
        if not _mqtt_client.is_connected():
            return {"topic": topic, "mqtt_rc": -2, "published": False, "qos": qos}
    except Exception:
        return {"topic": topic, "mqtt_rc": -2, "published": False, "qos": qos}

    info = _mqtt_client.publish(topic, body, qos=qos, retain=False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        return {"topic": topic, "mqtt_rc": int(info.rc), "published": False, "qos": qos}

    published = False
    deadline = time.time() + 15.0
    while time.time() < deadline:
        time.sleep(0.05)
        if info.is_published():
            published = True
            break
    return {"topic": topic, "mqtt_rc": int(info.rc), "published": published, "qos": qos}


def _mqtt_publish_manual(
    age: int, ci: int, si: int, device_id: str | None = None
) -> tuple[list[dict[str, Any]], str, str]:
    global _last_manual_publish
    assert _args_ns is not None
    did = (device_id or _args_ns.device_id or "station01").strip()
    body_cmd = json.dumps({"crop_age_days": age, "crop_idx": ci, "soil_idx": si})
    body_relay = json.dumps({"cmd": "manual", "crop_age_days": age, "crop_idx": ci, "soil_idx": si})
    results: list[dict[str, Any]] = []
    relay_topic = _relay_topic(_args_ns, did)
    cmd_topics = _command_topics_for_device(_args_ns, did)

    r_relay = _mqtt_publish_one(relay_topic, body_relay, qos=1)
    results.append(r_relay)
    if not r_relay.get("published"):
        for topic in cmd_topics:
            results.append(_mqtt_publish_one(topic, body_cmd, qos=1))
    else:
        for topic in cmd_topics:
            results.append(_mqtt_publish_one(topic, body_cmd, qos=1))

    _last_manual_publish = {
        "body_cmd": body_cmd,
        "body_relay": body_relay,
        "results": results,
        "at": int(time.time()),
    }
    return results, body_cmd, body_relay


def _api_manual_impl() -> Any:
    if not _mqtt_client or not _args_ns:
        return jsonify({"ok": False, "error": "MQTT non initialise"}), 503
    if not _mqtt_client.is_connected():
        return jsonify({"ok": False, "error": "MQTT deconnecte (reessayez dans 10 s)"}), 503
    try:
        age = int(request.form.get("crop_age_days", 0))
        ci = int(request.form.get("crop_idx", ""))
        si = int(request.form.get("soil_idx", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Champs invalides (age, culture, sol)"}), 400
    if age < 1 or age > 120:
        return jsonify({"ok": False, "error": "age doit etre entre 1 et 120"}), 400
    if ci < 0 or ci > 3 or si < 0 or si > 3:
        return jsonify({"ok": False, "error": "culture et sol : indices 0-3"}), 400

    device_id = (
        request.form.get("device_id")
        or request.args.get("device")
        or _env("DEVICE_ID", "station01")
    ).strip()
    from auth_supabase import auth_enabled
    from multi_station import check_device_access, get_bearer_user, tenant_store_from_env

    if auth_enabled():
        user = get_bearer_user()
        if not user:
            return jsonify({"ok": False, "error": "Authentification requise"}), 401
        tenant = tenant_store_from_env()
        if not check_device_access(user["id"], device_id, tenant):
            return jsonify({"ok": False, "error": "Acces refuse a cette station"}), 403
    results, body_cmd, body_relay = _mqtt_publish_manual(age, ci, si, device_id)
    ok = any(r.get("published") for r in results)
    relay_topic = _relay_topic(_args_ns, device_id)
    via = "relay" if any(r.get("published") and r.get("topic") == relay_topic for r in results) else "command"
    if not ok:
        return jsonify(
            {
                "ok": False,
                "via": via,
                "topics": results,
                "payload": json.loads(body_cmd),
                "error": (
                    "Publish MQTT refuse par HiveMQ (PUBLISH sur command/relay). "
                    "Permissions : irrigation/station01/command/# et irrigation/command/manual"
                ),
            }
        ), 502
    return jsonify(
        {
            "ok": True,
            "via": via,
            "topics": results,
            "payload": json.loads(body_cmd),
            "hint": "Moniteur ESP : [MQTT] RX topic=... puis Saisie manuelle appliquee (~5 s)",
        }
    )


@app.route("/api/manual", methods=["POST"])
def api_manual() -> Any:
    return _api_manual_impl()


def main() -> None:
    ns = bootstrap(sys.argv[1:])
    print(f"[bridge] Dashboard : http://{ns.http_host}:{ns.http_port}/")
    print(f"[bridge] MQTT {ns.mqtt_host}:{ns.mqtt_port} tls={ns.mqtt_tls}")
    app.run(host=ns.http_host, port=ns.http_port, debug=False, threaded=True)


def _health_snapshot() -> dict[str, Any]:
    mqtt_ok = False
    if _mqtt_client is not None:
        try:
            mqtt_ok = bool(_mqtt_client.is_connected())
        except Exception:
            mqtt_ok = False
    return {
        "mqtt": mqtt_ok,
        "last_mqtt_rx_at": _last_mqtt_rx_at,
        "mqtt_rx_count": _mqtt_rx_count,
    }


register_multi_routes(
    app,
    get_states=lambda: _device_states,
    state_lock=_state_lock,
    get_mqtt_publish=_mqtt_publish_one,
    get_pg_store=_get_pg_store,
    get_default_device=lambda: (_args_ns.device_id if _args_ns else _env("DEVICE_ID", "station01")),
    csv_export_header=CSV_EXPORT_HEADER,
    parse_export_date=_parse_export_date,
    csv_export_filename=_csv_export_filename,
    csv_response_from_rows=_csv_response_from_rows,
    get_health_snapshot=_health_snapshot,
)

# Gunicorn (Render) importe ce module : ne pas lire sys.argv (contient "bridge:app", --bind, etc.)
if __name__ != "__main__":
    try:
        bootstrap([])
    except Exception as e:
        print("[bridge] bootstrap au chargement:", e)
        _bootstrap_notes.append(f"bootstrap import: {e}")


if __name__ == "__main__":
    main()
