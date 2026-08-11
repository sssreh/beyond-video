"""
Tests for bv-config.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

There was no test coverage for this CLI at all before this file -
added alongside making its wizard I/O (ask/say/warn) injectable for
bv-web's job runner (see web/jobs.py), which also made it possible to
test the wizard's actual question/answer flow without needing a real
terminal or monkeypatching the builtin input()/print() (which
wouldn't work here anyway - prompt()'s `ask=input` default is bound
at function-definition time, so patching builtins.input afterwards
has no effect on it; this is exactly why the injectable parameter
exists in the first place).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blackvue.cli import bv_config as bv_config_module
from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import CameraConfigError
from blackvue.core.camera_config import default_target_dir
from blackvue.core.endpoint import Endpoint

# The Target prompt's suggested default for a brand-new "mycar" config -
# every scripted-ask test below that creates a new config (existing=None)
# needs this in its question-text key, since prompt() only shows the
# "[default]" suffix when a default is non-empty (see bv_config.py's
# run_wizard()).
_MYCAR_TARGET_PROMPT = f"Target (download path) [{default_target_dir('mycar')}]: "


def _scripted_ask(answers: dict[str, str | list[str]]):
    """Build an `ask` callable from a {question_text: answer} map.
    A list value is consumed one item at a time across repeated calls
    for the same question text (e.g. the endpoint-address loop, which
    asks "  New endpoint address: " more than once) - a bare string
    answers every call for that question the same way."""

    indices: dict[str, int] = {}

    def ask(text: str) -> str:
        answer = answers[text]
        if isinstance(answer, str):
            return answer
        index = indices.get(text, 0)
        indices[text] = index + 1
        return answer[index]

    return ask


def test_prompt_returns_the_answer_when_given():
    result = bv_config_module.prompt(
        "Name", default="fallback", ask=lambda text: "Kirby"
    )
    assert result == "Kirby"


def test_prompt_falls_back_to_default_on_empty_answer():
    result = bv_config_module.prompt(
        "Name", default="fallback", ask=lambda text: ""
    )
    assert result == "fallback"


def test_prompt_shows_the_default_in_the_question_text():
    seen = []

    def ask(text: str) -> str:
        seen.append(text)
        return ""

    bv_config_module.prompt("Name", default="Kirby", ask=ask)
    assert seen == ["Name [Kirby]: "]


def test_prompt_omits_the_bracket_when_no_default():
    seen = []

    def ask(text: str) -> str:
        seen.append(text)
        return "anything"

    bv_config_module.prompt("Name", default="", ask=ask)
    assert seen == ["Name: "]


def test_edit_endpoints_reviews_existing_then_appends_new():
    existing = [Endpoint(name="home", address="1.2.3.4")]
    ask = _scripted_ask(
        {
            "  Address (or 'remove') [1.2.3.4]: ": "1.2.3.4",
            "  Name [home]: ": "home",
            "  New endpoint address: ": ["5.6.7.8", ""],
            "  Name [EP2]: ": "office",
        }
    )
    say_lines: list[str] = []

    result = bv_config_module.edit_endpoints(
        existing, ask=ask, say=say_lines.append
    )

    assert result == [
        Endpoint(name="home", address="1.2.3.4"),
        Endpoint(name="office", address="5.6.7.8"),
    ]
    assert any("Add another endpoint" in line for line in say_lines)


def test_edit_endpoints_remove_drops_the_endpoint():
    existing = [
        Endpoint(name="home", address="1.2.3.4"),
        Endpoint(name="work", address="9.9.9.9"),
    ]
    ask = _scripted_ask(
        {
            "  Address (or 'remove') [1.2.3.4]: ": "remove",
            "  Address (or 'remove') [9.9.9.9]: ": "9.9.9.9",
            "  Name [work]: ": "work",
            "  New endpoint address: ": "",
        }
    )

    result = bv_config_module.edit_endpoints(existing, ask=ask, say=lambda t: None)

    assert result == [Endpoint(name="work", address="9.9.9.9")]


def test_run_wizard_builds_a_new_config():
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            _MYCAR_TARGET_PROMPT: "/tmp/archive",
            "Output (bv-export destination, optional): ": "",
            "  New endpoint address: ": ["1.2.3.4", ""],
            "  Name [EP1]: ": "home",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.id == "mycar"
    assert config.name == "Kirby"
    assert config.target == Path("/tmp/archive")
    assert config.output is None
    assert config.endpoints == [Endpoint(name="home", address="1.2.3.4")]


def test_run_wizard_target_defaults_to_a_suggested_archive_dir_for_a_new_camera():
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            _MYCAR_TARGET_PROMPT: "",
            "Output (bv-export destination, optional): ": "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.target == default_target_dir("mycar")


def test_run_wizard_sets_output_when_answered():
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            _MYCAR_TARGET_PROMPT: "/tmp/archive",
            "Output (bv-export destination, optional): ": "/tmp/exports",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.output == Path("/tmp/exports")


def test_run_wizard_defaults_every_question_to_the_existing_config():
    existing = CameraConfig(
        id="mycar",
        name="Kirby",
        target=Path("/tmp/archive"),
        endpoints=[Endpoint(name="home", address="1.2.3.4")],
    )
    # Every answer empty - Enter accepts every default, matching what
    # bv-config's own docstring promises ("defaulting every question
    # to the current value").
    ask = _scripted_ask(
        {
            "Name [Kirby]: ": "",
            "Target (download path) [/tmp/archive]: ": "",
            "Output (bv-export destination, optional): ": "",
            "  Address (or 'remove') [1.2.3.4]: ": "1.2.3.4",
            "  Name [home]: ": "home",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", existing, ask=ask, say=lambda t: None
    )

    assert config.name == "Kirby"
    assert config.target == Path("/tmp/archive")
    assert config.endpoints == [Endpoint(name="home", address="1.2.3.4")]


def test_run_wizard_defaults_output_to_the_existing_configs_output():
    existing = CameraConfig(
        id="mycar",
        name="Kirby",
        target=Path("/tmp/archive"),
        output=Path("/tmp/exports"),
        endpoints=[],
    )
    ask = _scripted_ask(
        {
            "Name [Kirby]: ": "",
            "Target (download path) [/tmp/archive]: ": "",
            "Output (bv-export destination, optional) [/tmp/exports]: ": "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", existing, ask=ask, say=lambda t: None
    )

    assert config.output == Path("/tmp/exports")


def test_run_wizard_reprompts_on_an_invalid_name():
    warns: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": ["x" * 200, "GoodName"],
            _MYCAR_TARGET_PROMPT: "/tmp/archive",
            "Output (bv-export destination, optional): ": "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", None, ask=ask, say=warns.append
    )

    assert config.name == "GoodName"
    assert any("too long" in line for line in warns)


def test_run_wizard_reprompts_on_an_empty_target(monkeypatch):
    # A brand-new camera's Target now always has a non-empty suggested
    # default (see default_target_dir()), so a blank Enter normally
    # accepts that suggestion rather than triggering the "must not be
    # empty" guard - monkeypatch the suggestion itself away to exercise
    # that guard against the one remaining way to hit it (a broken/
    # empty suggestion), rather than leaving it untested dead code.
    monkeypatch.setattr(bv_config_module, "default_target_dir", lambda id_: "")

    warns: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            "Target (download path): ": ["", "/tmp/archive"],
            "Output (bv-export destination, optional): ": "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", None, ask=ask, say=warns.append
    )

    assert config.target == Path("/tmp/archive")
    assert any("must not be empty" in line for line in warns)


class _FakeArgs:
    def __init__(self, id_: str, config_dir: Path):
        self.id = id_
        self.config_dir = config_dir


def test_run_reports_invalid_id_via_warn_not_real_stderr(monkeypatch, tmp_path):
    warns: list[str] = []
    monkeypatch.setattr(
        bv_config_module,
        "validate_id",
        lambda id_: (_ for _ in ()).throw(CameraConfigError("bad id")),
    )

    code = bv_config_module._run(
        _FakeArgs("bad id", tmp_path), warn=warns.append
    )

    assert code == bv_config_module.EXIT_INVALID_ID
    assert any("bad id" in line for line in warns)


def test_run_saves_a_new_config_end_to_end(monkeypatch, tmp_path):
    say_lines: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            _MYCAR_TARGET_PROMPT: str(tmp_path / "archive"),
            "Output (bv-export destination, optional): ": "",
            "  New endpoint address: ": ["1.2.3.4", ""],
            "  Name [EP1]: ": "home",
        }
    )

    code = bv_config_module._run(
        _FakeArgs("mycar", tmp_path), ask=ask, say=say_lines.append
    )

    assert code == bv_config_module.EXIT_OK
    assert any("Creating new config" in line for line in say_lines)
    assert any("Saved" in line for line in say_lines)

    saved_path = bv_config_module.config_path(tmp_path, "mycar")
    assert saved_path.exists()


def test_run_default_ask_say_warn_are_the_real_input_print_stderr():
    """Confirms the defaults are exactly input/print/real-stderr-print,
    i.e. real terminal use is unaffected by this refactor - this is
    checked by identity, not behavior, since actually exercising the
    real `input()` default would block waiting for a real terminal."""

    import builtins

    assert bv_config_module.prompt.__kwdefaults__["ask"] is builtins.input
    assert bv_config_module.run_wizard.__kwdefaults__["ask"] is builtins.input
    assert bv_config_module.run_wizard.__kwdefaults__["say"] is print
    assert bv_config_module._run.__kwdefaults__["say"] is print
