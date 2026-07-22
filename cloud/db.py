"""
Database layer — PostgreSQL via psycopg2.
Auto-creates tables on first run.
"""

import os
import secrets
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/ib_trading")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          SERIAL PRIMARY KEY,
                email       TEXT UNIQUE NOT NULL,
                password    TEXT NOT NULL,
                bridge_token TEXT UNIQUE NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # IB Flex Web Service credentials — lets us pull a user's FULL trade
        # history (reqExecutions only ever returns the current TWS session's
        # fills, there's no way around that through the live API).
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS flex_token TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS flex_query_id TEXT")

        # Per-user live-store snapshot. The in-memory user_data store in
        # cloud/server.py is wiped on every process restart/redeploy (Railway
        # cycles the container). Persisting a snapshot here lets a restarted
        # server serve last-known scan/ETF/portfolio data immediately instead
        # of blank tabs until the bridge reconnects and re-scans (~minutes).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_store (
                user_id    INTEGER PRIMARY KEY,
                data       JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # User feedback / platform rating. Collected from the "Tu Opinion" tab
        # so we can gather improvement ideas and satisfaction from real users.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER,
                email      TEXT,
                rating     INTEGER,
                category   TEXT,
                message    TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)


def create_user(email, hashed_password):
    token = secrets.token_urlsafe(32)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, bridge_token) VALUES (%s, %s, %s) RETURNING id, bridge_token",
            (email, hashed_password, token),
        )
        return cur.fetchone()


def get_user_by_email(email):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()


def get_user_by_id(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def get_user_by_token(token):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE bridge_token = %s", (token,))
        return cur.fetchone()


def regenerate_token(user_id):
    token = secrets.token_urlsafe(32)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET bridge_token = %s WHERE id = %s RETURNING bridge_token",
            (token, user_id),
        )
        return cur.fetchone()["bridge_token"]


def save_flex_config(user_id, flex_token, flex_query_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET flex_token = %s, flex_query_id = %s WHERE id = %s",
            (flex_token, flex_query_id, user_id),
        )


def get_flex_config(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT flex_token, flex_query_id FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return (row["flex_token"], row["flex_query_id"]) if row else (None, None)


def save_user_store(user_id, data_json):
    """Upsert a user's live-store snapshot. `data_json` is a JSON string
    (already NaN/Inf-cleaned by the caller) cast to jsonb."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_store (user_id, data, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
            """,
            (user_id, data_json),
        )


def save_feedback(user_id, email, rating, category, message):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO feedback (user_id, email, rating, category, message)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (user_id, email, rating, category, message),
        )
        return cur.fetchone()


def get_all_feedback(limit=300):
    """Newest first — for the owner-only review panel."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, email, rating, category, message, created_at
            FROM feedback ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def load_all_user_stores():
    """Return [(user_id, data_dict), ...] to restore the in-memory store on
    boot. RealDictCursor parses the jsonb column back into a Python dict."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, data FROM user_store")
        return [(row["user_id"], row["data"]) for row in cur.fetchall()]
