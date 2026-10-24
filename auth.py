# auth.py
# Registration and login for Suaru: SQLite users + cookie sessions.
# No new dependencies: pbkdf2_hmac, sqlite3 and secrets from the stdlib.
#
# Passwords: PBKDF2-HMAC-SHA256, per-user random salt, 390k iterations.
# Sessions: a random token lives in an HttpOnly cookie; only its SHA-256
# digest is stored in the DB, so a leaked DB does not leak live sessions.
#
# NOTE for Render free plan: there is no persistent disk, so suaru.db is
# wiped on every redeploy. Tables are re-created on startup and users just
# re-register; nothing else in the app depends on the file.

import hashlib
import pathlib
import secrets
import sqlite3
import time

DB_PATH = pathlib.Path(__file__).parent / "suaru.db"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # a week is plenty for a demo
PBKDF2_ITERATIONS = 390_000


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they do not exist; safe to call on every start."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()


def register_user(username: str, password: str) -> int:
    """Create a user; raises ValueError with a Russian message on bad input."""
    username = (username or "").strip().lower()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").isalnum():
        raise ValueError("Логин: 3–32 символа, только буквы, цифры и _")
    if len(password or "") < 6:
        raise ValueError("Пароль: минимум 6 символов")
    salt = secrets.token_hex(16)
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
                (username, _hash_password(password, salt), salt),
            )
    except sqlite3.IntegrityError:
        raise ValueError("Такой логин уже существует")
    return cur.lastrowid


def verify_user(username: str, password: str):
    """Return the user id if the credentials match, else None."""
    username = (username or "").strip().lower()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row and secrets.compare_digest(
        row["password_hash"], _hash_password(password or "", row["salt"])
    ):
        return row["id"]
    return None


def create_session(user_id: int) -> str:
    """Return a fresh session token and store its digest (with lazy cleanup)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), user_id, now + SESSION_TTL_SECONDS),
        )
    return token


def get_user_id(token: str):
    """Return the user id for a live session token, else None."""
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?", (digest,)
        ).fetchone()
    if row and row["expires_at"] > time.time():
        return row["user_id"]
    return None


def delete_session(token: str):
    if not token:
        return
    with _connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )


def get_username(user_id: int) -> str:
    with _connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else ""
