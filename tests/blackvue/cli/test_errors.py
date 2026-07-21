from blackvue.cli.errors import EXIT_INTERRUPTED
from blackvue.cli.errors import EXIT_OS_ERROR
from blackvue.cli.errors import run_cli


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
