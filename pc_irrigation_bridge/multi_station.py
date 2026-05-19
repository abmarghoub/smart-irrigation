"""
Multi-stations, auth, admin, provisioning — intégré par bridge.py
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from functools import wraps
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from auth_supabase import (
    auth_enabled,
    delete_supabase_user,
    is_admin_email,
    register_supabase_user,
    verify_access_token,
)
from db_mysql import VALID_CROP_NAMES, is_row_complete_for_db, extract_telemetry_from_mqtt
from tenant_db import TenantStore, normalize_mac, mac_compact, tenant_store_from_env

_TOPIC_DEV_RE = re.compile(r"^irrigation/([A-Za-z0-9_]+)/telemetry$")
_PROV_HELLO = "irrigation/provisioning/hello"
_PROV_CONFIG_PREFIX = "irrigation/provisioning/"
_provisioning_hellos: dict[str, dict[str, Any]] = {}


def record_provisioning_hello(mac: str, *, ip: str = "") -> None:
    try:
        mac_n = normalize_mac(mac)
    except ValueError:
        mac_n = mac.strip()
    if not mac_n:
        return
    _provisioning_hellos[mac_n] = {"mac": mac_n, "ip": ip, "at": int(time.time())}


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def parse_device_id_from_topic(topic: str) -> str | None:
    m = _TOPIC_DEV_RE.match(topic or "")
    return m.group(1) if m else None


def parse_device_id_from_payload(payload: dict[str, Any], topic: str) -> str:
    did = (payload.get("device_id") or "").strip()
    if did:
        return did[:64]
    from_topic = parse_device_id_from_topic(topic)
    if from_topic:
        return from_topic
    return _env("DEVICE_ID", "station01") or "station01"


def provisioning_config_topic(mac: str) -> str:
    return f"{_PROV_CONFIG_PREFIX}{mac_compact(mac)}/config"


def collect_subscribe_topics(
    default_device: str,
    extra_from_env: str,
    tenant: TenantStore | None,
) -> list[str]:
    topics: set[str] = set()
    primary = default_device or "station01"
    topics.add(f"irrigation/{primary}/telemetry")
    raw = extra_from_env or _env("DEVICE_IDS", "") or _env("MQTT_TOPICS", "")
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        if p.endswith("/telemetry"):
            topics.add(p)
        elif "/" not in p:
            topics.add(f"irrigation/{p}/telemetry")
        else:
            topics.add(p)
    if tenant:
        try:
            for did in tenant.list_active_device_ids():
                topics.add(f"irrigation/{did}/telemetry")
        except Exception as e:
            print("[multi] list devices:", e)
    topics.add(_PROV_HELLO)
    return sorted(topics)


def get_bearer_user() -> dict[str, Any] | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return verify_access_token(token)
    return None


def require_auth(admin: bool = False) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not auth_enabled():
                request.user = {"id": "local", "email": "local@bridge"}  # type: ignore[attr-defined]
                request.is_admin = True  # type: ignore[attr-defined]
                return fn(*args, **kwargs)
            user = get_bearer_user()
            if not user:
                return jsonify({"error": "Authentification requise"}), 401
            tenant = tenant_store_from_env()
            is_admin = is_admin_email(user.get("email", ""))
            if tenant:
                try:
                    tenant.upsert_app_user(user["id"], user.get("email", ""))
                    is_admin = is_admin or tenant.is_admin_user(
                        user["id"], user.get("email", "")
                    )
                except Exception:
                    pass
            if admin and not is_admin:
                return jsonify({"error": "Acces admin refuse"}), 403
            request.user = user  # type: ignore[attr-defined]
            request.is_admin = is_admin  # type: ignore[attr-defined]
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def check_device_access(user_id: str, device_id: str, tenant: TenantStore | None) -> bool:
    if not auth_enabled():
        return True
    if not tenant:
        return True
    if getattr(request, "is_admin", False):  # type: ignore[attr-defined]
        return True
    return tenant.user_owns_device(user_id, device_id)


def publish_set_device_id(mqtt_publish_fn: Callable, device_id: str, mac: str) -> dict[str, Any]:
    topic = provisioning_config_topic(mac)
    body = json.dumps(
        {
            "cmd": "set_device_id",
            "device_id": device_id,
            "telemetry": f"irrigation/{device_id}/telemetry",
            "command": f"irrigation/{device_id}/command/manual",
            "relay": f"irrigation/{device_id}/command/relay",
        }
    )
    return mqtt_publish_fn(topic, body, qos=1)


def register_multi_routes(
    app: Flask,
    *,
    get_states: Callable[[], dict[str, dict[str, Any]]],
    state_lock: Any,
    get_mqtt_publish: Callable[[str, str, int], dict[str, Any]],
    get_pg_store: Callable[[], Any],
    get_default_device: Callable[[], str],
    csv_export_header: list[str],
    parse_export_date: Callable[[str | None], date | None],
    csv_export_filename: Callable,
    csv_response_from_rows: Callable,
    get_health_snapshot: Callable[[], dict[str, Any]] | None = None,
) -> None:
    def _health() -> dict[str, Any]:
        if get_health_snapshot:
            return get_health_snapshot()
        return {}
    @app.route("/login")
    def login_page() -> Response:
        path = os.path.join(os.path.dirname(__file__), "login.html")
        return Response(open(path, encoding="utf-8").read(), mimetype="text/html; charset=utf-8")

    @app.route("/admin")
    @app.route("/admin/<path:subpath>")
    def admin_page(subpath: str = "") -> Response:
        path = os.path.join(os.path.dirname(__file__), "admin_dashboard.html")
        return Response(open(path, encoding="utf-8").read(), mimetype="text/html; charset=utf-8")

    @app.route("/view")
    def view_client_page() -> Response:
        path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        return Response(open(path, encoding="utf-8").read(), mimetype="text/html; charset=utf-8")

    @app.route("/provision")
    def provision_page() -> Response:
        path = os.path.join(os.path.dirname(__file__), "provision.html")
        return Response(open(path, encoding="utf-8").read(), mimetype="text/html; charset=utf-8")

    @app.route("/api/admin/provisioning_hellos", methods=["GET"])
    @require_auth(admin=True)
    def api_admin_hellos() -> Any:
        import datetime

        rows = []
        for mac, info in sorted(_provisioning_hellos.items(), key=lambda x: -x[1].get("at", 0)):
            at = info.get("at", 0)
            rows.append(
                {
                    "mac": mac,
                    "ip": info.get("ip", ""),
                    "at": at,
                    "at_iso": datetime.datetime.utcfromtimestamp(at).isoformat() + "Z" if at else "",
                }
            )
        return jsonify({"hellos": rows[:50]})

    @app.route("/api/auth/config", methods=["GET"])
    def api_auth_config() -> Any:
        return jsonify(
            {
                "auth_enabled": auth_enabled(),
                "supabase_url": _env("SUPABASE_URL"),
                "supabase_anon_key": _env("SUPABASE_ANON_KEY"),
            }
        )

    @app.route("/api/me", methods=["GET"])
    @require_auth()
    def api_me() -> Any:
        user = request.user  # type: ignore[attr-defined]
        tenant = tenant_store_from_env()
        devices: list[str] = []
        if tenant and auth_enabled():
            devices = tenant.list_user_devices(user["id"])
        elif not auth_enabled():
            devices = [get_default_device()]
        return jsonify(
            {
                "id": user["id"],
                "email": user.get("email", ""),
                "is_admin": getattr(request, "is_admin", False),
                "devices": devices,
            }
        )

    @app.route("/api/register-device", methods=["POST"])
    def api_register_device() -> Any:
        data = request.get_json(silent=True) or {}
        try:
            mac = normalize_mac(str(data.get("mac", "")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        first_name = str(data.get("first_name", "")).strip()
        last_name = str(data.get("last_name", "")).strip()
        if not email or len(password) < 6:
            return jsonify({"error": "email et mot de passe (6+ caracteres) requis"}), 400
        tenant = tenant_store_from_env()
        if tenant is None:
            return jsonify({"error": "Base de donnees indisponible"}), 503
        try:
            sb_user = register_supabase_user(email, password)
            uid = sb_user.get("id")
            if not uid and sb_user.get("error"):
                return jsonify({"error": sb_user.get("error", "inscription echouee")}), 400
            if not uid:
                return jsonify({"error": "Utilisateur non cree"}), 500
            tenant.upsert_app_user(uid, email, first_name=first_name, last_name=last_name)
            row = tenant.create_pending(
                mac, email, first_name=first_name, last_name=last_name, supabase_user_id=uid
            )
            return jsonify({"ok": True, "pending_id": row.get("id"), "mac": mac, "status": "pending"})
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/pending", methods=["GET"])
    @require_auth(admin=True)
    def api_admin_pending() -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        return jsonify({"pending": tenant.list_pending()})

    @app.route("/api/admin/stats", methods=["GET"])
    @require_auth(admin=True)
    def api_admin_stats() -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        import os

        table = os.environ.get("PG_TABLE", "irrigation_telemetry").strip() or "irrigation_telemetry"
        stats = tenant.admin_stats(table)
        h = _health()
        stats["mqtt_connected"] = h.get("mqtt", False)
        stats["last_mqtt_rx_at"] = h.get("last_mqtt_rx_at", 0)
        stats["mqtt_rx_count"] = h.get("mqtt_rx_count", 0)
        pending = tenant.list_pending()[:5]
        for p in pending:
            if p.get("created_at") and hasattr(p["created_at"], "isoformat"):
                p["created_at"] = p["created_at"].isoformat()
        stats["recent_pending"] = pending
        hellos = []
        for mac, info in sorted(_provisioning_hellos.items(), key=lambda x: -x[1].get("at", 0))[:5]:
            hellos.append(info)
        stats["recent_hellos"] = hellos
        return jsonify(stats)

    @app.route("/api/admin/users", methods=["GET"])
    @require_auth(admin=True)
    def api_admin_users_list() -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        return jsonify({"users": tenant.list_users_detailed()})

    @app.route("/api/admin/users", methods=["POST"])
    @require_auth(admin=True)
    def api_admin_users_create() -> Any:
        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        first_name = str(data.get("first_name", "")).strip()
        last_name = str(data.get("last_name", "")).strip()
        device_id = str(data.get("device_id", "")).strip()
        if not email or len(password) < 6:
            return jsonify({"error": "email et mot de passe (6+) requis"}), 400
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            sb = register_supabase_user(email, password)
            uid = sb.get("id")
            if not uid:
                return jsonify({"error": sb.get("error", "creation echouee")}), 400
            tenant.upsert_app_user(uid, email, first_name=first_name, last_name=last_name)
            if device_id:
                tenant.assign_device_to_user(device_id, uid)
            return jsonify({"ok": True, "id": uid, "email": email})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/users/<user_id>", methods=["PATCH"])
    @require_auth(admin=True)
    def api_admin_users_patch(user_id: str) -> Any:
        data = request.get_json(silent=True) or {}
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        tenant.update_app_user_profile(
            user_id,
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
        )
        device_ids = data.get("device_ids")
        if isinstance(device_ids, list):
            for did in device_ids:
                if did:
                    try:
                        tenant.assign_device_to_user(str(did), user_id)
                    except ValueError:
                        pass
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<user_id>", methods=["DELETE"])
    @require_auth(admin=True)
    def api_admin_users_delete(user_id: str) -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            tenant.deactivate_user_stations(user_id)
            try:
                delete_supabase_user(user_id)
            except Exception as e:
                print("[admin] delete supabase user:", e)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/devices", methods=["GET"])
    @require_auth(admin=True)
    def api_admin_devices() -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        status = request.args.get("status", "all").strip() or "all"
        states = get_states()
        hellos: list[dict[str, Any]] = []
        if status == "hello":
            devices = []
            pending = []
            for mac, info in sorted(
                _provisioning_hellos.items(), key=lambda x: -x[1].get("at", 0)
            ):
                hellos.append(
                    {
                        "mac": mac,
                        "ip": info.get("ip", ""),
                        "at": info.get("at", 0),
                    }
                )
        elif status == "pending":
            devices = []
            pending = tenant.list_pending()
        else:
            devices = tenant.list_devices_filtered(None if status == "all" else status)
            pending = tenant.list_pending() if status in ("all", "pending") else []
        for d in devices:
            did = d.get("device_id")
            st = states.get(did, {}) if did else {}
            d["last_ip"] = st.get("ip", "")
            d["wifi_connected"] = st.get("wifi_connected", False)
        for p in pending:
            if p.get("created_at") and hasattr(p["created_at"], "isoformat"):
                p["created_at"] = p["created_at"].isoformat()
        return jsonify({"devices": devices, "pending": pending, "hellos": hellos})

    @app.route("/api/admin/devices/<device_id>/assign", methods=["POST"])
    @require_auth(admin=True)
    def api_admin_device_assign(device_id: str) -> Any:
        data = request.get_json(silent=True) or {}
        user_id = str(data.get("user_id", "")).strip()
        if not user_id:
            return jsonify({"error": "user_id requis"}), 400
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            tenant.assign_device_to_user(device_id, user_id)
            return jsonify({"ok": True})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/admin/devices/<device_id>/unclaim", methods=["POST"])
    @require_auth(admin=True)
    def api_admin_device_unclaim(device_id: str) -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            tenant.admin_force_unclaimed(device_id)
            return jsonify({"ok": True, "status": "unclaimed"})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/admin/devices/<device_id>/resend-config", methods=["POST"])
    @require_auth(admin=True)
    def api_admin_device_resend(device_id: str) -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        dev = tenant.get_device(device_id)
        if not dev:
            return jsonify({"error": "Station inconnue"}), 404
        pub = publish_set_device_id(get_mqtt_publish, device_id, dev["mac"])
        return jsonify({"ok": True, "mqtt": pub})

    @app.route("/api/admin/view-client", methods=["GET"])
    @require_auth(admin=True)
    def api_admin_view_client() -> Any:
        user_id = request.args.get("user_id", "").strip()
        device_id = request.args.get("device", "").strip()
        tenant = tenant_store_from_env()
        email = ""
        if tenant and user_id:
            u = tenant.get_user_by_id(user_id)
            if u:
                email = u.get("email", "")
        return jsonify({"user_id": user_id, "device_id": device_id, "email": email})

    @app.route("/api/admin/pending/<int:pending_id>/approve", methods=["POST"])
    @require_auth(admin=True)
    def api_admin_approve(pending_id: int) -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            result = tenant.approve_pending(pending_id)
            device_id = result["device_id"]
            mac = result["mac"]
            pub = publish_set_device_id(get_mqtt_publish, device_id, mac)
            return jsonify({"ok": True, "device_id": device_id, "mac": mac, "mqtt": pub})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/pending/<int:pending_id>/reject", methods=["POST"])
    @require_auth(admin=True)
    def api_admin_reject(pending_id: int) -> Any:
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        notes = (request.get_json(silent=True) or {}).get("notes", "")
        tenant.reject_pending(pending_id, notes=str(notes))
        return jsonify({"ok": True})

    @app.route("/api/device/transfer", methods=["POST"])
    @require_auth()
    def api_device_transfer() -> Any:
        user = request.user  # type: ignore[attr-defined]
        data = request.get_json(silent=True) or {}
        device_id = str(data.get("device_id", "")).strip()
        if not device_id:
            return jsonify({"error": "device_id requis"}), 400
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            tenant.transfer_device(user["id"], device_id)
            return jsonify({"ok": True, "device_id": device_id, "status": "unclaimed"})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/device/reactivate", methods=["POST"])
    @require_auth()
    def api_device_reactivate() -> Any:
        user = request.user  # type: ignore[attr-defined]
        data = request.get_json(silent=True) or request.form
        mac = str((data or {}).get("mac", "")).strip()
        if not mac:
            return jsonify({"error": "mac requis"}), 400
        tenant = tenant_store_from_env()
        if not tenant:
            return jsonify({"error": "DB indisponible"}), 503
        try:
            info = tenant.reactivate_for_user(user["id"], mac)
            pub = publish_set_device_id(get_mqtt_publish, info["device_id"], info["mac"])
            return jsonify({"ok": True, **info, "mqtt": pub})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/states", methods=["GET"])
    @require_auth()
    def api_states_list() -> Any:
        user = request.user  # type: ignore[attr-defined]
        tenant = tenant_store_from_env()
        if auth_enabled() and tenant:
            devices = tenant.list_user_devices(user["id"])
        else:
            devices = [get_default_device()]
        return jsonify({"devices": devices})

    def _resolve_device_id() -> str | None:
        return (request.args.get("device") or request.form.get("device_id") or "").strip() or None

    @app.route("/api/state", methods=["GET"])
    @require_auth()
    def api_state_multi() -> Any:
        user = request.user  # type: ignore[attr-defined]
        device_id = _resolve_device_id() or get_default_device()
        tenant = tenant_store_from_env()
        if not check_device_access(user["id"], device_id, tenant):
            return jsonify({"error": "Acces refuse a cette station"}), 403
        with state_lock:
            states = get_states()
            st = states.get(device_id)
            if not st:
                return jsonify(
                    {
                        "device_id": device_id,
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
            out = dict(st)
            out["device_id"] = device_id
            return jsonify(out)

    @app.route("/api/irrigation_log.csv", methods=["GET"])
    @require_auth()
    def api_csv_multi() -> Any:
        user = request.user  # type: ignore[attr-defined]
        device_id = _resolve_device_id() or get_default_device()
        tenant = tenant_store_from_env()
        if not check_device_access(user["id"], device_id, tenant):
            return jsonify({"error": "Acces refuse"}), 403
        store = get_pg_store()
        if store is None:
            return jsonify({"error": "Base indisponible"}), 503
        crop = request.args.get("crop", "").strip()
        if crop and crop not in VALID_CROP_NAMES:
            return jsonify({"error": f"Culture invalide : {crop!r}"}), 400
        try:
            date_from = parse_export_date(request.args.get("from"))
            date_to = parse_export_date(request.args.get("to"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if date_from and date_to and date_from > date_to:
            return jsonify({"error": "La date de debut doit etre <= date de fin"}), 400
        recorded_after = None
        if tenant and auth_enabled():
            recorded_after = tenant.ownership_start(user["id"], device_id)
        try:
            rows = store.fetch_csv_rows_filtered(
                csv_export_header,
                device_id=device_id,
                recorded_after=recorded_after,
                crop_name=crop or None,
                date_from=date_from,
                date_to=date_to,
            )
        except Exception as e:
            return jsonify({"error": f"Lecture base: {e}"}), 500
        resp = csv_response_from_rows(rows, header=csv_export_header)
        resp.headers["X-CSV-Source"] = "database"
        resp.headers["X-CSV-Rows"] = str(len(rows))
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{csv_export_filename(crop or None, date_from, date_to)}"'
        )
        return resp
