"""
Stockage Supabase / PostgreSQL (DATABASE_URL) — memes champs que le CSV du pont.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

import psycopg2
import psycopg2.extras

from db_mysql import TelemetryData, telemetry_to_export_csv_row

_IDENTIFIER_SAFE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _running_on_render() -> bool:
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))


def _append_query_param(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = p.query or ""
    if f"{key}=" in q.lower():
        return url
    sep = "&" if q else ""
    new_query = f"{q}{sep}{key}={value}" if q else f"{key}={value}"
    return urlunparse(p._replace(query=new_query))


def _project_ref_from_host(host: str) -> str:
    h = host.lower()
    if h.startswith("db.") and h.endswith(".supabase.co"):
        return h[3 : -len(".supabase.co")]
    return ""


def _ensure_pooler_username(url: str) -> str:
    """Sur le pooler Supabase, l'utilisateur doit etre postgres.<project_ref>."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    ref = os.environ.get("SUPABASE_PROJECT_REF", "").strip() or _project_ref_from_host(host)
    if not ref:
        return url
    user = unquote(p.username or "")
    want = f"postgres.{ref}"
    if user == want:
        return url
    if user == "postgres" or (user.startswith("postgres.") and user != want):
        password = unquote(p.password or "")
        auth = quote(want, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        hostpart = p.hostname or ""
        if p.port:
            hostpart = f"{hostpart}:{p.port}"
        netloc = f"{auth}@{hostpart}"
        out = urlunparse(p._replace(netloc=netloc))
        print(f"[bridge] Supabase: utilisateur -> {want}")
        return out
    return url


def _rewrite_direct_to_pooler(url: str, pooler_host: str, pooler_port: int) -> str:
    """Reecrit db.xxx.supabase.co:5432 vers le pooler (IPv4) — host copie depuis Supabase Connect."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if "pooler.supabase.com" in host:
        return _ensure_pooler_username(url)
    if not host.startswith("db.") or not host.endswith(".supabase.co"):
        return url

    ref = _project_ref_from_host(host)
    user = unquote(p.username or "postgres")
    if user == "postgres" and ref:
        user = f"postgres.{ref}"
    password = unquote(p.password or "")
    path = p.path or "/postgres"

    auth = quote(user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    netloc = f"{auth}@{pooler_host}:{pooler_port}"
    out = urlunparse(p._replace(netloc=netloc, path=path))
    print(f"[bridge] Supabase pooler {pooler_host}:{pooler_port}")
    return _ensure_pooler_username(out)


def normalize_database_url(url: str) -> str:
    """
    SSL obligatoire.
    Sur Render : ne devine plus la region pooler (aws-0 vs aws-1).
    - Collez l'URI **Transaction** complete depuis Supabase Connect dans DATABASE_URL, ou
    - Definissez SUPABASE_POOLER_HOST (ex. aws-1-eu-central-1.pooler.supabase.com) + port 6543.
    """
    u = url.strip()
    if not u:
        return u
    u = _ensure_pooler_username(u)

    if _running_on_render() and "pooler.supabase.com" not in u.lower():
        pooler_host = os.environ.get("SUPABASE_POOLER_HOST", "").strip()
        if pooler_host:
            port = int(os.environ.get("SUPABASE_POOLER_PORT", "6543"))
            u = _rewrite_direct_to_pooler(u, pooler_host, port)
        else:
            print(
                "[bridge] Render + Supabase: copiez l'URI pooler (Transaction, port 6543) "
                "dans DATABASE_URL, ou definissez SUPABASE_POOLER_HOST depuis Supabase Connect."
            )

    if "sslmode=" not in u.lower():
        u = _append_query_param(u, "sslmode", "require")
    return u


class PostgresStore:
    def __init__(self, database_url: str, table: str = "irrigation_telemetry", device_id: str = "station01") -> None:
        if not database_url.strip():
            raise ValueError("DATABASE_URL vide")
        if not _IDENTIFIER_SAFE.match(table):
            raise ValueError(f"Nom de table invalide: {table!r}")
        self._database_url = normalize_database_url(database_url)
        self._table = table
        self._device_id = device_id[:64]

    def _connect(self) -> Any:
        return psycopg2.connect(self._database_url, connect_timeout=15)

    def ensure_table(self) -> None:
        t = self._table
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {t} (
          id BIGSERIAL PRIMARY KEY,
          recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          device_id VARCHAR(64) NOT NULL DEFAULT '',
          crop_name VARCHAR(64) NOT NULL DEFAULT '',
          soil_type VARCHAR(64) NOT NULL DEFAULT '',
          crop_age_days SMALLINT NULL,
          temperature_c DOUBLE PRECISION NULL,
          humidity_pct DOUBLE PRECISION NULL,
          rainfall_mm DOUBLE PRECISION NULL,
          wind_speed_m_s DOUBLE PRECISION NULL,
          soil_moisture_pct DOUBLE PRECISION NULL,
          p_fraction DOUBLE PRECISION NULL,
          irrigate SMALLINT NOT NULL DEFAULT 0,
          irrigation_litres DOUBLE PRECISION NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_{t}_recorded_at ON {t} (recorded_at);
        CREATE INDEX IF NOT EXISTS idx_{t}_device ON {t} (device_id, recorded_at);
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()

    def insert(self, row: TelemetryData, device_id: str | None = None) -> None:
        did = (device_id or self._device_id)[:64]
        sql = f"""
        INSERT INTO {self._table} (
          device_id, crop_name, soil_type, crop_age_days, temperature_c, humidity_pct, rainfall_mm,
          wind_speed_m_s, soil_moisture_pct, p_fraction, irrigate, irrigation_litres
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """
        vals = (
            did,
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
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, vals)
            conn.commit()

    @staticmethod
    def _sql_complete_rows_only() -> str:
        return """
          AND crop_name <> ''
          AND soil_type <> ''
          AND crop_age_days IS NOT NULL AND crop_age_days BETWEEN 1 AND 120
          AND p_fraction IS NOT NULL
          AND temperature_c IS NOT NULL
          AND humidity_pct IS NOT NULL
          AND rainfall_mm IS NOT NULL
          AND wind_speed_m_s IS NOT NULL
          AND soil_moisture_pct IS NOT NULL
          AND irrigation_litres IS NOT NULL
        """

    def _row_from_record(self, r: dict[str, Any]) -> TelemetryData:
        return TelemetryData(
            crop_name=r["crop_name"] or "",
            soil_type=r["soil_type"] or "",
            crop_age_days=r["crop_age_days"],
            manual_ok=True,
            temperature_c=float(r["temperature_c"]),
            humidity_pct=float(r["humidity_pct"]),
            rainfall_mm=float(r["rainfall_mm"]),
            wind_speed_m_s=float(r["wind_speed_m_s"]),
            soil_moisture_pct=float(r["soil_moisture_pct"]),
            p_fraction=float(r["p_fraction"]),
            irrigate=int(r["irrigate"] or 0),
            irrigation_litres=float(r["irrigation_litres"]),
        )

    def fetch_csv_rows_filtered(
        self,
        csv_header: list[str],
        *,
        device_id: str | None = None,
        recorded_after: datetime | None = None,
        crop_name: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[list[Any]]:
        did = (device_id or self._device_id)[:64]
        sql = f"""
        SELECT recorded_at, crop_name, soil_type, crop_age_days, temperature_c, humidity_pct, rainfall_mm,
               wind_speed_m_s, soil_moisture_pct, p_fraction, irrigate, irrigation_litres
        FROM {self._table}
        WHERE device_id = %s
        {self._sql_complete_rows_only()}
        """
        params: list[Any] = [did]
        if recorded_after is not None:
            sql += " AND recorded_at >= %s"
            params.append(recorded_after)
        if crop_name:
            sql += " AND crop_name = %s"
            params.append(crop_name)
        now_utc = datetime.now(timezone.utc)
        if date_from is not None:
            sql += " AND recorded_at >= %s"
            params.append(datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc))
        if date_to is not None:
            sql += " AND recorded_at < %s"
            params.append(
                datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            )
        elif date_from is not None:
            sql += " AND recorded_at <= %s"
            params.append(now_utc)
        sql += " ORDER BY recorded_at ASC, id ASC"

        out: list[list[Any]] = []
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    ts = r["recorded_at"]
                    if hasattr(ts, "isoformat"):
                        recorded_iso = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        recorded_iso = str(ts)
                    t = self._row_from_record(r)
                    out.append(telemetry_to_export_csv_row(recorded_iso, t, csv_header))
        return out

    def fetch_all_csv_rows(self, csv_header: list[str]) -> list[list[Any]]:
        return self.fetch_csv_rows_filtered(csv_header)


def postgres_store_from_env() -> PostgresStore | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return None
    table = os.environ.get("PG_TABLE", "irrigation_telemetry").strip() or "irrigation_telemetry"
    device_id = os.environ.get("DEVICE_ID", "station01").strip() or "station01"
    return PostgresStore(database_url=url, table=table, device_id=device_id)
