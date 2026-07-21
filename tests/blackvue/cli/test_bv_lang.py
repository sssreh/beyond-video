import sys
import types

from blackvue.cli import bv_lang
from blackvue.generate import MediaToolError


def _install_fake_argostranslate(monkeypatch, *, package_module=None, translate_module=None):
    """Inject fake argostranslate.package / argostranslate.translate
    submodules into sys.modules for the duration of a test, so bv_lang's
    lazy `import argostranslate.package` picks them up instead of the
    real (possibly missing) library."""

    argostranslate_pkg = types.ModuleType("argostranslate")

    if package_module is not None:
        argostranslate_pkg.package = package_module

    if translate_module is not None:
        argostranslate_pkg.translate = translate_module

    monkeypatch.setitem(sys.modules, "argostranslate", argostranslate_pkg)

    if package_module is not None:
        monkeypatch.setitem(
            sys.modules, "argostranslate.package", package_module
        )

    if translate_module is not None:
        monkeypatch.setitem(
            sys.modules, "argostranslate.translate", translate_module
        )


class _FakeToLang:
    def __init__(self, code):
        self.code = code


class _FakeTranslation:
    def __init__(self, to_code):
        self.to_lang = _FakeToLang(to_code)


class _FakeLanguage:
    def __init__(self, code, to_codes):
        self.code = code
        self.translations_from = [
            _FakeTranslation(to_code) for to_code in to_codes
        ]


def test_list_installed_flattens_languages_and_translations(monkeypatch):
    translate_module = types.ModuleType("argostranslate.translate")
    translate_module.get_installed_languages = lambda: [
        _FakeLanguage("en", ["sv", "th"]),
        _FakeLanguage("sv", ["en"]),
    ]

    _install_fake_argostranslate(monkeypatch, translate_module=translate_module)

    pairs = bv_lang.list_installed()

    assert sorted(pairs) == [("en", "sv"), ("en", "th"), ("sv", "en")]


def test_list_installed_raises_media_tool_error_when_argostranslate_missing(
    monkeypatch,
):
    # Simulate argostranslate genuinely not being installed by making
    # the import fail no matter what's in sys.modules.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "argostranslate.translate" or name == "argostranslate":
            raise ImportError("no module named argostranslate")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    try:
        bv_lang.list_installed()
        raised = False
    except MediaToolError:
        raised = True

    assert raised is True


class _FakeAvailablePackage:
    def __init__(self, from_code, to_code, downloaded_to=None):
        self.from_code = from_code
        self.to_code = to_code
        self._downloaded_to = downloaded_to
        self.download_called = False

    def download(self):
        self.download_called = True
        return self._downloaded_to or f"/tmp/{self.from_code}_{self.to_code}.argosmodel"


def test_list_available_updates_index_then_lists_packages(monkeypatch):
    calls = []

    package_module = types.ModuleType("argostranslate.package")
    package_module.update_package_index = lambda: calls.append("update")
    package_module.get_available_packages = lambda: [
        _FakeAvailablePackage("en", "sv"),
        _FakeAvailablePackage("en", "th"),
    ]

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    pairs = bv_lang.list_available()

    assert calls == ["update"]
    assert sorted(pairs) == [("en", "sv"), ("en", "th")]


def test_list_available_wraps_index_failure_in_media_tool_error(monkeypatch):
    package_module = types.ModuleType("argostranslate.package")

    def failing_update():
        raise OSError("network unreachable")

    package_module.update_package_index = failing_update
    package_module.get_available_packages = lambda: []

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    try:
        bv_lang.list_available()
        raised = False
    except MediaToolError as exc:
        raised = True
        assert "network unreachable" in str(exc)

    assert raised is True


def test_install_normalizes_codes_and_downloads_matching_package(
    monkeypatch,
):
    match = _FakeAvailablePackage("en", "sv")
    other = _FakeAvailablePackage("en", "th")
    installed = []

    package_module = types.ModuleType("argostranslate.package")
    package_module.update_package_index = lambda: None
    package_module.get_available_packages = lambda: [match, other]
    package_module.install_from_path = lambda path: installed.append(path)

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    # 3-letter codes in, 2-letter codes used to find the package.
    bv_lang.install("eng", "swe")

    assert match.download_called is True
    assert other.download_called is False
    assert installed == ["/tmp/en_sv.argosmodel"]


def test_install_raises_when_no_matching_package_in_index(monkeypatch):
    package_module = types.ModuleType("argostranslate.package")
    package_module.update_package_index = lambda: None
    package_module.get_available_packages = lambda: [
        _FakeAvailablePackage("en", "th")
    ]

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    try:
        bv_lang.install("en", "sv")
        raised = False
    except MediaToolError as exc:
        raised = True
        assert "'en' -> 'sv'" in str(exc)

    assert raised is True


def test_install_wraps_download_failure_in_media_tool_error(monkeypatch):
    class _BrokenPackage(_FakeAvailablePackage):
        def download(self):
            raise OSError("disk full")

    package_module = types.ModuleType("argostranslate.package")
    package_module.update_package_index = lambda: None
    package_module.get_available_packages = lambda: [
        _BrokenPackage("en", "sv")
    ]
    package_module.install_from_path = lambda path: None

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    try:
        bv_lang.install("en", "sv")
        raised = False
    except MediaToolError as exc:
        raised = True
        assert "disk full" in str(exc)

    assert raised is True


def test_main_list_prints_installed_pairs(monkeypatch, capsys):
    translate_module = types.ModuleType("argostranslate.translate")
    translate_module.get_installed_languages = lambda: [
        _FakeLanguage("en", ["sv"])
    ]

    _install_fake_argostranslate(monkeypatch, translate_module=translate_module)

    exit_code = bv_lang.main(["list"])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "eng -> swe" in out
    assert "en -> sv" in out


def test_main_list_reports_no_packages(monkeypatch, capsys):
    translate_module = types.ModuleType("argostranslate.translate")
    translate_module.get_installed_languages = lambda: []

    _install_fake_argostranslate(monkeypatch, translate_module=translate_module)

    exit_code = bv_lang.main(["list"])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "No installed language packages." in out


def test_main_install_prints_confirmation(monkeypatch, capsys):
    match = _FakeAvailablePackage("en", "sv")

    package_module = types.ModuleType("argostranslate.package")
    package_module.update_package_index = lambda: None
    package_module.get_available_packages = lambda: [match]
    package_module.install_from_path = lambda path: None

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    exit_code = bv_lang.main(["install", "en", "sv"])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Installed" in out
    assert "eng -> swe" in out


def test_main_install_reports_error_and_nonzero_exit(monkeypatch, capsys):
    package_module = types.ModuleType("argostranslate.package")
    package_module.update_package_index = lambda: None
    package_module.get_available_packages = lambda: []

    _install_fake_argostranslate(monkeypatch, package_module=package_module)

    exit_code = bv_lang.main(["install", "en", "sv"])

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "bv-lang:" in err
