-- Multi-stations : devices, utilisateurs, provisioning, historique propriété
-- Exécuter dans l'éditeur SQL Supabase (ou via migration CLI).

CREATE TABLE IF NOT EXISTS devices (
  mac TEXT PRIMARY KEY,
  device_id VARCHAR(64) NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unclaimed', 'retired')),
  current_owner_id UUID NULL,
  label TEXT NOT NULL DEFAULT '',
  first_registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  unclaimed_at TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices (current_owner_id) WHERE current_owner_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_devices_status ON devices (status);

CREATE TABLE IF NOT EXISTS user_stations (
  user_id UUID NOT NULL,
  device_id VARCHAR(64) NOT NULL REFERENCES devices (device_id) ON DELETE CASCADE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_user_stations_device ON user_stations (device_id) WHERE active = TRUE;

CREATE TABLE IF NOT EXISTS device_ownership_history (
  id BIGSERIAL PRIMARY KEY,
  device_id VARCHAR(64) NOT NULL,
  mac TEXT NOT NULL,
  user_id UUID NOT NULL,
  from_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  to_date TIMESTAMPTZ NULL,
  reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ownership_device ON device_ownership_history (device_id, from_date);

CREATE TABLE IF NOT EXISTS pending_registrations (
  id BIGSERIAL PRIMARY KEY,
  mac TEXT NOT NULL,
  email TEXT NOT NULL,
  first_name TEXT NOT NULL DEFAULT '',
  last_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  supabase_user_id UUID NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ NULL,
  notes TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_mac_open
  ON pending_registrations (mac)
  WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS app_users (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  first_name TEXT NOT NULL DEFAULT '',
  last_name TEXT NOT NULL DEFAULT '',
  is_admin BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- irrigation_telemetry : colonne device_id déjà présente dans le pont ; index si besoin
CREATE INDEX IF NOT EXISTS idx_telemetry_device_time
  ON irrigation_telemetry (device_id, recorded_at DESC);
