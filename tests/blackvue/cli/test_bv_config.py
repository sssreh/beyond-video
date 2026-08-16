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
from blackvue.core.camera_config import default_archive_dir
from blackvue.core.camera_config import default_target_dir
from blackvue.core.endpoint import Endpoint

# The Archive prompt's suggested default for a brand-new "mycar" config -
# every scripted-ask test below that creates a new config (existing=None)
# needs this in its question-text key, since prompt() only shows the
# "[default]" suffix when a default is non-empty (see bv_config.py's
# run_wizard()).
_MYCAR_ARCHIVE_PROMPT = f"Archive (download path) [{default_archive_dir('mycar')}]: "

# The Adapter prompt's question text and "accept the default" answer -
# every scripted-ask test below that drives run_wizard() needs this in
# its dict now that the wizard asks a third question before Archive
# (see bv_config.py's run_wizard()); every test in this file predates
# multi-adapter support, so "accept blackvue" (the default for both a
# brand-new config and every existing CameraConfig built without an
# explicit `adapter=` in this file) is the right answer everywhere
# except the tests written specifically to exercise adapter selection.
_ADAPTER_PROMPT = "Adapter (blackvue/folder/gopro) [blackvue]: "
_ACCEPT_DEFAULT_ADAPTER = {_ADAPTER_PROMPT: ""}


def _target_prompt_for(archive: str) -> str:
    """The Target prompt's question text once Archive has been
    answered `archive` - its suggested default is only known after
    that point (see run_wizard()'s own comment on why)."""

    default = default_target_dir(Path(archive))
    return f"Target (bv-export destination, optional) [{default}]: "


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
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            _target_prompt_for("/tmp/archive"): "/tmp/exports",
            "  New endpoint address: ": ["1.2.3.4", ""],
            "  Name [EP1]: ": "home",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.id == "mycar"
    assert config.name == "Kirby"
    assert config.adapter == "blackvue"
    assert config.archive == Path("/tmp/archive")
    assert config.target == Path("/tmp/exports")
    assert config.endpoints == [Endpoint(name="home", address="1.2.3.4")]


def test_run_wizard_archive_defaults_to_a_suggested_dir_for_a_new_camera():
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: "",
            _target_prompt_for(str(default_archive_dir("mycar"))): "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.archive == default_archive_dir("mycar")


def test_run_wizard_target_defaults_to_a_parallel_trips_dir():
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            _target_prompt_for("/tmp/archive"): "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.target == default_target_dir(Path("/tmp/archive"))


def test_run_wizard_target_can_be_left_unset(monkeypatch):
    # default_target_dir() itself never returns an empty suggestion (it
    # always falls back to a sibling "trips" dir), so the only way to
    # actually exercise "Target left unset" is to monkeypatch that
    # suggestion away - matching the same technique the Archive
    # empty-reprompt test below uses.
    monkeypatch.setattr(bv_config_module, "default_target_dir", lambda archive: "")

    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            "Target (bv-export destination, optional): ": "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.target is None


def test_run_wizard_sets_target_when_answered():
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            _target_prompt_for("/tmp/archive"): "/tmp/exports",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard("mycar", None, ask=ask, say=lambda t: None)

    assert config.target == Path("/tmp/exports")


def test_run_wizard_defaults_every_question_to_the_existing_config():
    existing = CameraConfig(
        id="mycar",
        name="Kirby",
        archive=Path("/tmp/archive"),
        endpoints=[Endpoint(name="home", address="1.2.3.4")],
    )
    # Every answer empty - Enter accepts every default, matching what
    # bv-config's own docstring promises ("defaulting every question
    # to the current value").
    ask = _scripted_ask(
        {
            "Name [Kirby]: ": "",
            **_ACCEPT_DEFAULT_ADAPTER,
            "Archive (download path) [/tmp/archive]: ": "",
            _target_prompt_for("/tmp/archive"): "",
            "  Address (or 'remove') [1.2.3.4]: ": "1.2.3.4",
            "  Name [home]: ": "home",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", existing, ask=ask, say=lambda t: None
    )

    assert config.name == "Kirby"
    assert config.adapter == "blackvue"
    assert config.archive == Path("/tmp/archive")
    assert config.endpoints == [Endpoint(name="home", address="1.2.3.4")]


def test_run_wizard_defaults_target_to_the_existing_configs_target():
    existing = CameraConfig(
        id="mycar",
        name="Kirby",
        archive=Path("/tmp/archive"),
        target=Path("/tmp/exports"),
        endpoints=[],
    )
    ask = _scripted_ask(
        {
            "Name [Kirby]: ": "",
            **_ACCEPT_DEFAULT_ADAPTER,
            "Archive (download path) [/tmp/archive]: ": "",
            "Target (bv-export destination, optional) [/tmp/exports]: ": "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", existing, ask=ask, say=lambda t: None
    )

    assert config.target == Path("/tmp/exports")


def test_run_wizard_reprompts_on_an_invalid_name():
    warns: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": ["x" * 200, "GoodName"],
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            _target_prompt_for("/tmp/archive"): "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", None, ask=ask, say=warns.append
    )

    assert config.name == "GoodName"
    assert any("too long" in line for line in warns)


def test_run_wizard_reprompts_on_an_empty_archive(monkeypatch):
    # A brand-new camera's Archive now always has a non-empty suggested
    # default (see default_archive_dir()), so a blank Enter normally
    # accepts that suggestion rather than triggering the "must not be
    # empty" guard - monkeypatch the suggestion itself away to exercise
    # that guard against the one remaining way to hit it (a broken/
    # empty suggestion), rather than leaving it untested dead code.
    monkeypatch.setattr(bv_config_module, "default_archive_dir", lambda id_: "")

    warns: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            **_ACCEPT_DEFAULT_ADAPTER,
            "Archive (download path): ": ["", "/tmp/archive"],
            _target_prompt_for("/tmp/archive"): "",
            "  New endpoint address: ": "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", None, ask=ask, say=warns.append
    )

    assert config.archive == Path("/tmp/archive")
    assert any("must not be empty" in line for line in warns)


def test_run_wizard_reprompts_on_an_unknown_adapter():
    warns: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            _ADAPTER_PROMPT: ["nonsense", "folder"],
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            _target_prompt_for("/tmp/archive"): "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", None, ask=ask, say=warns.append
    )

    assert config.adapter == "folder"
    assert any("Unknown adapter" in line for line in warns)


def test_run_wizard_skips_endpoint_setup_for_a_non_network_adapter():
    say_lines: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            _ADAPTER_PROMPT: "folder",
            _MYCAR_ARCHIVE_PROMPT: "/tmp/archive",
            _target_prompt_for("/tmp/archive"): "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", None, ask=ask, say=say_lines.append
    )

    assert config.adapter == "folder"
    assert config.endpoints == []
    assert any("doesn't use network endpoints" in line for line in say_lines)


def test_run_wizard_preserves_existing_endpoints_when_switched_to_a_non_network_adapter():
    existing = CameraConfig(
        id="mycar",
        name="Kirby",
        archive=Path("/tmp/archive"),
        adapter="blackvue",
        endpoints=[Endpoint(name="home", address="1.2.3.4")],
    )
    ask = _scripted_ask(
        {
            "Name [Kirby]: ": "",
            "Adapter (blackvue/folder/gopro) [blackvue]: ": "folder",
            "Archive (download path) [/tmp/archive]: ": "",
            _target_prompt_for("/tmp/archive"): "",
        }
    )

    config = bv_config_module.run_wizard(
        "mycar", existing, ask=ask, say=lambda t: None
    )

    # Not re-asked (no "  Address (or 'remove')..."/"  New endpoint
    # address: " entries in the script above - a KeyError from
    # _scripted_ask would fail this test if edit_endpoints() were
    # called), and the old endpoint is left untouched rather than
    # cleared - see run_wizard()'s own comment on why.
    assert config.endpoints == [Endpoint(name="home", address="1.2.3.4")]


def test_run_wizard_defaults_adapter_to_the_existing_configs_adapter():
    existing = CameraConfig(
        id="gp",
        name="GP",
        archive=Path("/tmp/gopro"),
        adapter="folder",
        endpoints=[],
    )
    ask = _scripted_ask(
        {
            "Name [GP]: ": "",
            "Adapter (blackvue/folder/gopro) [folder]: ": "",
            "Archive (download path) [/tmp/gopro]: ": "",
            _target_prompt_for("/tmp/gopro"): "",
        }
    )

    config = bv_config_module.run_wizard("gp", existing, ask=ask, say=lambda t: None)

    assert config.adapter == "folder"


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
    archive = str(tmp_path / "archive")
    say_lines: list[str] = []
    ask = _scripted_ask(
        {
            "Name [mycar]: ": "Kirby",
            **_ACCEPT_DEFAULT_ADAPTER,
            _MYCAR_ARCHIVE_PROMPT: archive,
            _target_prompt_for(archive): "",
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
