-- Crowd Safety Platform — PostgreSQL Schema
-- Apply with: psql crowd_safety -f schema.sql

CREATE TABLE IF NOT EXISTS zones (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    location_type   TEXT NOT NULL,
    safe_capacity   INTEGER NOT NULL DEFAULT 1000,
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crowd_readings (
    id              SERIAL PRIMARY KEY,
    zone_id         INTEGER REFERENCES zones(id) ON DELETE CASCADE,
    count           INTEGER NOT NULL,
    source          TEXT,
    status          TEXT NOT NULL,              -- 'Safe' | 'Warning' | 'Critical'
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_readings_zone_time
    ON crowd_readings (zone_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id              SERIAL PRIMARY KEY,
    zone_id         INTEGER REFERENCES zones(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,              -- 'Warning' | 'Critical'
    peak_count      INTEGER NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,                -- NULL while ongoing
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS action_log (
    id              SERIAL PRIMARY KEY,
    incident_id     INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
    action_text     TEXT NOT NULL,
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dispersion_plans (
    id              SERIAL PRIMARY KEY,
    zone_id         INTEGER REFERENCES zones(id) ON DELETE CASCADE,
    start_address   TEXT,
    end_address     TEXT,
    crowd_count     INTEGER,
    total_time_min  NUMERIC,
    plan_json       JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
