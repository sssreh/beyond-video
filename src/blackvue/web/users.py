"""
User accounts for bv-web: username, password hash, role.

Modeled on core/camera_config.py's TOML-file pattern (a plain,
hand-editable file under ~/.config/beyond-video by default), not a
database - there are only ever going to be a handful of accounts
(Christer plus a few family members), so a file is simpler to reason
about, back up, and inspect than standing up sqlite for it.

Passwords are hashed with the stdlib's own hashlib.pbkdf2_hmac +
secrets, the same minimal-dependency choice already made elsewhere in
this codebase (e.g. no numpy anywhere despite early prototyping using
it) - this avoids adding passlib/bcrypt as a dependency just for
this. PBKDF2-HMAC-SHA256 at a six-figure iteration count is a
reasonable, audited, stdlib-only choice for this threat model (a
small self-hosted app, not a public multi-tenant service).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

# OWASP's 2023 guidance for PBKDF2-HMAC-SHA256 is >= 600,000
# iterations - re-check this number occasionally as hardware gets
# faster, same as any password-hashing parameter.
PBKDF2_ITERATIONS = 600_000
PBKDF2_ALGORITHM = "sha256"
SALT_BYTES = 16

# "owner" is Christer: browse/watch, plus (once built) trigger
# download/generate/export. "viewer" is everyone else invited in for
# now: browse/watch only. A third "manager" role is anticipated
# (Christer: "maybe later i let simeone in as a manager too") but its
# permissions haven't been decided yet, so it isn't added here - add
# it to this tuple and teach auth.py what it's allowed to do once
# that's actually decided.
ROLES = ("owner", "viewer")


class UsersConfigError(Exception):
    """Raised when the users file can't be loaded or is invalid, or a
    user-account operation itself is invalid (bad role, duplicate
    username, empty password)."""


def default_users_path() -> Path:
    """Return the default path bv-web's accounts file lives at."""

    return Path.home() / ".config" / "beyond-video" / "web-users.cfg"


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise UsersConfigError(f"role must be one of {ROLES!r}: {role!r}")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a self-describing hash string:
    'pbkdf2_sha256$iterations$salt_hex$hash_hex' - storing the
    algorithm and iteration count alongside the hash itself (rather
    than assuming today's PBKDF2_ITERATIONS forever) means
    PBKDF2_ITERATIONS can be raised later without invalidating
    existing accounts' stored hashes."""

    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)

    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM, password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )

    return (
        f"pbkdf2_{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """Check `password` against a hash string produced by
    hash_password(). Never raises - a malformed `stored` value (e.g.
    a hand-edited users file) is just treated as "doesn't match"
    rather than crashing the login attempt."""

    try:
        algorithm_field, iterations_str, salt_hex, hash_hex = stored.split("$")
        algorithm = algorithm_field.removeprefix("pbkdf2_")
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        algorithm, password.encode("utf-8"), salt, iterations
    )
    # Constant-time compare - a naive `==` would leak how many
    # leading bytes matched via response timing.
    return hmac.compare_digest(actual, expected)


@dataclass
class User:
    """One account: who they are, their stored password hash, and
    their role. Never holds a plaintext password."""

    username: str
    password_hash: str
    role: str


@dataclass
class UsersConfig:
    """All accounts loaded from one users file. Mutating methods
    (add_user/remove_user) only change the in-memory dict - call
    save() to write it back out."""

    path: Path
    users: dict[str, User] = field(default_factory=dict)

    def get(self, username: str) -> User | None:
        return self.users.get(username)

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the matching User if `username`/`password` are
        correct, else None - deliberately the same None for "no such
        user" and "wrong password", so a login form can't be used to
        enumerate valid usernames."""

        user = self.users.get(username)
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def add_user(self, username: str, password: str, role: str) -> User:
        validate_role(role)

        if not username:
            raise UsersConfigError("username must not be empty")
        if username in self.users:
            raise UsersConfigError(f"user already exists: {username!r}")
        if not password:
            raise UsersConfigError("password must not be empty")

        user = User(
            username=username, password_hash=hash_password(password), role=role
        )
        self.users[username] = user
        return user

    def remove_user(self, username: str) -> None:
        if username not in self.users:
            raise UsersConfigError(f"no such user: {username!r}")
        del self.users[username]

    def save(self) -> None:
        save_users_config(self.path, self)


def load_users_config(path: Path) -> UsersConfig:
    """Load a users file. A missing file is not an error - it's read
    as zero accounts, same as a freshly-installed bv-web before the
    first `bv-web adduser` has run."""

    if not path.exists():
        return UsersConfig(path=path, users={})

    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UsersConfigError(f"{path}: {exc}") from exc

    users: dict[str, User] = {}

    for item in data.get("user", []):
        try:
            username = item["username"]
            password_hash = item["password_hash"]
            role = item["role"]
        except KeyError as exc:
            raise UsersConfigError(
                f"{path}: user entry missing required key {exc}"
            ) from exc

        validate_role(role)
        users[username] = User(
            username=username, password_hash=password_hash, role=role
        )

    return UsersConfig(path=path, users=users)


def _toml_string(value: str) -> str:
    """Render a TOML basic string, keeping UTF-8 text literal - same
    helper as core/camera_config.py's."""

    return json.dumps(value, ensure_ascii=False)


def save_users_config(path: Path, config: UsersConfig) -> None:
    """Save a users file. Plain TOML, meant to be hand-editable (e.g.
    to hand-fix a role, or delete an account, without going through
    `bv-web adduser`)."""

    lines: list[str] = []

    for user in config.users.values():
        lines.append("[[user]]")
        lines.append(f"username = {_toml_string(user.username)}")
        lines.append(f"password_hash = {_toml_string(user.password_hash)}")
        lines.append(f"role = {_toml_string(user.role)}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
