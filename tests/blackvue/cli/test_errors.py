import json

from blackvue.cli.errors import EXIT_INTERRUPTED
from blackvue.cli.errors import EXIT_OS_ERROR
from blackvue.cli.errors import run_cli
from blackvue.core import history


def test_run_cli_returns_the_wrapped_functions_result():
    assert run_cli("bv-test", lambda: 0) == 0
    assert run_cli("bv-test", lambda: 7) == 7


def test_run_cli_turns_keyboard_interrupt_into_a_clean_message(capsys):
    def raiser():
        raise KeyboardInterrupt

    exit_code = run_cli("bv-test", raiser)

    err = capsys.readouterr().err

    assert exit_code == EXIT_INTERRUPTED
    assert "bv-test" in err
    assert "interrupted" in err
    assert "Traceback" not in err


def test_run_cli_turns_missing_path_error_into_a_clean_message(capsys, tmp_path):
    missing = tmp_path / "does-not-exist"

    def raiser():
        list((missing).iterdir())

    exit_code = run_cli("bv-test", raiser)

    err = capsys.readouterr().err

    assert exit_code == EXIT_OS_ERROR
    assert "bv-test" in err
    assert str(missing) in err
    assert "Traceback" not in err


def test_run_cli_turns_not_a_directory_error_into_a_clean_message(capsys, tmp_path):
    a_file = tmp_path / "just_a_file.mp4"
    a_file.write_bytes(b"x")

    def raiser():
        import os
        list(os.scandir(a_file))

    exit_code = run_cli("bv-test", raiser)

    err = capsys.readouterr().err

    assert exit_code == EXIT_OS_ERROR
    assert "bv-test" in err
    assert str(a_file) in err


def test_run_cli_lets_other_exceptions_propagate():
    def raiser():
        raise ValueError("something else entirely")

    try:
        run_cli("bv-test", raiser)
        raised = False
    except ValueError:
        raised = True

    assert raised is True


def test_run_cli_lets_system_exit_propagate():
    # argparse's own error handling (bad flags, etc.) uses SystemExit
    # and must not be swallowed/reinterpreted by run_cli.
    def raiser():
        raise SystemExit(2)

    try:
        run_cli("bv-test", raiser)
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code == 2

    assert raised is True


# ---------------------------------------------------------------------------
# run_cli() also records one core/history.py entry per invocation - the
# direct-CLI half of the persistent command-history index (see
# core/history.py's own module docstring for the full "Scope - settled"
# picture, including the bv-web half in web/jobs.py).
# ---------------------------------------------------------------------------


def test_run_cli_records_a_succeeded_history_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    run_cli("bv-ls", lambda: 0, argv=["/data/archive/Kirby", "--all"])

    lines = history.history_path().read_text().strip().split("\n")
    entry = json.loads(lines[-1])
    assert entry["command"] == "bv-ls"
    assert entry["command_line"] == "bv-ls /data/archive/Kirby --all"
    assert entry["source"] == "cli"
    assert entry["username"] is None
    assert entry["status"] == "succeeded"


def test_run_cli_records_a_failed_history_entry_for_nonzero_exit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    run_cli("bv-gps", lambda: 3, argv=["Kirby"])

    entry = json.loads(history.history_path().read_text().strip())
    assert entry["status"] == "failed"


def test_run_cli_records_an_interrupted_history_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    def raiser():
        raise KeyboardInterrupt

    run_cli("bv-scribe", raiser, argv=["Kirby"])

    entry = json.loads(history.history_path().read_text().strip())
    assert entry["status"] == "interrupted"


def test_run_cli_records_a_failed_history_entry_for_os_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    def raiser():
        raise FileNotFoundError(2, "No such file or directory", "/nope")

    run_cli("bv-download", raiser, argv=["Kirby"])

    entry = json.loads(history.history_path().read_text().strip())
    assert entry["status"] == "failed"


def test_run_cli_records_history_even_when_an_unhandled_exception_propagates(
    tmp_path, monkeypatch
):
    # run_cli() only special-cases KeyboardInterrupt/OSError - any other
    # exception (including SystemExit) is left to propagate, but the
    # history entry must still be recorded via the `finally` block.
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    def raiser():
        raise ValueError("boom")

    try:
        run_cli("bv-search", raiser, argv=["Kirby"])
    except ValueError:
        pass

    entry = json.loads(history.history_path().read_text().strip())
    assert entry["command"] == "bv-search"
    assert entry["status"] == "failed"


def test_run_cli_falls_back_to_sys_argv_when_argv_is_none(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["bv-ls", "Kirby", "--all"])

    run_cli("bv-ls", lambda: 0)

    entry = json.loads(history.history_path().read_text().strip())
    assert entry["command_line"] == "bv-ls Kirby --all"
