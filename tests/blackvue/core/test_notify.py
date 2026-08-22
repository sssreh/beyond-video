"""
Tests for core/notify.py - the voluntary, global crash-notification
email settings (see that module's own docstring for the full "why
global, why opt-in" design reasoning).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import smtplib

import pytest

from blackvue.core.notify import NotifyConfig
from blackvue.core.notify import NotifyConfigError
from blackvue.core.notify import load_notify_config
from blackvue.core.notify import notify_config_path
from blackvue.core.notify import send_crash_notification


# ---------------------------------------------------------------------------
# load_notify_config
# ---------------------------------------------------------------------------


def test_load_notify_config_returns_none_when_file_is_missing(tmp_path):
    assert load_notify_config(tmp_path) is None


def test_load_notify_config_returns_none_when_email_is_not_set(tmp_path):
    # Pre-filled relay details but no opt-in email yet - still "not
    # configured", not an error.
    notify_config_path(tmp_path).write_text(
        'smtp_host = "smtp.example.com"\n'
    )

    assert load_notify_config(tmp_path) is None


def test_load_notify_config_raises_when_email_set_without_smtp_host(tmp_path):
    notify_config_path(tmp_path).write_text('email = "me@example.com"\n')

    with pytest.raises(NotifyConfigError, match="smtp_host"):
        load_notify_config(tmp_path)


def test_load_notify_config_raises_on_malformed_toml(tmp_path):
    notify_config_path(tmp_path).write_text("this is not [ toml")

    with pytest.raises(NotifyConfigError):
        load_notify_config(tmp_path)


def test_load_notify_config_reads_full_settings(tmp_path):
    notify_config_path(tmp_path).write_text(
        'email = "me@example.com"\n'
        'smtp_host = "smtp.example.com"\n'
        "smtp_port = 465\n"
        'smtp_username = "me@example.com"\n'
        'smtp_password = "hunter2"\n'
        "use_tls = false\n"
        'from_address = "beyond-video@example.com"\n'
    )

    config = load_notify_config(tmp_path)

    assert config == NotifyConfig(
        email="me@example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="me@example.com",
        smtp_password="hunter2",
        use_tls=False,
        from_address="beyond-video@example.com",
    )


def test_load_notify_config_applies_defaults_for_optional_fields(tmp_path):
    notify_config_path(tmp_path).write_text(
        'email = "me@example.com"\nsmtp_host = "smtp.example.com"\n'
    )

    config = load_notify_config(tmp_path)

    assert config.smtp_port == 587
    assert config.smtp_username is None
    assert config.smtp_password is None
    assert config.use_tls is True
    assert config.from_address is None


# ---------------------------------------------------------------------------
# NotifyConfig.sender
# ---------------------------------------------------------------------------


def test_sender_prefers_explicit_from_address():
    config = NotifyConfig(
        email="me@example.com",
        smtp_host="smtp.example.com",
        smtp_username="login@example.com",
        from_address="beyond-video@example.com",
    )
    assert config.sender == "beyond-video@example.com"


def test_sender_falls_back_to_smtp_username():
    config = NotifyConfig(
        email="me@example.com",
        smtp_host="smtp.example.com",
        smtp_username="login@example.com",
    )
    assert config.sender == "login@example.com"


def test_sender_falls_back_to_email_as_last_resort():
    config = NotifyConfig(email="me@example.com", smtp_host="smtp.example.com")
    assert config.sender == "me@example.com"


# ---------------------------------------------------------------------------
# send_crash_notification
# ---------------------------------------------------------------------------


class _FakeSmtp:
    """Records calls instead of touching a real network - a context
    manager since send_crash_notification() uses smtplib.SMTP as one."""

    instances: list["_FakeSmtp"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args = None
        self.sent_message = None
        _FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent_message = message


@pytest.fixture(autouse=True)
def _reset_fake_smtp_instances():
    _FakeSmtp.instances.clear()
    yield
    _FakeSmtp.instances.clear()


def test_send_crash_notification_sends_via_smtp_with_tls_and_login(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    config = NotifyConfig(
        email="me@example.com",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="login@example.com",
        smtp_password="hunter2",
        use_tls=True,
    )

    result = send_crash_notification(config, subject="bv-generate crashed", body="boom")

    assert result is True
    sent = _FakeSmtp.instances[0]
    assert sent.starttls_called is True
    assert sent.login_args == ("login@example.com", "hunter2")
    assert sent.sent_message["To"] == "me@example.com"
    assert sent.sent_message["Subject"] == "bv-generate crashed"
    assert sent.sent_message.get_content().strip() == "boom"


def test_send_crash_notification_skips_login_when_no_credentials(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    config = NotifyConfig(email="me@example.com", smtp_host="smtp.example.com")

    send_crash_notification(config, subject="x", body="y")

    assert _FakeSmtp.instances[0].login_args is None


def test_send_crash_notification_skips_starttls_when_disabled(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    config = NotifyConfig(
        email="me@example.com", smtp_host="smtp.example.com", use_tls=False
    )

    send_crash_notification(config, subject="x", body="y")

    assert _FakeSmtp.instances[0].starttls_called is False


def test_send_crash_notification_never_raises_on_failure(monkeypatch):
    class _BrokenSmtp:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _BrokenSmtp)
    config = NotifyConfig(email="me@example.com", smtp_host="smtp.example.com")

    result = send_crash_notification(config, subject="x", body="y")

    assert result is False
