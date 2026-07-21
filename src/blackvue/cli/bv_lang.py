"""
Manage argos-translate language packages for bv-generate --translate.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys

from .errors import run_cli
from ..generate import MediaToolError
from ..generate import normalize_language
from ..generate import short_code


def _package_module():
    """Return the argostranslate.package module, or raise MediaToolError
    with install instructions if argostranslate isn't installed."""

    try:
        import argostranslate.package
    except ImportError as exc:
        raise MediaToolError(
            "argostranslate is not installed (pip install argostranslate)"
        ) from exc

    return argostranslate.package


def _translate_module():
    """Return the argostranslate.translate module, or raise
    MediaToolError with install instructions if argostranslate isn't
    installed."""

    try:
        import argostranslate.translate
    except ImportError as exc:
        raise MediaToolError(
            "argostranslate is not installed (pip install argostranslate)"
        ) from exc

    return argostranslate.translate


def list_installed() -> list[tuple[str, str]]:
    """Return (from_code, to_code) pairs for every argos-translate
    package installed on this machine."""

    translate_module = _translate_module()

    pairs = []

    for language in translate_module.get_installed_languages():
        for translation in language.translations_from:
            pairs.append((language.code, translation.to_lang.code))

    return pairs


def list_available() -> list[tuple[str, str]]:
    """Return (from_code, to_code) pairs for every package in the
    argos-translate package index.

    Updates the local index first, which needs network access.
    """

    package_module = _package_module()

    try:
        package_module.update_package_index()
    except Exception as exc:
        raise MediaToolError(
            f"could not reach the argos-translate package index: {exc}"
        ) from exc

    return [
        (pkg.from_code, pkg.to_code)
        for pkg in package_module.get_available_packages()
    ]


def install(source_language: str, target_language: str) -> None:
    """Download and install the argos-translate package for
    source_language -> target_language.

    Both languages may be given as a 2-letter or 3-letter code. This
    is the only place in beyond-video that reaches the network for
    translation - it only happens when you explicitly run
    `bv-lang install`, never automatically from bv-generate --translate.
    """

    package_module = _package_module()

    from_code = normalize_language(source_language)
    to_code = normalize_language(target_language)

    try:
        package_module.update_package_index()
    except Exception as exc:
        raise MediaToolError(
            f"could not reach the argos-translate package index: {exc}"
        ) from exc

    available = package_module.get_available_packages()

    match = next(
        (
            pkg
            for pkg in available
            if pkg.from_code == from_code and pkg.to_code == to_code
        ),
        None,
    )

    if match is None:
        raise MediaToolError(
            "no argos-translate package available for "
            f"{from_code!r} -> {to_code!r}"
        )

    try:
        downloaded_path = match.download()
        package_module.install_from_path(downloaded_path)
    except Exception as exc:
        raise MediaToolError(
            f"failed to install {from_code!r} -> {to_code!r}: {exc}"
        ) from exc


def _format_pair(from_code: str, to_code: str) -> str:
    return (
        f"{short_code(from_code)} -> {short_code(to_code)} "
        f"({from_code} -> {to_code})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bv-lang",
        description=(
            "Manage argos-translate language packages used by "
            "bv-generate --translate."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="List language packages."
    )
    list_parser.add_argument(
        "--available",
        action="store_true",
        help=(
            "List packages available to install instead of what's "
            "already installed (needs network access)."
        ),
    )

    install_parser = subparsers.add_parser(
        "install", help="Download and install a language package."
    )
    install_parser.add_argument(
        "source", metavar="SOURCE", help="Source language (e.g. en, eng)."
    )
    install_parser.add_argument(
        "target", metavar="TARGET", help="Target language (e.g. sv, swe)."
    )

    args = parser.parse_args(argv)

    return run_cli("bv-lang", lambda: _run(args))


def _run(args: argparse.Namespace) -> int:
    if args.command == "list":
        try:
            pairs = list_available() if args.available else list_installed()
        except MediaToolError as exc:
            print(f"bv-lang: {exc}", file=sys.stderr)
            return 1

        if not pairs:
            label = "available" if args.available else "installed"
            print(f"No {label} language packages.")
            return 0

        for from_code, to_code in sorted(pairs):
            print(_format_pair(from_code, to_code))

        return 0

    if args.command == "install":
        try:
            install(args.source, args.target)
        except MediaToolError as exc:
            print(f"bv-lang: {exc}", file=sys.stderr)
            return 1

        print(
            "Installed "
            + _format_pair(
                normalize_language(args.source),
                normalize_language(args.target),
            )
        )
        return 0

    # Unreachable in practice - add_subparsers(required=True) already
    # rejects anything but "list"/"install" before _run() is called.
    print(f"bv-lang: unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
