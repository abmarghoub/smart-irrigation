"""
Tables multi-tenant (devices, pending, ownership) sur la même DATABASE_URL que le pont.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

import psycopg2.extras

from db_postgres import PostgresStore, normalize_database_url

_MAC_RE = re.compile(r"[^0-9A-Fa-f]")
_DEVICE_ID_SAFE = re.compile(r"[^A-Za-z0-9_]")


def normalize_mac(raw: str) -> str:
    s = _MAC_RE.sub("", (raw or "").upper())
    if len(s) != 12:
        raise ValueError(f"MAC invalide : {raw!r}")
    return ":".join(s[i : i + 2] for i in range(0, 12, 2))


def mac_compact(mac: str) -> str:
    return normalize_mac(mac).replace(":", "")


def generate_device_id(mac: str, *, seq: int | None = None) -> str:
    compact = mac_compact(mac)
    suffix = compact[-6:].lower()
    base = f"station_{suffix}"
    if seq is None:
        return base[:64]
    return f"{base}_{seq}"[:64]


def _connect(database_url: str) -> Any:
    import psycopg2

    return psycopg2.connect(normalize_database_url(database_url), connect_timeout=15)


class TenantStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("DATABASE_URL vide")
        self._url = normalize_database_url(database_url)

    def ensure_schema(self) -> None:
        path = (
            __file__.replace("\\", "/").rsplit("/", 1)[0]
            + "/../supabase/migrations/001_multi_tenant.sql"
        )
        # DDL inline (évite dépendance chemin sur Render)
        ddl = """
        CREATE TABLE IF NOT EXISTS devices (
          mac TEXT PRIMARY KEY,
          device_id VARCHAR(64) NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'active',
          current_owner_id UUID NULL,
          label TEXT NOT NULL DEFAULT '',
          first_registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          unclaimed_at TIMESTAMPTZ NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS user_stations (
          user_id UUID NOT NULL,
          device_id VARCHAR(64) NOT NULL,
          active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (user_id, device_id)
        );
        CREATE TABLE IF NOT EXISTS device_ownership_history (
          id BIGSERIAL PRIMARY KEY,
          device_id VARCHAR(64) NOT NULL,
          mac TEXT NOT NULL,
          user_id UUID NOT NULL,
          from_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          to_date TIMESTAMPTZ NULL,
          reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS pending_registrations (
          id BIGSERIAL PRIMARY KEY,
          mac TEXT NOT NULL,
          email TEXT NOT NULL,
          first_name TEXT NOT NULL DEFAULT '',
          last_name TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          supabase_user_id UUID NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          resolved_at TIMESTAMPTZ NULL,
          notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS app_users (
          id UUID PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          first_name TEXT NOT NULL DEFAULT '',
          last_name TEXT NOT NULL DEFAULT '',
          is_admin BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()

    def upsert_app_user(
        self,
        user_id: str,
        email: str,
        *,
        first_name: str = "",
        last_name: str = "",
        is_admin: bool = False,
    ) -> None:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_users (id, email, first_name, last_name, is_admin)
                    VALUES (%s::uuid, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      email = EXCLUDED.email,
                      first_name = COALESCE(NULLIF(EXCLUDED.first_name, ''), app_users.first_name),
                      last_name = COALESCE(NULLIF(EXCLUDED.last_name, ''), app_users.last_name),
                      is_admin = app_users.is_admin OR EXCLUDED.is_admin
                    """,
                    (user_id, email.lower().strip(), first_name, last_name, is_admin),
                )
            conn.commit()

    def is_admin_user(self, user_id: str, email: str) -> bool:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_admin FROM app_users WHERE id = %s::uuid",
                    (user_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return True
        admin_emails = [
            e.strip().lower()
            for e in (__import__("os").environ.get("ADMIN_EMAILS", "") or "").split(",")
            if e.strip()
        ]
        return email.lower().strip() in admin_emails

    def get_device_by_mac(self, mac: str) -> dict[str, Any] | None:
        mac_n = normalize_mac(mac)
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM devices WHERE mac = %s", (mac_n,))
                return cur.fetchone()

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,))
                return cur.fetchone()

    def list_active_device_ids(self) -> list[str]:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT device_id FROM devices
                    WHERE status IN ('active', 'unclaimed')
                    ORDER BY device_id
                    """
                )
                return [r[0] for r in cur.fetchall()]

    def user_owns_device(self, user_id: str, device_id: str) -> bool:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM user_stations
                    WHERE user_id = %s::uuid AND device_id = %s AND active = TRUE
                    """,
                    (user_id, device_id),
                )
                return cur.fetchone() is not None

    def list_user_devices(self, user_id: str) -> list[str]:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT device_id FROM user_stations
                    WHERE user_id = %s::uuid AND active = TRUE
                    ORDER BY device_id
                    """,
                    (user_id,),
                )
                return [r[0] for r in cur.fetchall()]

    def ownership_start(self, user_id: str, device_id: str) -> datetime | None:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT from_date FROM device_ownership_history
                    WHERE user_id = %s::uuid AND device_id = %s AND to_date IS NULL
                    ORDER BY from_date DESC LIMIT 1
                    """,
                    (user_id, device_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
                cur.execute(
                    """
                    SELECT created_at FROM user_stations
                    WHERE user_id = %s::uuid AND device_id = %s
                    """,
                    (user_id, device_id),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def _unique_device_id(self, mac: str) -> str:
        mac_n = normalize_mac(mac)
        for seq in range(20):
            did = generate_device_id(mac_n, seq=seq if seq else None)
            with _connect(self._url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM devices WHERE device_id = %s", (did,))
                    if not cur.fetchone():
                        return did
        raise RuntimeError("Impossible de generer device_id unique")

    def create_pending(
        self,
        mac: str,
        email: str,
        *,
        first_name: str,
        last_name: str,
        supabase_user_id: str | None,
    ) -> dict[str, Any]:
        mac_n = normalize_mac(mac)
        email_l = email.lower().strip()
        dev = self.get_device_by_mac(mac_n)
        if dev and dev.get("status") == "active" and dev.get("current_owner_id"):
            raise ValueError("Cette carte est deja assignee a un utilisateur.")
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id FROM pending_registrations
                    WHERE mac = %s AND status = 'pending'
                    """,
                    (mac_n,),
                )
                if cur.fetchone():
                    raise ValueError("Une demande est deja en attente pour cette carte.")
                cur.execute(
                    """
                    INSERT INTO pending_registrations
                      (mac, email, first_name, last_name, supabase_user_id, status)
                    VALUES (%s, %s, %s, %s, %s::uuid, 'pending')
                    RETURNING *
                    """,
                    (mac_n, email_l, first_name, last_name, supabase_user_id),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else {}

    def list_pending(self) -> list[dict[str, Any]]:
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM pending_registrations
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    """
                )
                return [dict(r) for r in cur.fetchall()]

    def list_devices(self) -> list[dict[str, Any]]:
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM devices ORDER BY updated_at DESC")
                return [dict(r) for r in cur.fetchall()]

    def approve_pending(self, pending_id: int, *, admin_user_id: str | None = None) -> dict[str, Any]:
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM pending_registrations WHERE id = %s AND status = 'pending'",
                    (pending_id,),
                )
                pending = cur.fetchone()
                if not pending:
                    raise ValueError("Demande introuvable ou deja traitee.")
                mac_n = pending["mac"]
                uid = pending.get("supabase_user_id")
                if not uid:
                    raise ValueError("Compte utilisateur manquant — inscription incomplete.")
            conn.commit()
        device_id = self._unique_device_id(mac_n)
        with _connect(self._url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM pending_registrations WHERE id = %s",
                    (pending_id,),
                )
                pending = cur.fetchone()
                if not pending or pending["status"] != "pending":
                    raise ValueError("Demande introuvable ou deja traitee.")
                uid = pending["supabase_user_id"]
                cur.execute(
                    """
                    INSERT INTO devices (mac, device_id, status, current_owner_id, label)
                    VALUES (%s, %s, 'active', %s::uuid, %s)
                    ON CONFLICT (mac) DO UPDATE SET
                      device_id = EXCLUDED.device_id,
                      status = 'active',
                      current_owner_id = EXCLUDED.current_owner_id,
                      unclaimed_at = NULL,
                      updated_at = NOW()
                    RETURNING *
                    """,
                    (mac_n, device_id, str(uid), device_id),
                )
                device = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO user_stations (user_id, device_id, active)
                    VALUES (%s::uuid, %s, TRUE)
                    ON CONFLICT (user_id, device_id) DO UPDATE SET active = TRUE
                    """,
                    (str(uid), device_id),
                )
                cur.execute(
                    """
                    INSERT INTO device_ownership_history (device_id, mac, user_id, reason)
                    VALUES (%s, %s, %s::uuid, %s)
                    """,
                    (device_id, mac_n, str(uid), "approved"),
                )
                cur.execute(
                    """
                    UPDATE pending_registrations
                    SET status = 'approved', resolved_at = NOW()
                    WHERE id = %s
                    """,
                    (pending_id,),
                )
            conn.commit()
        return {"device": dict(device) if device else {}, "device_id": device_id, "mac": mac_n}

    def reject_pending(self, pending_id: int, notes: str = "") -> None:
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_registrations
                    SET status = 'rejected', resolved_at = NOW(), notes = %s
                    WHERE id = %s AND status = 'pending'
                    """,
                    (notes, pending_id),
                )
            conn.commit()

    def transfer_device(self, user_id: str, device_id: str) -> None:
        if not self.user_owns_device(user_id, device_id):
            raise ValueError("Station non possedee par cet utilisateur.")
        dev = self.get_device(device_id)
        if not dev:
            raise ValueError("Station inconnue.")
        mac_n = dev["mac"]
        with _connect(self._url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE device_ownership_history
                    SET to_date = NOW(), reason = COALESCE(NULLIF(reason, ''), 'transfer')
                    WHERE device_id = %s AND user_id = %s::uuid AND to_date IS NULL
                    """,
                    (device_id, user_id),
                )
                cur.execute(
                    """
                    UPDATE user_stations SET active = FALSE
                    WHERE user_id = %s::uuid AND device_id = %s
                    """,
                    (user_id, device_id),
                )
                cur.execute(
                    """
                    UPDATE devices
                    SET status = 'unclaimed', current_owner_id = NULL,
                        unclaimed_at = NOW(), updated_at = NOW()
                    WHERE device_id = %s
                    """,
                    (device_id,),
                )
            conn.commit()

    def reactivate_for_user(self, user_id: str, mac: str) -> dict[str, Any]:
        mac_n = normalize_mac(mac)
        dev = self.get_device_by_mac(mac_n)
        if not dev:
            raise ValueError("Carte inconnue — contactez l'administrateur.")
        if dev.get("status") == "unclaimed":
            raise ValueError("Station liberee — faites une nouvelle inscription.")
        if str(dev.get("current_owner_id") or "") != user_id:
            raise ValueError("Cette carte n'appartient pas a votre compte.")
        return {"device_id": dev["device_id"], "mac": mac_n}

    def insert_telemetry(
        self,
        pg: PostgresStore,
        row: Any,
        device_id: str,
    ) -> None:
        """Insert avec device_id dynamique (contourne le device_id fixe du store)."""
        import psycopg2

        sql = f"""
        INSERT INTO {pg._table} (
          device_id, crop_name, soil_type, crop_age_days, temperature_c, humidity_pct, rainfall_mm,
          wind_speed_m_s, soil_moisture_pct, p_fraction, irrigate, irrigation_litres
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """
        vals = (
            device_id[:64],
            row.crop_name,
            row.soil_type,
            row.crop_age_days,
            round(row.temperature_c, 2),
            round(row.humidity_pct, 2),
            round(row.rainfall_mm, 2),
            round(row.wind_speed_m_s, 2),
            round(row.soil_moisture_pct, 2),
            round(row.p_fraction, 6) if row.p_fraction is not None else None,
            int(row.irrigate),
            round(row.irrigation_litres, 2),
        )
        with _connect(pg._database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, vals)
            conn.commit()

    def fetch_csv_for_user(
        self,
        pg: PostgresStore,
        csv_header: list[str],
        user_id: str,
        device_id: str,
        *,
        crop_name: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[list[Any]]:
        from db_mysql import telemetry_to_export_csv_row

        own_start = self.ownership_start(user_id, device_id)
        sql = f"""
        SELECT recorded_at, crop_name, soil_type, crop_age_days, temperature_c, humidity_pct, rainfall_mm,
               wind_speed_m_s, soil_moisture_pct, p_fraction, irrigate, irrigation_litres
        FROM {pg._table}
        WHERE device_id = %s
        {pg._sql_complete_rows_only()}
        """
        params: list[Any] = [device_id]
        if own_start:
            sql += " AND recorded_at >= %s"
            params.append(own_start)
        if crop_name:
            sql += " AND crop_name = %s"
            params.append(crop_name)
        now_utc = datetime.now(timezone.utc)
        if date_from is not None:
            sql += " AND recorded_at >= %s"
            params.append(datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc))
        if date_to is not None:
            from datetime import timedelta

            sql += " AND recorded_at < %s"
            params.append(
                datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            )
        elif date_from is not None:
            sql += " AND recorded_at <= %s"
            params.append(now_utc)
        sql += " ORDER BY recorded_at ASC, id ASC"
        out: list[list[Any]] = []
        with _connect(pg._database_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    ts = r["recorded_at"]
                    if hasattr(ts, "isoformat"):
                        recorded_iso = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        recorded_iso = str(ts)
                    t = pg._row_from_record(r)
                    out.append(telemetry_to_export_csv_row(recorded_iso, t, csv_header))
        return out


def tenant_store_from_env() -> TenantStore | None:
    import os

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return None
    return TenantStore(url)
