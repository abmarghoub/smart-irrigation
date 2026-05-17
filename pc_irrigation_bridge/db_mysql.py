"""
Stockage MySQL des lignes de telemetrie (memes champs que le CSV du pont).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pymysql

_IDENTIFIER_SAFE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


@dataclass
class TelemetryData:
    crop_name: str
    soil_type: str
    crop_age_days: int | None
    manual_ok: bool
    temperature_c: float
    humidity_pct: float
    rainfall_mm: float
    wind_speed_m_s: float
    soil_moisture_pct: float
    p_fraction: float | None
    irrigate: int
    irrigation_litres: float


def _fmt2(x: Any) -> str:
    if x == "" or x is None:
        return ""
    try:
        return f"{round(float(x), 2):.2f}"
    except (TypeError, ValueError):
        return ""


def extract_telemetry_from_mqtt(payload: dict[str, Any]) -> TelemetryData:
    s = payload.get("sensors") or {}
    w = payload.get("weather") or {}
    m = payload.get("model_inputs") or {}
    d = payload.get("decision") or {}
    man = payload.get("manual") or {}
    manual_ok = bool(man.get("confirmed"))

    if manual_ok:
        crop_name = str(m.get("crop") or "").strip()
        soil_type = str(m.get("soil") or "").strip()
        try:
            age = int(man.get("crop_age_days") or 0)
        except (TypeError, ValueError):
            crop_age_days = None
        else:
            crop_age_days = age if age >= 1 else None
        _tm = m.get("temp_c")
        temp_c = float(_tm if _tm is not None else s.get("temp_c") or 0)
        _hm = m.get("rh_pct")
        rh = float(_hm if _hm is not None else s.get("rh_pct") or 0)
        rain = float(m.get("rain_mm") or 0)
        _sm = m.get("soil_pct")
        soil_m = float(_sm if _sm is not None else s.get("soil_pct") or 0)
        p_frac: float | None = float(d.get("p") or 0)
        irr = 1 if d.get("irrigate") in (1, True) else 0
        vol_l = float(d.get("volume_model_l") or 0)
    else:
        crop_name = ""
        soil_type = ""
        crop_age_days = None
        temp_c = float(s.get("temp_c") or 0)
        rh = float(s.get("rh_pct") or 0)
        rain = float(m.get("rain_mm") or 0) if m.get("rain_mm") is not None else 0.0
        soil_m = float(s.get("soil_pct") or 0)
        p_frac = None
        irr = 0
        vol_l = 0.0

    wind_ms = float(w.get("wind_ms") or 0)

    return TelemetryData(
        crop_name=crop_name,
        soil_type=soil_type,
        crop_age_days=crop_age_days,
        manual_ok=manual_ok,
        temperature_c=temp_c,
        humidity_pct=rh,
        rainfall_mm=rain,
        wind_speed_m_s=wind_ms,
        soil_moisture_pct=soil_m,
        p_fraction=p_frac,
        irrigate=irr,
        irrigation_litres=vol_l,
    )


def telemetry_to_csv_row(t: TelemetryData, csv_header: list[str]) -> list[Any]:
    """Liste de valeurs alignee sur csv_header (ordre du pont)."""
    age_cell = t.crop_age_days if t.crop_age_days is not None else ""
    row_map = {
        "crop_name": t.crop_name,
        "soil_type": t.soil_type,
        "crop_age_days": age_cell,
        "temperature_C": _fmt2(t.temperature_c),
        "humidity_%": _fmt2(t.humidity_pct),
        "rainfall_mm": _fmt2(t.rainfall_mm),
        "wind_speed_m_s": _fmt2(t.wind_speed_m_s),
        "soil_moisture_%": _fmt2(t.soil_moisture_pct),
        "p_fraction": _fmt2(t.p_fraction) if t.manual_ok else "",
        "irrigate": t.irrigate,
        "irrigation_litres": _fmt2(t.irrigation_litres),
    }
    return [row_map[c] for c in csv_header]


class MySQLStore:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        table: str,
    ) -> None:
        if not _IDENTIFIER_SAFE.match(database):
            raise ValueError(f"Nom de base MySQL invalide: {database!r} (lettres, chiffres, underscore, max 64)")
        if not _IDENTIFIER_SAFE.match(table):
            raise ValueError(f"Nom de table MySQL invalide: {table!r}")
        self._database = database
        self._table = table
        self._cfg: dict[str, Any] = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
            "charset": "utf8mb4",
            "autocommit": True,
        }

    def _connect_server_only(self) -> Any:
        """Connexion sans base selectionnee (pour CREATE DATABASE)."""
        d = {k: v for k, v in self._cfg.items() if k != "database"}
        return pymysql.connect(**d)

    def _connect(self) -> Any:
        return pymysql.connect(**self._cfg)

    def _ensure_database_exists(self) -> None:
        sql = (
            f"CREATE DATABASE IF NOT EXISTS `{self._database}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        with self._connect_server_only() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def ensure_table(self) -> None:
        """Cree la base si besoin, puis la table de telemetrie."""
        self._ensure_database_exists()
        t = self._table
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{t}` (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          recorded_at DATETIME(0) NOT NULL DEFAULT CURRENT_TIMESTAMP,
          crop_name VARCHAR(64) NOT NULL DEFAULT '',
          soil_type VARCHAR(64) NOT NULL DEFAULT '',
          crop_age_days SMALLINT UNSIGNED NULL,
          temperature_c DECIMAL(6,2) NULL,
          humidity_pct DECIMAL(6,2) NULL,
          rainfall_mm DECIMAL(8,2) NULL,
          wind_speed_m_s DECIMAL(8,2) NULL,
          soil_moisture_pct DECIMAL(6,2) NULL,
          p_fraction DECIMAL(10,6) NULL,
          irrigate TINYINT(1) NOT NULL DEFAULT 0,
          irrigation_litres DECIMAL(12,2) NOT NULL DEFAULT 0.00,
          KEY idx_recorded_at (recorded_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)

    def insert(self, row: TelemetryData) -> None:
        sql = f"""
        INSERT INTO `{self._table}` (
          crop_name, soil_type, crop_age_days, temperature_c, humidity_pct, rainfall_mm,
          wind_speed_m_s, soil_moisture_pct, p_fraction, irrigate, irrigation_litres
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """
        vals = (
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
