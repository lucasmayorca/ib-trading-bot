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
