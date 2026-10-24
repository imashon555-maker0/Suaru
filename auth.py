# auth.py
# Registration, login and email verification for Suaru: SQLite only.
# No new dependencies: pbkdf2_hmac, sqlite3, secrets and re from the stdlib.
#
# Passwords: PBKDF2-HMAC-SHA256, per-user random salt, 390k iterations.
# Sessions: a random token lives in an HttpOnly cookie; only its SHA-256
# digest is stored in the DB, so a leaked DB does not leak live sessions.
# Email verification: single-use random token, stored hashed, valid 24h.
#
# NOTE for Render free plan: there is no persistent disk, so suaru.db is
# wiped on every redeploy. Tables are re-created on startup and users just
# re-register; nothing else in the app depends on the file.

import hashlib
import pathlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = pathlib.Path(__file__).parent / "suaru.db"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # a week is plenty for a demo
EMAIL_TOKEN_TTL_SECONDS = 24 * 3600
PBKDF2_ITERATIONS = 390_000
PASSWORD_MIN_LENGTH = 8

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class AuthError(ValueError):
    """Validation error with a message safe to show to the user."""


@contextmanager
def _connect():
    """One connection per block: the transaction commits (or rolls back) and
    the file handle is always closed, so requests cannot leak connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    """Create tables if missing and migrate older databases in place."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                email TEXT,
                email_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )

        # Databases created before email support lack these columns.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "email" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "email_verified" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")

        # One account per email, ignoring accounts without an email (legacy).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
            "ON users(email) WHERE email IS NOT NULL"
        )


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()


def _clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address")
    return email


def _check_password(password: str):
    if len(password or "") < PASSWORD_MIN_LENGTH:
        raise AuthError(f"Password: at least {PASSWORD_MIN_LENGTH} characters")


def register_user(username: str, password: str, email: str) -> int:
    """Create a user; raises AuthError with a user-safe message."""
    username = (username or "").strip().lower()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").isalnum():
        raise AuthError("Username: 3–32 characters, letters, digits and _")
    _check_password(password)
    email = _clean_email(email)
    salt = secrets.token_hex(16)
    try:
        with _connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise AuthError("This email is already registered")
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt, email) VALUES (?, ?, ?, ?)",
                (username, _hash_password(password, salt), salt, email),
            )
    except sqlite3.IntegrityError as exc:
        # Unique-constraint guard: username or email was taken between the checks.
        message = str(exc).lower()
        if "username" in message:
            raise AuthError("This username is already taken")
        if "email" in message:
            raise AuthError("This email is already registered")
        raise AuthError("This username or email is already taken")
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


def change_password(user_id: int, new_password: str):
    _check_password(new_password)
    salt = secrets.token_hex(16)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (_hash_password(new_password, salt), salt, user_id),
        )


def get_profile(user_id: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, email, email_verified FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "username": row["username"],
        "email": row["email"],
        "email_verified": bool(row["email_verified"]),
    }


def get_username(user_id: int) -> str:
    profile = get_profile(user_id)
    return profile["username"] if profile else ""


def is_email_verified(user_id: int) -> bool:
    profile = get_profile(user_id)
    return bool(profile and profile["email_verified"])


def create_email_token(user_id: int) -> str:
    """Issue a single-use verification token for the user's email."""
    profile = get_profile(user_id)
    if not profile or not profile["email"]:
        raise AuthError("No email on file")
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _connect() as conn:
        conn.execute("DELETE FROM email_tokens WHERE user_id = ? OR expires_at < ?", (user_id, now))
        conn.execute(
            "INSERT INTO email_tokens (token_hash, user_id, email, expires_at) VALUES (?, ?, ?, ?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                user_id,
                profile["email"],
                now + EMAIL_TOKEN_TTL_SECONDS,
            ),
        )
    return token


def consume_email_token(token: str):
    """Mark the email verified and return the user id, or None if invalid."""
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, email, expires_at FROM email_tokens WHERE token_hash = ?", (digest,)
        ).fetchone()
        if not row or row["expires_at"] < now:
            return None
        conn.execute(
            "UPDATE users SET email = ?, email_verified = 1 WHERE id = ?",
            (row["email"], row["user_id"]),
        )
        conn.execute("DELETE FROM email_tokens WHERE token_hash = ?", (digest,))
    return row["user_id"]


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


def delete_sessions_for_user(user_id: int):
    """Revoke every session of a user (used after a password change)."""
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
