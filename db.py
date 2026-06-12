"""
db.py — PostgreSQL connection + helper functions
----------------------------------------------------
Connection string from env var DATABASE_URL, defaulting to a local
trust-auth connection (no password needed on Mac by default).

To use a different host/user/password:
    export DATABASE_URL="postgresql://user:password@host:5432/crowd_safety"
"""

import os
import json
from typing import Optional
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://localhost:5432/crowd_safety"
)


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Zones ─────────────────────────────────────────────────────────────────────

def get_or_create_zone(name: str, location_type: str, safe_capacity: int,
                       lat: float = None, lon: float = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM zones WHERE name = %s", (name,))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                """INSERT INTO zones (name, location_type, safe_capacity, lat, lon)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (name, location_type, safe_capacity, lat, lon),
            )
            return cur.fetchone()[0]


def list_zones() -> 'list[dict]':
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM zones ORDER BY name")
            return [dict(r) for r in cur.fetchall()]


# ── Crowd readings ───────────────────────────────────────────────────────────

def log_reading(zone_id: int, count: int, status: str, source: str = "") -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO crowd_readings (zone_id, count, status, source)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (zone_id, count, status, source),
            )
            return cur.fetchone()[0]


def get_recent_readings(zone_id: int, limit: int = 60) -> 'list[dict]':
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT count, status, source, recorded_at
                   FROM crowd_readings
                   WHERE zone_id = %s
                   ORDER BY recorded_at DESC
                   LIMIT %s""",
                (zone_id, limit),
            )
            rows = cur.fetchall()
            return [dict(r) for r in reversed(rows)]  # chronological for charts


def get_latest_per_zone() -> 'list[dict]':
    """Latest reading for every zone — for a multi-zone dashboard."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT z.id AS zone_id, z.name, z.location_type, z.safe_capacity,
                       r.count, r.status, r.recorded_at
                FROM zones z
                LEFT JOIN LATERAL (
                    SELECT count, status, recorded_at
                    FROM crowd_readings
                    WHERE zone_id = z.id
                    ORDER BY recorded_at DESC
                    LIMIT 1
                ) r ON true
                ORDER BY z.name
            """)
            return [dict(r) for r in cur.fetchall()]


# ── Incidents ─────────────────────────────────────────────────────────────────

def get_open_incident(zone_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM incidents
                   WHERE zone_id = %s AND ended_at IS NULL
                   ORDER BY started_at DESC LIMIT 1""",
                (zone_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def open_incident(zone_id: int, status: str, peak_count: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO incidents (zone_id, status, peak_count)
                   VALUES (%s, %s, %s) RETURNING id""",
                (zone_id, status, peak_count),
            )
            return cur.fetchone()[0]


def update_incident_peak(incident_id: int, count: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE incidents SET peak_count = GREATEST(peak_count, %s)
                   WHERE id = %s""",
                (count, incident_id),
            )


def close_incident(incident_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE incidents SET ended_at = now() WHERE id = %s",
                (incident_id,),
            )


def log_action(incident_id: int, action_text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO action_log (incident_id, action_text) VALUES (%s, %s)",
                (incident_id, action_text),
            )


def get_incident_history(zone_id: int = None, limit: int = 20) -> 'list[dict]':
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if zone_id:
                cur.execute(
                    """SELECT i.*, z.name AS zone_name
                       FROM incidents i JOIN zones z ON z.id = i.zone_id
                       WHERE i.zone_id = %s
                       ORDER BY i.started_at DESC LIMIT %s""",
                    (zone_id, limit),
                )
            else:
                cur.execute(
                    """SELECT i.*, z.name AS zone_name
                       FROM incidents i JOIN zones z ON z.id = i.zone_id
                       ORDER BY i.started_at DESC LIMIT %s""",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]


# ── Dispersion plans ─────────────────────────────────────────────────────────

def save_dispersion_plan(zone_id: int, start_addr: str, end_addr: str,
                          crowd_count: int, plan: dict) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO dispersion_plans
                   (zone_id, start_address, end_address, crowd_count, total_time_min, plan_json)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (zone_id, start_addr, end_addr, crowd_count,
                 plan.get("total_time_min"), json.dumps(plan)),
            )
            return cur.fetchone()[0]


# ── Connection test ───────────────────────────────────────────────────────────

def test_connection() -> 'tuple[bool, str]':
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                return True, cur.fetchone()[0]
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    ok, msg = test_connection()
    print("✅ Connected!" if ok else "❌ Failed!")
    print(msg)
