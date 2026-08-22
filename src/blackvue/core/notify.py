"""
Optional email notification for unattended CLI crashes.

Christer: "Time to take a note about mailing when cli commands without
any tty crashes, since noone knows that if they dont read logs all the
time" - a `bv-generate`/`bv-scribe` run kicked off unattended (cron, a
scheduled task, an SSH session left running) that dies mid-way leaves
no signal anywhere except its own stdout/stderr and the persistent
logfile/history entry (core/joblog.py, core/history.py) - both purely
passive, nobody notices unless they go looking.

This module is deliberately global, not per-camera (unlike
core/camera_config.py's CameraConfig, which does have its own archive/
target/adapter fields): a crash isn't tied to which camera's archive
was being processed the way those paths are, and SMTP relay
credentials (a password) shouldn't need to be copy-pasted into every
single camera's own .cfg file just to get notified. One notify.toml
next to the camera configs, in the same default_config_dir(), covers
every bv-* command and every camera.

Voluntary/opt-in by design (Christer's own word) - notify.toml simply
not existing, or existing without an `email` key filled in, means "no
notification wanted," not an error; same convention as
core/resume.py's load_resume_state() and core/lock.py's
load_lock_manifest() treating a missing file as "nothing configured
yet," not a problem.

notify.toml layout:

    email = "you@example.com"
    smtp_host = "smtp.example.com"
    smtp_port = 587          # optional, default 587
    smtp_username = "..."    # optional - omit for an open/internal relay
    smtp_password = "..."    # optional
    use_tls = true           # optional, default true
    from_address = "..."     # optional, defaults to smtp_username or email

No dependency beyond the standard library (`smtplib`, `email.message`)
- same "no new service to sign up for" reasoning as every other
stdlib-only piece of this project.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import smtplib
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path


class NotifyConfigError(Exception):
    """Raised for a malformed notify.toml - not for "no file" or "no
    email set", both of which are the normal not-opted-in case (see
    load_notify_config())."""


@dataclass(frozen=True)
class NotifyConfig:
    """Resolved, ready-to-use notification settings - only ever
    returned by load_notify_config() once `email` and `smtp_host` are
    both confirmed present."""

    email: str
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    use_tls: bool = True
    from_address: str | None = None

    @property
    def sender(self) -> str:
        """The address mail appears to come from - an explicit
        from_address if given, else the SMTP login (most relays
        require the From to match the authenticated account anyway),
        else just the recipient itself as a last resort."""

        return self.from_address or self.smtp_username or self.email


def notify_config_path(config_dir: Path) -> Path:
    """Where the global notify settings live - a plain .toml
    extension (not the .cfg camera configs use) so it can never
    collide with a real camera id, no leading-dot hidden-file
    treatment needed since, unlike the cache dirs under this same
    directory, this file is meant to be opened and edited by hand."""

    return config_dir / "notify.toml"


def load_notify_config(config_dir: Path) -> NotifyConfig | None:
    """Load the optional crash-notification settings, or None if
    nothing is configured yet - either notify.toml doesn't exist at
    all, or it exists but has no `email` filled in (e.g. someone
    pre-filled the SMTP relay details and hasn't opted in yet). Either
    way, "no notification wanted" is the normal case, not an error.

    Raises NotifyConfigError only for a genuinely malformed file: bad
    TOML syntax, unreadable, or `email` set without the `smtp_host`
    a message could actually be sent through.
    """

    path = notify_config_path(config_dir)
    if not path.exists():
        return None

    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise NotifyConfigError(f"{path}: {exc}") from exc

    email = data.get("email")
    if not email:
        return None

    smtp_host = data.get("smtp_host")
    if not smtp_host:
        raise NotifyConfigError(
            f"{path}: 'email' is set but 'smtp_host' is missing"
        )

    return NotifyConfig(
        email=email,
        smtp_host=smtp_host,
        smtp_port=int(data.get("smtp_port", 587)),
        smtp_username=data.get("smtp_username"),
        smtp_password=data.get("smtp_password"),
        use_tls=bool(data.get("use_tls", True)),
        from_address=data.get("from_address"),
    )


def send_crash_notification(
    notify_config: NotifyConfig, subject: str, body: str
) -> bool:
    """Best-effort send - deliberately never raises. Returns True once
    the message has been handed off to the SMTP server, False on any
    failure (bad credentials, unreachable relay, timeout, ...).

    A notification going out is never allowed to affect the command it
    is reporting on - not its exit code, not whether it appears to
    have "succeeded" - so every failure mode here is swallowed rather
    than propagated. The command that crashed already has its own real
    error surfaced on stderr/the logfile; this is a best-effort extra
    signal on top, not a load-bearing part of the run.
    """

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = notify_config.sender
    message["To"] = notify_config.email
    message.set_content(body)

    try:
        with smtplib.SMTP(
            notify_config.smtp_host, notify_config.smtp_port, timeout=30
        ) as server:
            if notify_config.use_tls:
                server.starttls()
            if notify_config.smtp_username and notify_config.smtp_password:
                server.login(notify_config.smtp_username, notify_config.smtp_password)
            server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        return False
