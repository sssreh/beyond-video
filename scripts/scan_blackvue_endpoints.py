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
import time


# (path, description) - GET only, no destructive query params. See this
# module's own docstring for where each one came from and which camera
# models/firmware it's confirmed on so far. Order here drives the
# order everything downstream prints in (the detailed per-endpoint
# section, the summary table, results itself) - endpoints confirmed on
# Christer's actual DR900S-2CH come first, then the two /Config/
# endpoints, then the four Elite-10-only entries (seen only in Elite
# 10 firmware strings, never confirmed live - and two of them,
# blackvue_snap.cgi/blackvue_hss_live.cgi, have since timed out and
# blackvue_gps.cgi/blackvue_log.cgi 502'd on a real Elite 10 scan, and
# none of the four appear anywhere in the official Android app's own
# code either - see WORKING_CONTEXT.md), then finally the three
# app-sourced entries below, last, since they're the newest and least
# confirmed group of all - referenced in the real BlackVue app's own
# compiled code (v4.27 APK, static `strings` analysis, not yet tried
# against any real camera). blackvue_delete_file.cgi, format.cgi, and
# upgrade2.cgi were also found in the same app but are deliberately
# excluded here - destructive by name alone, exactly the category this
# script's own docstring says never to probe, even as a bare GET.
#
# One deliberate exception to the "confirmed first" ordering above:
# blackvue_live.cgi's direction=I and direction=O variants sit right
# next to its confirmed direction=F/direction=R entries rather than
# further down with the rest of the unconfirmed group - Christer asked
# for both tested too, and keeping every direction of the same
# endpoint together makes the four easy to compare at a glance, which
# matters more here than strict confirmed/unconfirmed grouping.
#
# Caveat confirmed on a real scan of Christer's own camera: F, R, I,
# and O all came back HTTP 200 with the exact same Content-Type
# (multipart/x-mixed-replace) - despite Christer confirming that
# camera only actually has front and rear channels. blackvue_live.cgi
# on that firmware evidently doesn't validate the direction value at
# all; it just streams something regardless of what's passed. So a
# "Valid" mark here only means the path answered, not that the
# direction was honored - four identical-looking 200s is itself a
# signal the endpoint ignores the parameter, not four confirmed
# channels. Telling real channels apart needs actually watching each
# stream's content (this script only reads/discards a short prefix -
# see its own docstring), not just the HTTP status.
CANDIDATE_ENDPOINTS = [
    ("/blackvue_vod.cgi", "recording listing"),
    ("/blackvue_live.cgi?direction=F", "live front video (MJPEG stream)"),
    ("/blackvue_live.cgi?direction=R", "live rear video (MJPEG stream)"),
    (
        "/blackvue_live.cgi?direction=I",
        "live interior video (MJPEG stream) - Christer: also test this "
        "direction. 'I' is the existing interior-camera letter this "
        "project already recognizes elsewhere (recording filenames, "
        "thumbnails - see RecordingId/Recording). A 200/Valid result "
        "here doesn't by itself confirm a real interior channel - see "
        "this list's own caveat above about direction validation.",
    ),
    (
        "/blackvue_live.cgi?direction=O",
        "live video, direction 'O' (MJPEG stream) - Christer: also test "
        "this direction. Unconfirmed candidate for how a camera might "
        "address a channel beyond F/R/I. A 200/Valid result here "
        "doesn't by itself confirm a real channel either - see this "
        "list's own caveat above.",
    ),
    ("/blackvue_livedata.cgi", "live GPS/g-sensor telemetry (JSON stream)"),
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
    ("/blackvue_snap.cgi", "single-frame snapshot (seen on Elite 10)"),
    ("/blackvue_hss_live.cgi", "alternate live-view stream (seen on Elite 10, HTTP Smooth Streaming)"),
    ("/blackvue_gps.cgi", "alternate GPS endpoint (seen on Elite 10)"),
    ("/blackvue_log.cgi", "event log listing (seen on Elite 10)"),
    (
        "/blackvue_status.cgi",
        "camera status/telemetry - referenced in the official BlackVue "
        "Android app's own code (v4.27 APK, static strings analysis), "
        "never tried against a real camera. The app has no reference "
        "to blackvue_livedata.cgi at all, so this is a plausible "
        "candidate for where its live GPS/g-sensor data actually comes "
        "from - unconfirmed.",
    ),
    (
        "/blackvue_sim_info.cgi",
        "LTE SIM card info - referenced in the official BlackVue Android "
        "app's own code (v4.27 APK), never tried against a real camera. "
        "Only relevant to LTE-module-equipped cameras.",
    ),
    (
        "/blackvue_sos_info.cgi",
        "SOS/emergency-call feature status - referenced in the official "
        "BlackVue Android app's own code (v4.27 APK), never tried "
        "against a real camera.",
    ),
]

# Endpoints known to be continuous streams that never close on their
# own - these get a bounded read regardless of how much Content-Length
# claims (which is often absent/wrong for a stream anyway).
STREAMING_PATHS = {
    "/blackvue_live.cgi?direction=F",
    "/blackvue_live.cgi?direction=R",
    "/blackvue_live.cgi?direction=I",
    "/blackvue_live.cgi?direction=O",
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


STREAM_VERIFY_MAX_BYTES = 262144
STREAM_VERIFY_POLL_TIMEOUT = 0.25


def _confirm_live_stream(sock, resp, *, window_seconds: float) -> bool:
    """True only if new bytes keep arriving in the *second half* of a
    `window_seconds`-long observation window - proof the server is
    still actively pushing data well after any single initial
    response has had time to fully arrive, not just answering once
    and going quiet while holding the connection open.

    History: this went through two wrong approaches before landing
    here, both based on parsing the multipart boundary out of
    Content-Type and counting how many times it recurred in the body -
    first a naive `data.count(boundary) >= 2` (fooled by a single
    frame's own proper MIME closing delimiter, `boundary + "--"`,
    which contains the plain boundary token as a substring - counted
    as 2 for what was really just 1 frame), then a fix that excluded
    the closing delimiter specifically (`boundary` not immediately
    followed by `-`) - which then wrongly reported *zero* confirmed
    streams at all, including the genuinely-live F/R directions
    Christer can actually watch. That strongly suggests this camera's
    firmware doesn't follow the assumed "-- only means closing" MIME
    convention at all (e.g. every per-frame delimiter might carry a
    trailing `--`, not just the final one) - i.e. the whole approach
    was trying to reverse-engineer an unknown, possibly nonstandard
    wire format from a webpage's worth of assumptions, and kept
    breaking on real hardware because of it.

    This version doesn't parse the stream's framing at all. It only
    asks the one thing that's actually true regardless of framing
    convention: does new data keep showing up over time, or did it
    all arrive in one initial burst and then stop forever? A real
    MJPEG stream keeps pushing frames periodically for as long as
    it's open; a dead direction answers with whatever its one static
    response is and then never sends another byte. Temporarily lowers
    `sock`'s own timeout to STREAM_VERIFY_POLL_TIMEOUT so a single
    `resp.read()` call can't block past the observation window -
    restored via `finally` either way.

    Takes `sock` explicitly rather than reading it off the
    HTTPConnection - probe() sends `Connection: close`, and
    HTTPConnection.getresponse() reacts to that by calling its own
    self.close() (nulling connection.sock) the moment it sees the
    response will close the connection, transferring real ownership
    of the socket to `resp.fp` instead. Grabbing the reference right
    after conn.request() - before that happens - is what actually
    keeps working here.
    """

    original_timeout = sock.gettimeout()
    start = time.monotonic()
    half = window_seconds / 2
    deadline = start + window_seconds
    saw_data_in_second_half = False
    total = 0

    try:
        sock.settimeout(STREAM_VERIFY_POLL_TIMEOUT)
        while time.monotonic() < deadline and total < STREAM_VERIFY_MAX_BYTES:
            try:
                chunk = resp.read(READ_BYTES)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            if not chunk:
                break
            total += len(chunk)
            if time.monotonic() - start >= half:
                saw_data_in_second_half = True
                break
    finally:
        try:
            sock.settimeout(original_timeout)
        except OSError:
            pass

    return saw_data_in_second_half


def probe(host: str, port: int, path: str, timeout: float):
    """GET `path`, reading at most READ_BYTES of the body regardless of
    what the server claims its length is - this is what keeps a
    never-closing MJPEG/telemetry stream from hanging the script.

    Returns `(status, headers, body, error, stream_confirmed)`.
    `stream_confirmed` is only ever meaningful for a `path` in
    STREAMING_PATHS with a valid (2xx/3xx) status - see
    _confirm_live_stream() for what it actually checks and why. It's
    None for every other path/outcome (not applicable), never a
    silent False standing in for "didn't check"."""

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        # Grabbed here, before getresponse() - see _confirm_live_stream()'s
        # own docstring for why conn.sock itself can't be trusted after
        # that call for a Connection: close request.
        sock = conn.sock
        resp = conn.getresponse()
        status = resp.status
        headers = dict(resp.getheaders())
        body = resp.read(READ_BYTES)

        stream_confirmed = None
        if path in STREAMING_PATHS and is_valid_status(status):
            # window_seconds ties to this probe's own --timeout rather
            # than a separate hardcoded constant - a user who already
            # widened --timeout for a slow camera gets a proportionally
            # longer observation window here too, and the default (5s)
            # is comfortably longer than any real frame interval an
            # MJPEG stream would use.
            stream_confirmed = _confirm_live_stream(sock, resp, window_seconds=timeout)

        return status, headers, body, None, stream_confirmed
    except (TimeoutError, socket.timeout) as exc:
        return None, {}, b"", f"timed out after {timeout}s ({exc})", None
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        return None, {}, b"", str(exc), None
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

# Below this, a response is treated as a genuine answer from the
# camera (2xx/3xx) rather than an error (4xx/5xx) - same threshold
# this module already used for STREAMING_PATHS's own "looks like a
# live stream" check below, just pulled out as its own named concept
# now that Christer asked to see it directly: "want to see wich
# endpoints give me a valid answer". Distinct from - and narrower
# than - "Found" above: FOUND only means the camera's web server
# responded at all (a 403/404/502 still confirms the path exists),
# VALID means that response was actually a success/redirect, not an
# error page.
VALID_STATUS_CEILING = 400


def is_valid_status(status: int | None) -> bool:
    """True if `status` is a genuine answer (2xx/3xx) rather than a
    client/server error (4xx/5xx) or no response at all (None)."""

    return status is not None and status < VALID_STATUS_CEILING


def is_valid_result(
    path: str, status: int | None, stream_confirmed: bool | None
) -> bool:
    """True if this result means "you can actually use this", not just
    "the server answered".

    For an ordinary endpoint this is exactly is_valid_status(). For a
    `path` in STREAMING_PATHS, HTTP status alone isn't enough - see
    _confirm_live_stream()'s own docstring for why: Christer confirmed
    on a real camera that blackvue_live.cgi answers 200 with an
    identical multipart Content-Type for direction=F/R/I/O alike, but
    "I never get a stream to watch for I and O" - only two of the four
    actually deliver a second frame. So a streaming path additionally
    requires `stream_confirmed` to be True.
    """

    if not is_valid_status(status):
        return False
    if path in STREAMING_PATHS:
        return bool(stream_confirmed)
    return True


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
    endpoint plus the FTP port check - with bv-ls-style "Found"/"Valid"
    marks (see bv-ls's own asset-columns table, which this
    deliberately echoes: one row per thing being checked, an X mark
    for whether it was there) and a short detail.

    Found and Valid are deliberately separate columns, not one: Found
    means the camera's web server answered at all on this path (a
    403/404/502 still confirms the path exists); Valid means that
    answer was actually usable (see is_valid_result()) - for most
    paths that's just a success/redirect status, but for a streaming
    path (see STREAMING_PATHS) it also requires a second frame/chunk
    to have actually arrived, not just a 200 wearing the right
    Content-Type. A path can be Found without being Valid.

    This complements, not replaces, the full per-endpoint dump above -
    that section still has the actual response bodies/content-types
    (and, for streaming paths, whether a live stream was confirmed)
    needed for real diagnosis. This table is what's meant to be
    skimmed at a glance, or pasted whole into a GitHub issue as the
    headline result for a camera model/firmware nobody's confirmed yet.
    """

    rows: list[tuple[str, bool, bool, str]] = []

    for path, _description, status, headers, _body, error, stream_confirmed in results:
        if error:
            rows.append((path, False, False, _shorten(f"no response: {error}")))
        else:
            content_type = headers.get("Content-Type", "")
            detail = f"HTTP {status}"
            if path in STREAMING_PATHS and is_valid_status(status):
                # Placed right after the status, before Content-Type -
                # this is the important new signal for a streaming
                # path (see is_valid_result()'s own docstring), and
                # Content-Type is long enough on its own
                # (multipart/x-mixed-replace; boundary=...) that
                # _shorten() below would otherwise cut this off before
                # it ever became visible.
                detail += (
                    ", stream confirmed" if stream_confirmed
                    else ", no second frame seen"
                )
            if content_type:
                detail += f", {content_type}"
            rows.append(
                (path, True, is_valid_result(path, status, stream_confirmed), _shorten(detail))
            )

    rows.append((
        "FTP (port 21)",
        ftp_open,
        ftp_open,
        "open" if ftp_open else "closed/unreachable",
    ))

    name_width = max(len("Endpoint"), max(len(name) for name, _, _, _ in rows))

    header = f'{"Endpoint":<{name_width}}  {"Found":^5}  {"Valid":^5}  Detail'
    print(header)
    print("-" * len(header))

    for name, found, valid, detail in rows:
        found_mark = "X" if found else ""
        valid_mark = "X" if valid else ""
        print(f"{name:<{name_width}}  {found_mark:^5}  {valid_mark:^5}  {detail}")


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
    version_bin_preview = None
    for path, description in CANDIDATE_ENDPOINTS:
        status, headers, body, error, stream_confirmed = probe(
            args.host, args.port, path, args.timeout
        )
        results.append((path, description, status, headers, body, error, stream_confirmed))

        print(f"\n{path}  ({description})")
        if error:
            # Explicit FOUND/NOT FOUND up front - Christer: "the http
            # output doesnt tell me if the test was successful or if
            # it failed", i.e. the raw status/error text alone made
            # you read between the lines. This mirrors the summary
            # table's own Found semantics exactly (any HTTP status,
            # even 403/404, is FOUND - only a connection failure is
            # NOT FOUND), so the two sections never disagree.
            print("  -> Result: NOT FOUND")
            print(f"  -> no response: {error}")
            continue

        print("  -> Result: FOUND")
        print(f"  -> HTTP {status}")
        valid = is_valid_result(path, status, stream_confirmed)
        if path in STREAMING_PATHS and is_valid_status(status):
            # A streaming path's Valid depends on stream_confirmed, not
            # just the HTTP status - see is_valid_result()'s own
            # docstring for why (Christer: "I never get a stream to
            # watch for I and O", despite both answering 200 same as
            # F/R). Spelled out explicitly here rather than folded into
            # the one-line Valid message below, since "no (error
            # response)" would be actively misleading for a case that
            # answered 200 just fine but never actually streamed.
            if stream_confirmed:
                print("  -> Valid: yes (confirmed live - a second frame arrived)")
            else:
                print(
                    "  -> Valid: no (HTTP 200, but no second frame arrived - "
                    "looks like a dead/placeholder response wearing a live "
                    "stream's Content-Type, not an actual stream)"
                )
        else:
            print(f"  -> Valid: {'yes' if valid else 'no (error response)'}")
        content_type = headers.get("Content-Type", "(none)")
        print(f"  -> Content-Type: {content_type}")
        if path in STREAMING_PATHS and is_valid_status(status):
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
        if path == "/Config/version.bin":
            # Captured for the model-hint section below, whatever it
            # actually contains - it's confirmed unreliable for model
            # ID on Christer's own camera (see its CANDIDATE_ENDPOINTS
            # description above), but showing the raw content lets a
            # contributor compare it against their camera's real model
            # themselves rather than this script silently discarding it.
            version_bin_preview = body_preview or "(empty response)"
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

    valid = [r for r in results if is_valid_result(r[0], r[2], r[6])]
    print(
        f"{len(valid)}/{len(CANDIDATE_ENDPOINTS)} candidate endpoints gave a valid answer "
        f"(HTTP < {VALID_STATUS_CEILING} - an actual success/redirect, not an error page - "
        "and, for a streaming path, an actual confirmed live stream, not just a 200 "
        "wearing the right Content-Type - see is_valid_result())."
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

    print()
    if version_bin_preview is not None:
        print(f"/Config/version.bin content: {version_bin_preview}")
        print(
            "Confirmed on Christer's own DR900S-2CH to not reliably "
            "report the correct model - shown here for reference/"
            "comparison only, not as a trustworthy model source on its own."
        )
    else:
        print("/Config/version.bin: not reachable on this camera - no content to show.")
    print(
        "\nIf you're reporting this for a camera model beyond-video hasn't "
        "been tested against, please also note (from your camera's own "
        "packaging/settings menu): the exact model name and the firmware "
        "version currently installed. See CONTRIBUTING.md for where to post this."
    )


if __name__ == "__main__":
    main()
