"""
scan_blackvue_endpoints.py - probe a BlackVue camera's own local IP for
known/candidate CGI endpoints and report what's actually there.

Why this exists: different BlackVue camera models and firmware
versions expose different CGI endpoints. beyond-video has only been
tested against a DR900S-2CH (firmware v1.009/v1.012/v1.015) and one
BlackVue Elite 10 firmware build (see WORKING_CONTEXT.md's
firmware-analysis entries for what's confirmed so far). If you own a
different BlackVue model, running this against your own camera and
posting the result as a GitHub issue (see CONTRIBUTING.md) is a direct,
concrete way to help extend support to your camera - no firmware
access or reverse engineering needed on your end.

Usage:
    python scan_blackvue_endpoints.py <camera-ip> [--port 80] [--timeout 5]

Example:
    python scan_blackvue_endpoints.py 192.168.1.42

No dependencies beyond the standard library.

Safety, read this before running it against your own camera:
  - GET/HEAD only. Nothing here writes, deletes, or uploads anything
    to the camera. upload.cgi (firmware/config upload) and the
    delete-capable query params some cameras' vod.cgi/log.cgi accept
    are deliberately never probed - only their safe, no-argument
    listing form is tried.
  - Several of these endpoints (blackvue_live.cgi, blackvue_livedata.cgi,
    blackvue_hss_live.cgi) are continuous streams that never close on
    their own once the camera starts responding. This script always
    reads a small, fixed number of bytes and then closes the
    connection itself - it never asks for "all" of a response, so it
    can't get stuck waiting on a stream that never ends.
  - Every response body is truncated to a short preview before being
    printed - this won't dump a live video/telemetry stream to your
    terminal.
  - This only ever talks to the IP you give it on your own local
    network. Nothing here reaches out to the internet.

Privacy note: blackvue_vod.cgi's response lists your own recording
filenames, which BlackVue's own naming convention encodes with the
recording's date and time. Skim the output before pasting it into a
public GitHub issue if you'd rather not share exactly when you drive.

Config.ini also carries your Wi-Fi/cloud network names and passwords
(confirmed on a real camera - see WORKING_CONTEXT.md). Any field whose
name looks like a password/secret (ap_pw, sta_pw, sta2_pw, sta3_pw, or
anything else containing "pw", "pass", "secret", or "token") is
replaced with "[secret]" in this script's own preview before it's ever
printed - see redact_config_ini() below. Network *names* (ap_ssid,
sta_ssid, etc.) are left as-is; only credential-looking values are
redacted.
"""

import argparse
import http.client
import re
import socket
import sys


# (path, description) - GET only, no destructive query params. See this
# module's own docstring for where each one came from and which camera
# models/firmware it's confirmed on so far.
CANDIDATE_ENDPOINTS = [
    ("/blackvue_vod.cgi", "recording listing"),
    ("/blackvue_live.cgi?direction=F", "live front video (MJPEG stream)"),
    ("/blackvue_live.cgi?direction=R", "live rear video (MJPEG stream)"),
    ("/blackvue_livedata.cgi", "live GPS/g-sensor telemetry (JSON stream)"),
    ("/blackvue_snap.cgi", "single-frame snapshot (seen on Elite 10)"),
    ("/blackvue_hss_live.cgi", "alternate live-view stream (seen on Elite 10, HTTP Smooth Streaming)"),
    ("/blackvue_gps.cgi", "alternate GPS endpoint (seen on Elite 10)"),
    ("/blackvue_log.cgi", "event log listing (seen on Elite 10)"),
    ("/Config/config.ini", "camera configuration file"),
    (
        "/Config/version.bin",
        "firmware version file - confirmed on Christer's own DR900S-2CH "
        "to not reliably report the correct model, so don't trust its "
        "content for model ID without cross-checking (config.ini's "
        "ap_ssid - see the summary below - or just asking the camera's "
        "own settings menu). Still probed since other cameras/firmware "
        "may behave differently.",
    ),
]

# Endpoints known to be continuous streams that never close on their
# own - these get a bounded read regardless of how much Content-Length
# claims (which is often absent/wrong for a stream anyway).
STREAMING_PATHS = {
    "/blackvue_live.cgi?direction=F",
    "/blackvue_live.cgi?direction=R",
    "/blackvue_livedata.cgi",
    "/blackvue_hss_live.cgi",
    "/blackvue_gps.cgi",
}

READ_BYTES = 2048
PREVIEW_BYTES = 300

# Matches a "key=value" line in config.ini's INI-like text whose key
# name suggests a credential - by key name, not value shape, so it
# generalizes across camera models that might not use exactly the
# ap_pw/sta_pw/sta2_pw/sta3_pw names seen on the one real camera this
# was confirmed against. Deliberately broad (any key merely containing
# "pw"/"pass"/"secret"/"token" anywhere in its name) - an occasional
# false-positive redaction of a harmless field is a fine trade for
# never missing a real one.
_SECRET_KEY_LINE = re.compile(
    r"^(?P<key>[^=\r\n]*?(?:pw|pass(?:word)?|secret|token)[^=\r\n]*)=.*$",
    re.IGNORECASE | re.MULTILINE,
)


def redact_config_ini(text: str) -> str:
    """Replace any credential-looking value in config.ini's own text
    with a fixed "[secret]" placeholder, keeping the key name visible.

    This script's whole purpose is producing output a contributor
    pastes into a public GitHub issue - config.ini carries Wi-Fi/cloud
    passwords (confirmed on a real camera, see WORKING_CONTEXT.md), so
    this must run before that text is ever previewed/printed, not
    just skimmed for afterward.
    """

    return _SECRET_KEY_LINE.sub(lambda m: f"{m.group('key')}=[secret]", text)


# The camera's own default AP network name - not a credential (see
# redact_config_ini() above, which deliberately leaves ap_ssid/sta_ssid
# alone), and commonly embeds the model in BlackVue's own default
# naming convention. Christer: "I only know 2 different ways to find
# out camera model. version.bin and the ssid name (as default) in
# config.ini" - and separately confirmed version.bin doesn't reliably
# report the correct model on his own DR900S-2CH (see
# CANDIDATE_ENDPOINTS's own entry for it above), leaving this as the
# one method with any real signal behind it. Still just a hint, not
# ground truth - see print_model_hint()'s own caveat below.
_AP_SSID_LINE = re.compile(r"^ap_ssid=(?P<value>.*)$", re.IGNORECASE | re.MULTILINE)


def extract_ap_ssid(config_ini_text: str) -> str | None:
    """Pull the ap_ssid value out of config.ini's raw text, or None if
    the key isn't present or its value is empty."""

    match = _AP_SSID_LINE.search(config_ini_text)
    if match is None:
        return None
    value = match.group("value").strip()
    return value or None


def probe(host: str, port: int, path: str, timeout: float):
    """GET `path`, reading at most READ_BYTES of the body regardless of
    what the server claims its length is - this is what keeps a
    never-closing MJPEG/telemetry stream from hanging the script."""

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        status = resp.status
        headers = dict(resp.getheaders())
        body = resp.read(READ_BYTES)
        return status, headers, body, None
    except (TimeoutError, socket.timeout) as exc:
        return None, {}, b"", f"timed out after {timeout}s ({exc})"
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        return None, {}, b"", str(exc)
    finally:
        conn.close()


def check_tcp_port(host: str, port: int, timeout: float) -> bool:
    """True if a plain TCP connect to host:port succeeds - used for the
    FTP port only (a real TCP service). TFTP is UDP-based and isn't
    meaningfully checkable with a plain connect, so it's not probed
    here - noted as a manual follow-up instead."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


SUMMARY_DETAIL_LIMIT = 60


def _shorten(text: str, limit: int = SUMMARY_DETAIL_LIMIT) -> str:
    """Truncate a detail string for the summary table below - the
    full text (the real error message, etc.) is already printed in
    the detailed per-endpoint section above this; the summary table
    is meant to be skimmed as one line per test, not read for the
    full message."""

    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def print_summary_table(results: list[tuple], ftp_open: bool) -> None:
    """Print one row per test this script ran - every candidate
    endpoint plus the FTP port check - with a bv-ls-style "Found" mark
    (see bv-ls's own asset-columns table, which this deliberately
    echoes: one row per thing being checked, an X mark for whether it
    was there) and a short detail.

    This complements, not replaces, the full per-endpoint dump above -
    that section still has the actual response bodies/content-types
    needed for real diagnosis. This table is what's meant to be
    skimmed at a glance, or pasted whole into a GitHub issue as the
    headline result for a camera model/firmware nobody's confirmed yet.
    """

    rows: list[tuple[str, bool, str]] = []

    for path, _description, status, headers, _body, error in results:
        if error:
            rows.append((path, False, _shorten(f"no response: {error}")))
        else:
            content_type = headers.get("Content-Type", "")
            detail = f"HTTP {status}"
            if content_type:
                detail += f", {content_type}"
            rows.append((path, True, _shorten(detail)))

    rows.append((
        "FTP (port 21)",
        ftp_open,
        "open" if ftp_open else "closed/unreachable",
    ))

    name_width = max(len("Endpoint"), max(len(name) for name, _, _ in rows))

    header = f'{"Endpoint":<{name_width}}  {"Found":^5}  Detail'
    print(header)
    print("-" * len(header))

    for name, found, detail in rows:
        mark = "X" if found else ""
        print(f"{name:<{name_width}}  {mark:^5}  {detail}")


def preview(body: bytes) -> str:
    """A short, report-friendly preview of a response body - plain text
    is shown (truncated) as-is; anything that looks like binary data
    (a JPEG frame in an MJPEG stream, say) is summarized instead of
    dumped as a wall of unicode replacement characters."""

    if not body:
        return ""

    text = body.decode("utf-8", errors="replace")

    # isprintable() alone isn't a reliable binary detector: the U+FFFD
    # replacement character errors="replace" substitutes for invalid
    # UTF-8 bytes counts as "printable" under Python's own definition,
    # so a JPEG frame's raw bytes would sail right through that check.
    # Counting how much of the text actually had to be replaced is a
    # direct measure of "how much of this wasn't valid UTF-8 to begin
    # with" instead.
    replacement_ratio = text.count("�") / len(text)

    if replacement_ratio > 0.05:
        return f"[binary data, {len(body)} bytes read, starts with {body[:16].hex()}]"

    text = "".join(ch if ch.isprintable() or ch in "\n\r\t" else "." for ch in text)
    if len(text) > PREVIEW_BYTES:
        return text[:PREVIEW_BYTES] + f"... [truncated, {len(body)} bytes read]"
    return text


def main():
    parser = argparse.ArgumentParser(
        description="Probe a BlackVue camera's local IP for known CGI endpoints."
    )
    parser.add_argument("host", help="Camera's local IP address (e.g. 192.168.1.42)")
    parser.add_argument("--port", type=int, default=80, help="Default: 80")
    parser.add_argument(
        "--timeout", type=float, default=5.0,
        help="Per-request timeout in seconds (default: 5.0)",
    )
    args = parser.parse_args()

    print(f"Scanning {args.host}:{args.port} ...\n")
    print("=" * 70)

    results = []
    ap_ssid = None
    for path, description in CANDIDATE_ENDPOINTS:
        status, headers, body, error = probe(args.host, args.port, path, args.timeout)
        results.append((path, description, status, headers, body, error))

        print(f"\n{path}  ({description})")
        if error:
            print(f"  -> no response: {error}")
            continue

        print(f"  -> HTTP {status}")
        content_type = headers.get("Content-Type", "(none)")
        print(f"  -> Content-Type: {content_type}")
        if path in STREAMING_PATHS and status and status < 400:
            print("  -> looks like a live stream - read a short prefix and closed the connection")
        preview_source = body
        if path == "/Config/config.ini":
            # Redact before preview() ever sees it - not after - so a
            # credential can't slip through via preview()'s own
            # truncation/binary-detection logic never being reached
            # this specific way.
            config_text = body.decode("utf-8", errors="replace")
            ap_ssid = extract_ap_ssid(config_text)
            redacted_text = redact_config_ini(config_text)
            preview_source = redacted_text.encode("utf-8")
        body_preview = preview(preview_source)
        if body_preview.strip():
            print("  -> body preview:")
            for line in body_preview.splitlines()[:10]:
                print(f"     {line}")

    print("\n" + "=" * 70)
    print(f"\nChecking FTP (port 21) ...")
    ftp_open = check_tcp_port(args.host, 21, args.timeout)
    print(f"  -> port 21: {'open' if ftp_open else 'closed/unreachable'}")
    print(
        "\nNote: TFTP (port 69, UDP) isn't checked by this script - a plain "
        "TCP connect can't test it meaningfully. If you want to check it "
        "yourself: `nc -u -v <ip> 69` or similar."
    )

    print("\n" + "=" * 70)
    print("\nSummary (one row per test):\n")
    print_summary_table(results, ftp_open)

    responded = [r for r in results if r[2] is not None]
    print(
        f"\n{len(responded)}/{len(CANDIDATE_ENDPOINTS)} candidate endpoints got a response "
        f"(any status code counts - even a 403/404 confirms the path exists on this camera)."
    )

    print()
    if ap_ssid:
        print(f"Probable model hint (config.ini ap_ssid): {ap_ssid}")
    else:
        print("Probable model hint: none - config.ini wasn't reachable, or had no ap_ssid.")
    print(
        "This is just the camera's own default WiFi AP name, not a "
        "verified model number - it commonly embeds the model in "
        "BlackVue's own default naming, but that hasn't been confirmed "
        "as reliable across models/firmware, and it's meaningless if "
        "the AP name was ever changed from its factory default. Still "
        "note your camera's exact model name and firmware version "
        "yourself (from its settings menu or the BlackVue app) when "
        "reporting this - see below - rather than relying on this hint alone."
    )
    print(
        "\nIf you're reporting this for a camera model beyond-video hasn't "
        "been tested against, please also note (from your camera's own "
        "packaging/settings menu): the exact model name and the firmware "
        "version currently installed. See CONTRIBUTING.md for where to post this."
    )


if __name__ == "__main__":
    main()
