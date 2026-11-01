"""Local multi-user review state, kept separate from immutable releases."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .storage import digest


ROLES = {"admin", "reviewer", "viewer"}
DECISIONS = {"pending", "approved", "needs_changes", "rejected"}
FLAGS = {"conflict", "inconsistent", "stale"}
USERNAME = re.compile(r"^[a-zA-Z0-9_.-]{3,40}$")


class ReviewConflict(Exception):
    """Another reviewer changed this decision after it was loaded."""


class ReviewStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / ".iltkb" / "management.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL, role TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                  csrf TEXT NOT NULL, expires_at INTEGER NOT NULL,
                  FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS reviews (
                  release TEXT NOT NULL, claim_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, decision TEXT NOT NULL,
                  flags TEXT NOT NULL, note TEXT NOT NULL,
                  reviewer_id INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                  version INTEGER NOT NULL DEFAULT 1,
                  carried_from TEXT,
                  PRIMARY KEY(release, claim_id));
                CREATE TABLE IF NOT EXISTS audit (
                  id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL,
                  action TEXT NOT NULL, target TEXT NOT NULL,
                  detail TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_reviews_release ON reviews(release);
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit(created_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(reviews)")}
            if "version" not in columns:
                db.execute("ALTER TABLE reviews ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "carried_from" not in columns:
                db.execute("ALTER TABLE reviews ADD COLUMN carried_from TEXT")
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def enabled(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    @staticmethod
    def _password(password: str, salt: bytes | None = None) -> str:
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("Password must contain 12–1024 characters")
        salt = salt or secrets.token_bytes(16)
        key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return salt.hex() + ":" + key.hex()

    def add_user(self, username: str, password: str, role: str, actor_id: int | None = None) -> dict:
        if not USERNAME.fullmatch(username) or role not in ROLES:
            raise ValueError("Invalid username or role")
        if self.enabled() and actor_id is None:
            raise PermissionError("Only an admin can add users after setup")
        hashed = self._password(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if actor_id is None and db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise PermissionError("Setup already completed")
            if actor_id is not None:
                actor = db.execute("SELECT role,active FROM users WHERE id=?", (actor_id,)).fetchone()
                if not actor or actor["role"] != "admin" or not actor["active"]:
                    raise PermissionError("Admin role required")
            try:
                cursor = db.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                                    (username, hashed, role, int(time.time())))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Username already exists") from exc
            db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor_id or cursor.lastrowid, "user_created", username, json.dumps({"role": role}), int(time.time())))
        return {"username": username, "role": role}

    def authenticate(self, username: str, password: str) -> dict | None:
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 40 or len(password) > 1024:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if not row:
            # Constant-work failure path, including unknown usernames.
            self._password(password if len(password) >= 12 else "invalid-password", bytes(16))
            return None
        try:
            salt, expected = row["password_hash"].split(":", 1)
            actual = self._password(password if len(password) >= 12 else "invalid-password", bytes.fromhex(salt)).split(":", 1)[1]
        except ValueError:
            return None
        if not hmac.compare_digest(actual, expected):
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def create_session(self, user_id: int) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                       (digest(token), user_id, csrf, int(time.time()) + 12*3600))
        return token, csrf

    def session(self, token: str) -> dict | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute("SELECT u.id,u.username,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id "
                             "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                             (digest(token), int(time.time()))).fetchone()
        return dict(row) if row else None

    def end_session(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def users(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,username,role,active,created_at FROM users ORDER BY username")]

    def set_user(self, user_id: int, role: str, active: bool, actor_id: int) -> None:
        if role not in ROLES or type(active) is not bool:
            raise ValueError("Invalid role or active state")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor = db.execute("SELECT role,active FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor or actor["role"] != "admin" or not actor["active"]:
                raise PermissionError("Admin role required")
            current = db.execute("SELECT id,role,active,username FROM users WHERE id=?", (user_id,)).fetchone()
            if not current:
                raise ValueError("Unknown user")
            if user_id == actor_id and (role != "admin" or not active):
                raise ValueError("Cannot remove your own admin access")
            if current["role"] == "admin" and current["active"] and (role != "admin" or not active):
                count = db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError("At least one active admin is required")
            db.execute("UPDATE users SET role=?,active=? WHERE id=?", (role, int(active), user_id))
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor_id, "user_updated", current["username"], json.dumps({"role": role, "active": active}), int(time.time())))

    @staticmethod
    def fingerprint(claim: dict) -> str:
        return digest({key: claim[key] for key in
                       ("text", "kind", "evidence", "conditions", "reasoning", "independence", "support_sources")})

    def reviews(self, release: str, db=None) -> dict[str, dict]:
        if db is None:
            with self.connect() as connection:
                return self.reviews(release, connection)
        rows = db.execute("SELECT r.*,u.username FROM reviews r JOIN users u ON u.id=r.reviewer_id WHERE release=?", (release,))
        return {row["claim_id"]: {**dict(row), "flags": json.loads(row["flags"])} for row in rows}

    def previous_reviews(self, release: str, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            return [{**dict(row), "flags": json.loads(row["flags"])} for row in db.execute(
                "SELECT r.release,r.claim_id,r.decision,r.flags,r.note,r.updated_at,u.username "
                "FROM reviews r JOIN users u ON u.id=r.reviewer_id WHERE r.release<>? "
                "ORDER BY r.updated_at DESC LIMIT ?", (release, limit))]

    def save_review(self, release: str, claim: dict, decision: str, flags: list[str], note: str,
                    actor_id: int, expected_version: int) -> dict:
        if decision not in DECISIONS or not isinstance(flags, list) or any(flag not in FLAGS for flag in flags):
            raise ValueError("Invalid decision or flags")
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError("Review note is too long")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Review version must be a nonnegative integer")
        flags = sorted(set(flags))
        now = int(time.time())
        fingerprint = self.fingerprint(claim)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT version FROM reviews WHERE release=? AND claim_id=?",
                                 (release, claim["id"])).fetchone()
            if (current["version"] if current else 0) != expected_version:
                raise ReviewConflict("Review changed; reload before saving")
            version = expected_version + 1
            db.execute("INSERT INTO reviews(release,claim_id,fingerprint,decision,flags,note,reviewer_id,updated_at,version) "
                       "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(release,claim_id) DO UPDATE SET "
                       "fingerprint=excluded.fingerprint,decision=excluded.decision,flags=excluded.flags,"
                       "note=excluded.note,reviewer_id=excluded.reviewer_id,updated_at=excluded.updated_at,"
                       "version=excluded.version,carried_from=NULL",
                       (release, claim["id"], fingerprint, decision, json.dumps(flags), note, actor_id, now, version))
            db.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor_id, "claim_reviewed", claim["id"], json.dumps({"release": release, "decision": decision, "flags": flags}), now))
        return {"release": release, "claim_id": claim["id"], "fingerprint": fingerprint,
                "decision": decision, "flags": flags, "note": note, "reviewer_id": actor_id,
                "updated_at": now, "version": version}

    def audit(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT a.id,u.username,a.action,a.target,a.detail,a.created_at "
                                                   "FROM audit a JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT ?", (limit,))]
