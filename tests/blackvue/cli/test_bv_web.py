from blackvue.cli import bv_web
from blackvue.web.users import UsersConfig
from blackvue.web.users import load_users_config


def test_main_adduser_creates_owner_account(monkeypatch, capsys, tmp_path):
    users_file = tmp_path / "web-users.cfg"

    passwords = iter(["hunter2", "hunter2"])
    monkeypatch.setattr(bv_web.getpass, "getpass", lambda prompt="": next(passwords))

    exit_code = bv_web.main(
        ["adduser", "christer", "--role", "owner", "--users-file", str(users_file)]
    )

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Added owner user 'christer'" in out

    config = load_users_config(users_file)
    assert config.get("christer").role == "owner"
    assert config.authenticate("christer", "hunter2") is not None


def test_main_adduser_rejects_mismatched_passwords(monkeypatch, capsys, tmp_path):
    users_file = tmp_path / "web-users.cfg"

    passwords = iter(["hunter2", "something-else"])
    monkeypatch.setattr(bv_web.getpass, "getpass", lambda prompt="": next(passwords))

    exit_code = bv_web.main(
        ["adduser", "christer", "--role", "owner", "--users-file", str(users_file)]
    )

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "passwords did not match" in err
    assert not users_file.exists()


def test_main_adduser_rejects_duplicate_username(monkeypatch, capsys, tmp_path):
    users_file = tmp_path / "web-users.cfg"

    passwords = iter(["hunter2", "hunter2", "hunter2", "hunter2"])
    monkeypatch.setattr(bv_web.getpass, "getpass", lambda prompt="": next(passwords))

    bv_web.main(
        ["adduser", "christer", "--role", "owner", "--users-file", str(users_file)]
    )
    exit_code = bv_web.main(
        ["adduser", "christer", "--role", "viewer", "--users-file", str(users_file)]
    )

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "bv-web:" in err


def test_main_adduser_rejects_unknown_role(capsys, tmp_path):
    users_file = tmp_path / "web-users.cfg"

    exit_code = None
    try:
        bv_web.main(
            ["adduser", "christer", "--role", "manager", "--users-file", str(users_file)]
        )
    except SystemExit as exc:
        exit_code = exc.code

    err = capsys.readouterr().err

    assert exit_code == 2
    assert "manager" in err


def test_main_serve_reports_missing_uvicorn_cleanly(monkeypatch, capsys, tmp_path):
    # This sandbox has no uvicorn installed - confirm bv-web fails with
    # a clean, actionable stderr message instead of a raw traceback,
    # rather than actually trying to start a server (which needs
    # fastapi/uvicorn - see WORKING_CONTEXT.md's note on this being
    # unverifiable end-to-end in this sandbox). Needs a users file with
    # at least one account first, or _serve() reports the "no users
    # yet" problem before it ever gets to importing uvicorn.
    users_file = tmp_path / "users.cfg"
    config = UsersConfig(path=users_file)
    config.add_user("christer", "hunter2", "owner")
    config.save()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no module named uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    exit_code = bv_web.main(
        ["serve", str(tmp_path / "trips"), "--users-file", str(users_file)]
    )

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "uvicorn is not installed" in err


def test_main_serve_reports_when_no_users_exist_yet(monkeypatch, capsys, tmp_path):
    # uvicorn is genuinely absent in this sandbox, so this exercises
    # the "no accounts yet" check, which runs before uvicorn would
    # even be needed - see bv_web._serve()'s ordering.
    exit_code = bv_web.main(
        ["serve", str(tmp_path / "trips"), "--users-file", str(tmp_path / "users.cfg")]
    )

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "has no users yet" in err
