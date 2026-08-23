"""
Tests for web/app.py's "reuse a previous run's parameters" helpers -
_recent_web_runs() and _reuse_defaults() (see their own docstrings) -
the bv-scribe pilot of the feature Christer asked for: "i would like
to have a button or something like in bv-web to get the latest run
parameters filled in for bv-web or maybe a list of the latest."

Deliberately its own file rather than folded into test_jobs.py:
web/app.py imports fastapi (unlike web/jobs.py), so this module can
only be collected in an environment with the `web` extra installed
(CI installs it - see .github/workflows/*.yml - but this repo's own
day-to-day sandbox often doesn't have fastapi available, hence the
separate file rather than mixing collection-import requirements
inside test_jobs.py).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

import pytest
from fastapi import HTTPException

from blackvue.core import history
from blackvue.web.app import _apply_snapshot_deletion_gating
from blackvue.web.app import _archive_filter_flags
from blackvue.web.app import _authorize_job_view
from blackvue.web.app import _delete_job_snapshots
from blackvue.web.app import _fields_for_aggregation
from blackvue.web.app import _job_camera_id
from blackvue.web.app import _job_snapshot_path
from blackvue.web.app import _recent_web_runs
from blackvue.web.app import _reuse_defaults
from blackvue.web.app import _selected_graph_fields
from blackvue.web.app import _selected_stat_fields
from blackvue.web.app import _slugify
from blackvue.web.app import _sliced_job_output
from blackvue.web.app import _stats_chart_series
from blackvue.web.app import _video_label_for_filename
from blackvue.web.app import TAIL_LINE_COUNT
from blackvue.web.archive_browser import scan_archive
from blackvue.web.jobs import Job
from blackvue.web.jobs import JobStatus
from blackvue.web.users import User
from blackvue.stats_report import DEFAULT_FIELDS
from blackvue.stats_report import StatBucket


def _record(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    fields = dict(
        command="bv-scribe",
        command_line="bv-scribe kirby --task both",
        source="bv-web",
        username="christer",
        started_at="2026-08-11T12:00:00+00:00",
        duration_seconds=5.0,
        status="succeeded",
        params={"id": "kirby", "task": "both"},
    )
    fields.update(overrides)
    history.record(history.HistoryEntry(**fields))


# ---------------------------------------------------------------------------
# _recent_web_runs()
# ---------------------------------------------------------------------------


def test_recent_web_runs_empty_when_no_history(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_excludes_cli_entries(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, source="cli", username=None)

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_excludes_entries_without_params(tmp_path, monkeypatch):
    # A bv-web entry recorded before HistoryEntry.params existed (or
    # a real one that somehow captured no fields) has params=None -
    # nothing to reuse from, so it must not show up here.
    _record(tmp_path, monkeypatch, params=None)

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_excludes_other_commands(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, command="bv-search", command_line="bv-search kirby")

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_returns_matches_newest_first(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-09T10:00:00+00:00")
    _record(tmp_path, monkeypatch, started_at="2026-08-10T10:00:00+00:00")
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00")

    runs = _recent_web_runs("bv-scribe")

    assert [n.entry.started_at for n in runs] == [
        "2026-08-11T10:00:00+00:00",
        "2026-08-10T10:00:00+00:00",
        "2026-08-09T10:00:00+00:00",
    ]
    # Numbering is still the real, absolute oldest-first position -
    # newest entry (recorded 3rd) is entry #3, not #1.
    assert [n.number for n in runs] == [3, 2, 1]


def test_recent_web_runs_skips_non_matching_entries_in_between(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-09T10:00:00+00:00")
    _record(
        tmp_path,
        monkeypatch,
        command="bv-search",
        command_line="bv-search kirby",
        started_at="2026-08-10T10:00:00+00:00",
        params={"id": "kirby"},
    )
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00")

    runs = _recent_web_runs("bv-scribe")

    assert [n.number for n in runs] == [3, 1]


# ---------------------------------------------------------------------------
# _reuse_defaults()
# ---------------------------------------------------------------------------


def test_reuse_defaults_empty_with_no_recent_runs():
    assert _reuse_defaults([], None) == ({}, None)


def test_reuse_defaults_empty_with_no_recent_runs_even_with_reuse_param():
    assert _reuse_defaults([], "1") == ({}, None)


def test_reuse_defaults_defaults_to_latest_run_when_no_reuse_param(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-10T10:00:00+00:00", params={"id": "old"})
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")

    defaults, active_number = _reuse_defaults(runs, None)

    assert defaults == {"id": "new"}
    assert active_number == runs[0].number


def test_reuse_defaults_honors_a_valid_reuse_param(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-10T10:00:00+00:00", params={"id": "old"})
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")
    older_number = runs[1].number

    defaults, active_number = _reuse_defaults(runs, str(older_number))

    assert defaults == {"id": "old"}
    assert active_number == older_number


def test_reuse_defaults_falls_back_to_latest_for_unknown_reuse_number(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")

    defaults, active_number = _reuse_defaults(runs, "999999")

    assert defaults == {"id": "new"}
    assert active_number == runs[0].number


def test_reuse_defaults_falls_back_to_latest_for_non_numeric_reuse_param(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")

    defaults, active_number = _reuse_defaults(runs, "not-a-number")

    assert defaults == {"id": "new"}
    assert active_number == runs[0].number


# ---------------------------------------------------------------------------
# _archive_filter_flags() - archive_recording_list()'s "hide recordings
# with no video by default" logic. Christer first got a `videos_only`
# checkbox that defaulted to checked ("Show only with videos"), then
# said that was unclear and asked for the option to read more like
# "Show all recordings" instead. The form's checkbox is therefore the
# opt-in `include_no_video` ("Show all recordings (including ones
# without video)"), unchecked by default - see the function's own
# docstring for why framing it as an opt-in sidesteps the whole
# fresh-visit-vs-explicit-uncheck ambiguity a checked-by-default
# checkbox would need a hidden marker field to resolve.
# ---------------------------------------------------------------------------


def test_archive_filter_flags_defaults_to_videos_only_when_box_is_unchecked():
    videos_only, filters_active, show_clear_filters = _archive_filter_flags(
        include_no_video=False,
        selected_modes=set(),
        timestamp=None,
        from_=None,
        until=None,
    )

    assert videos_only is True
    assert filters_active is True
    assert show_clear_filters is False


def test_archive_filter_flags_include_no_video_turns_off_the_videos_only_filter():
    videos_only, filters_active, show_clear_filters = _archive_filter_flags(
        include_no_video=True,
        selected_modes=set(),
        timestamp=None,
        from_=None,
        until=None,
    )

    assert videos_only is False
    assert filters_active is False
    assert show_clear_filters is True


def test_archive_filter_flags_other_filters_count_even_with_include_no_video_on():
    videos_only, filters_active, show_clear_filters = _archive_filter_flags(
        include_no_video=True,
        selected_modes={"E"},
        timestamp=None,
        from_=None,
        until=None,
    )

    assert videos_only is False
    assert filters_active is True
    assert show_clear_filters is True


def test_archive_filter_flags_a_mode_filter_alone_does_not_count_as_clearable():
    # videos_only stays on (the default), so the mode filter is the only
    # real deviation from the bare default here.
    videos_only, filters_active, show_clear_filters = _archive_filter_flags(
        include_no_video=False,
        selected_modes={"E"},
        timestamp=None,
        from_=None,
        until=None,
    )

    assert videos_only is True
    assert filters_active is True
    assert show_clear_filters is True


# ---------------------------------------------------------------------------
# _authorize_job_view() / _sliced_job_output() / _job_camera_id() - shared
# by job_detail() and its /jobs/{job_id}/poll AJAX sibling (task #772-776,
# WORKING_CONTEXT.md). Testing the helpers directly, rather than the two
# routes through a TestClient, matches this file's own established
# approach above (see the module docstring) and this repo's convention of
# not depending on httpx/TestClient anywhere in the suite.
# ---------------------------------------------------------------------------


def _job(command: str = "bv-generate kirby") -> Job:
    return Job(
        id="test-job-id",
        command=command,
        username="christer",
        created_at=datetime.now(timezone.utc),
    )


def _user(role: str) -> User:
    return User(username="christer", password_hash="x", role=role)


def test_authorize_job_view_allows_owner_for_any_command():
    _authorize_job_view(_job("bv-export kirby"), _user("owner"))


def test_authorize_job_view_allows_viewer_for_bv_search():
    _authorize_job_view(_job("bv-search kirby"), _user("viewer"))


def test_authorize_job_view_rejects_viewer_for_other_commands():
    with pytest.raises(HTTPException) as exc_info:
        _authorize_job_view(_job("bv-export kirby"), _user("viewer"))

    assert exc_info.value.status_code == 403


def test_authorize_job_view_rejects_viewer_for_a_command_merely_starting_with_bv_search_prefix_mismatch():
    # "bv-search-ish kirby" must not slip through a naive startswith("bv-search")
    # check without the trailing space _authorize_job_view() actually uses.
    with pytest.raises(HTTPException):
        _authorize_job_view(_job("bv-search-ish kirby"), _user("viewer"))


def test_sliced_job_output_returns_full_output_when_tail_not_requested():
    output = [f"line {i}" for i in range(TAIL_LINE_COUNT + 10)]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.RUNNING, output, tail_requested=False
    )

    assert tail_active is False
    assert displayed == output
    assert truncated_count == 0


def test_sliced_job_output_truncates_when_tail_requested_and_running():
    output = [f"line {i}" for i in range(TAIL_LINE_COUNT + 10)]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.RUNNING, output, tail_requested=True
    )

    assert tail_active is True
    assert displayed == output[-TAIL_LINE_COUNT:]
    assert truncated_count == 10


def test_sliced_job_output_tail_requested_but_short_output_is_not_truncated():
    output = ["line 1", "line 2"]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.RUNNING, output, tail_requested=True
    )

    assert tail_active is True
    assert displayed == output
    assert truncated_count == 0


def test_sliced_job_output_tail_requested_but_finished_job_shows_full_output():
    # Tailing is offered only while running (see the route's own
    # comment) - a finished job's output is cheap to keep around/
    # re-render since no more poll ticks are coming.
    output = [f"line {i}" for i in range(TAIL_LINE_COUNT + 10)]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.SUCCEEDED, output, tail_requested=True
    )

    assert tail_active is False
    assert displayed == output
    assert truncated_count == 0


def test_job_camera_id_extracts_id_for_bv_search():
    assert _job_camera_id(_job("bv-search kirby")) == "kirby"


def test_job_camera_id_is_none_for_other_commands():
    assert _job_camera_id(_job("bv-export kirby")) is None
    assert _job_camera_id(_job("bv-generate kirby")) is None


def test_job_camera_id_is_none_when_bv_search_command_has_no_id():
    # Shouldn't happen in practice (start_bv_search() always sets
    # job.command to "bv-search {camera_id}"), but the split() guard
    # should not raise on malformed input.
    assert _job_camera_id(_job("bv-search")) is None


# ---------------------------------------------------------------------------
# _video_label_for_filename() - what archive_recording_watch() (the page
# Front/Rear links on the recording detail page now open, instead of
# playing the raw file full-page with no way back except the browser's
# own back button - Christer: "I would like that the Front and Rear
# links goes to a page just like that, instead of going straight into
# play full size(no escape)... I would like to go back to previous
# page without needing to press left arrow on browser") uses to both
# validate the requested filename and title the page. Built against a
# real ArchiveRecording via scan_archive() on a small fake archive on
# disk, same convention test_archive_browser.py already establishes,
# rather than hand-constructing one - ArchiveRecording is a frozen
# dataclass wrapping the real Recording/RecordingId parsing logic, not
# something worth re-deriving by hand here.
# ---------------------------------------------------------------------------


def _write(folder, filename, content=b"x"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(content)


def test_video_label_for_filename_matches_front_and_rear(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_NR.mp4")
    recording = scan_archive(archive, "kirby")[0]

    assert _video_label_for_filename(recording, "20260715_140212_NF.mp4") == "Front"
    assert _video_label_for_filename(recording, "20260715_140212_NR.mp4") == "Rear"


def test_video_label_for_filename_returns_none_for_unknown_filename(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    recording = scan_archive(archive, "kirby")[0]

    assert _video_label_for_filename(recording, "not-a-real-file.mp4") is None


def test_video_label_for_filename_returns_none_for_a_non_video_sidecar(tmp_path):
    """GPS/g-sensor sidecars are real files this recording actually
    has (they'd pass a known_filenames check) but aren't videos - the
    watch page has no business serving a <video> player for one, so
    this must 404 through the route rather than quietly rendering an
    empty/broken player."""

    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.gps")
    recording = scan_archive(archive, "kirby")[0]

    assert _video_label_for_filename(recording, "20260715_140212_N.gps") is None


# ---------------------------------------------------------------------------
# _job_snapshot_path() / _delete_job_snapshots() - back the inline snapshot
# preview feature (task #1123, Christer: "Of course i want to see the
# snapshot pictures on bv-web ... and then deleted after page refresh").
# Both parse the job's own "<direction>: saved <path>" output lines (see
# SNAP_SAVED_RE's comment in app.py) rather than trusting anything
# client-supplied, so these tests build a Job with snapshot_dir set and
# real files on disk under tmp_path, exactly like a real bv-snap/bv-gps
# --snap job would leave behind.
# ---------------------------------------------------------------------------


def _snap_job(tmp_path, output, snapshot_dir=None):
    job = Job(
        id="snap-job",
        command="bv-snap kirby",
        username="christer",
        created_at=datetime.now(timezone.utc),
        snapshot_dir=snapshot_dir if snapshot_dir is not None else tmp_path,
    )
    job.output.extend(output)
    return job


def test_job_snapshot_path_finds_the_saved_file(tmp_path):
    saved = tmp_path / "F_kirby_20260821.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"])

    assert _job_snapshot_path(job, "F") == saved


def test_job_snapshot_path_returns_none_for_a_direction_never_captured(tmp_path):
    saved = tmp_path / "F_kirby_20260821.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}", "R: no snapshot received"])

    assert _job_snapshot_path(job, "R") is None


def test_job_snapshot_path_returns_none_when_job_has_no_snapshot_dir(tmp_path):
    saved = tmp_path / "F_kirby_20260821.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"], snapshot_dir=None)

    assert _job_snapshot_path(job, "F") is None


def test_job_snapshot_path_rejects_a_path_outside_snapshot_dir(tmp_path):
    # Defense-in-depth (see the function's own docstring) - a "saved"
    # line pointing outside job.snapshot_dir should never actually
    # happen from real output, but must not be trusted if it did.
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {outside}"])

    assert _job_snapshot_path(job, "F") is None


def test_delete_job_snapshots_removes_every_captured_direction(tmp_path):
    front = tmp_path / "F_kirby.jpg"
    rear = tmp_path / "R_kirby.jpg"
    front.write_bytes(b"f")
    rear.write_bytes(b"r")
    job = _snap_job(tmp_path, [f"F: saved {front}", f"R: saved {rear}"])

    _delete_job_snapshots(job)

    assert not front.exists()
    assert not rear.exists()


def test_delete_job_snapshots_leaves_other_files_in_the_shared_dir_alone(tmp_path):
    # default_snapshots_dir(id_) is shared across every run against a
    # given camera (see Job.snapshot_dir's own docstring) - deletion
    # must target only this job's own captured files, not wipe the
    # directory.
    mine = tmp_path / "F_mine.jpg"
    someone_elses = tmp_path / "F_earlier_run.jpg"
    mine.write_bytes(b"mine")
    someone_elses.write_bytes(b"not mine")
    job = _snap_job(tmp_path, [f"F: saved {mine}"])

    _delete_job_snapshots(job)

    assert not mine.exists()
    assert someone_elses.exists()


def test_delete_job_snapshots_is_a_no_op_without_a_snapshot_dir(tmp_path):
    job = _snap_job(tmp_path, ["some line"], snapshot_dir=None)

    _delete_job_snapshots(job)  # must not raise


def test_delete_job_snapshots_tolerates_an_already_missing_file(tmp_path):
    already_gone = tmp_path / "F_kirby.jpg"
    job = _snap_job(tmp_path, [f"F: saved {already_gone}"])

    _delete_job_snapshots(job)  # must not raise (missing_ok=True)


# ---------------------------------------------------------------------------
# _apply_snapshot_deletion_gating() - job_detail()'s show-once-then-delete
# decision (task #1126, Christer: "running bv-gps inside bv-web, the files
# are not deleted after a page refresh"). The bug: job_detail.html's own
# automatic completion reload was silently counting as the "already shown
# once" load, so a real refresh right after it was actually the *second*
# finished load and should have deleted - but on a fast job (finished
# before the very first page load, so no automatic reload ever ran) there
# was no such reload to exclude, meaning TWO genuine manual refreshes were
# needed before anything got deleted. These tests cover both timelines.
# ---------------------------------------------------------------------------


def test_snapshot_gating_does_nothing_while_the_job_is_still_running(tmp_path):
    job = _snap_job(tmp_path, [])

    _apply_snapshot_deletion_gating(job, JobStatus.RUNNING, is_auto_reload=False)

    assert job.snapshot_shown_while_finished is False


def test_snapshot_gating_shows_without_deleting_on_the_first_finished_load(tmp_path):
    saved = tmp_path / "F_kirby.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"])

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=False)

    assert job.snapshot_shown_while_finished is True
    assert saved.exists()


def test_snapshot_gating_shows_without_deleting_on_an_automatic_reload(tmp_path):
    # job_detail.html's own poll loop marks its one completion reload
    # "?auto=1" - the very first finished load must still just show
    # the images regardless of that marker.
    saved = tmp_path / "F_kirby.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"])

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=True)

    assert job.snapshot_shown_while_finished is True
    assert saved.exists()


def test_snapshot_gating_deletes_on_a_manual_load_after_the_automatic_reload(tmp_path):
    # The slow-job timeline: poll loop's own automatic "?auto=1" reload
    # shows the images first, then Christer's real refresh (no marker,
    # since job_detail.html strips it from the address bar) deletes.
    saved = tmp_path / "F_kirby.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"])

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=True)
    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=False)

    assert not saved.exists()


def test_snapshot_gating_deletes_on_a_single_manual_refresh_when_job_finished_fast(
    tmp_path,
):
    # The fast-job timeline (this was the actual bug): the job finishes
    # before the very first page load, so no poll loop - and therefore
    # no automatic reload - ever runs. The first-ever load already
    # shows (is_finished True from the start), so Christer's one
    # subsequent manual refresh must be enough to delete, not two.
    saved = tmp_path / "F_kirby.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"])

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=False)
    assert saved.exists(), "first-ever finished load must show, not delete"

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=False)
    assert not saved.exists(), "one manual refresh after that must delete"


def test_snapshot_gating_repeated_auto_reload_never_deletes(tmp_path):
    # Defensive: even if two "?auto=1" loads somehow landed back to
    # back (a retry, a doubled reload), neither should ever delete -
    # only a load without the marker does.
    saved = tmp_path / "F_kirby.jpg"
    saved.write_bytes(b"jpeg-bytes")
    job = _snap_job(tmp_path, [f"F: saved {saved}"])

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=True)
    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=True)

    assert saved.exists()


def test_snapshot_gating_is_a_no_op_without_a_snapshot_dir(tmp_path):
    job = _snap_job(tmp_path, [], snapshot_dir=None)

    _apply_snapshot_deletion_gating(job, JobStatus.SUCCEEDED, is_auto_reload=False)

    assert job.snapshot_shown_while_finished is False


# ---------------------------------------------------------------------------
# _selected_stat_fields() / _stats_chart_data() / _slugify() - stats_dashboard()
# (task #1174, the bv-web "Stats" tab over bv-stats' own aggregation - see
# stats_report.py's module docstring). Pulled out of the route into plain
# module-level functions specifically so they're testable here without a
# TestClient, matching this file's own established approach (see the module
# docstring above).
# ---------------------------------------------------------------------------


def test_selected_stat_fields_keeps_only_known_keys():
    assert _selected_stat_fields(["distance_km", "not_a_real_field", "avg_speed_kmh"]) == [
        "distance_km",
        "avg_speed_kmh",
    ]


def test_selected_stat_fields_falls_back_to_defaults_when_empty():
    assert _selected_stat_fields([]) == list(DEFAULT_FIELDS)


def test_selected_stat_fields_falls_back_to_defaults_when_all_unknown():
    # A stale bookmark or hand-edited URL with only bogus ?fields=
    # values should degrade to the same default report bv-stats
    # itself opens with, not an empty/broken page.
    assert _selected_stat_fields(["bogus"]) == list(DEFAULT_FIELDS)


def test_selected_graph_fields_keeps_only_known_stat_fields():
    assert _selected_graph_fields(
        ["distance_km", "avg_speed_kmh", "not_a_real_field"]
    ) == ["distance_km", "avg_speed_kmh"]


def test_selected_graph_fields_allows_fields_not_in_report_selection():
    # Christer, looking at a 5-series chart: "Why 15 fields but only 5
    # graph fields." _selected_graph_fields() no longer takes a
    # selected_fields param at all - a field can be graphed without
    # also being a report table column. max_gforce_x isn't among
    # DEFAULT_FIELDS/the report's own selection, but should still be
    # graphable on its own.
    assert _selected_graph_fields(["max_gforce_x"]) == ["max_gforce_x"]


def test_selected_graph_fields_falls_back_to_default_field_when_empty():
    assert _selected_graph_fields([]) == [DEFAULT_FIELDS[0]]


def test_selected_graph_fields_falls_back_when_nothing_valid_left():
    assert _selected_graph_fields(["bogus"]) == [DEFAULT_FIELDS[0]]


def test_selected_graph_fields_keeps_more_than_one_field():
    # Christer's own follow-up request: "more than 1 stats on the y
    # axis" - multiple checked graph fields should all survive.
    assert _selected_graph_fields(["avg_speed_kmh", "distance_km"]) == [
        "avg_speed_kmh",
        "distance_km",
    ]


def test_fields_for_aggregation_unions_selected_and_graph_fields():
    # A graph-only field (checked under "Graph fields" but not
    # "Fields") still needs aggregate_recording_stats() to actually
    # compute it, or the chart would silently render an empty series
    # for it - see _fields_for_aggregation()'s own docstring.
    assert _fields_for_aggregation(
        ["distance_km", "avg_speed_kmh"], ["max_gforce_x", "distance_km"]
    ) == ["distance_km", "avg_speed_kmh", "max_gforce_x"]


def test_fields_for_aggregation_preserves_report_field_order_first():
    assert _fields_for_aggregation(
        ["duration_seconds", "distance_km"], ["distance_km", "duration_seconds"]
    ) == ["duration_seconds", "distance_km"]


def test_fields_for_aggregation_handles_empty_graph_fields():
    assert _fields_for_aggregation(["distance_km"], []) == ["distance_km"]


def test_stats_chart_series_builds_one_series_per_graphed_field():
    buckets = [
        StatBucket(
            key="2026-07",
            recordings=("a", "b", "c"),
            values={"distance_km": 505.93, "avg_speed_kmh": 42.0},
        ),
        StatBucket(
            key="2026-08",
            recordings=("d",),
            values={"distance_km": None, "avg_speed_kmh": 41.0},
        ),
    ]

    chart_data = _stats_chart_series(buckets, ["distance_km", "avg_speed_kmh"])

    assert chart_data["keys"] == ["2026-07", "2026-08"]
    assert chart_data["recording_counts"] == [3, 1]
    assert chart_data["series"] == [
        {
            "field": "distance_km",
            "label": "Distance",
            "unit": "km",
            "values": [505.93, None],
        },
        {
            "field": "avg_speed_kmh",
            "label": "Avg speed",
            "unit": "km/h",
            "values": [42.0, 41.0],
        },
    ]


def test_stats_chart_series_single_field_matches_old_behavior():
    buckets = [StatBucket(key="Monday", recordings=(), values={"avg_speed_kmh": 55.0})]

    chart_data = _stats_chart_series(buckets, ["avg_speed_kmh"])

    assert chart_data["keys"] == ["Monday"]
    assert chart_data["recording_counts"] == [0]
    assert chart_data["series"][0]["values"] == [55.0]


def test_stats_chart_series_empty_buckets_has_empty_keys_but_keeps_series_shape():
    chart_data = _stats_chart_series([], ["distance_km"])

    assert chart_data["keys"] == []
    assert chart_data["recording_counts"] == []
    assert chart_data["series"] == [
        {"field": "distance_km", "label": "Distance", "unit": "km", "values": []}
    ]


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("2026-08") == "2026-08"
    assert _slugify("Monday") == "monday"


def test_slugify_collapses_non_alnum_runs_and_strips_edges():
    assert _slugify("2026-08-23 09:20:14") == "2026-08-23-09-20-14"


def test_slugify_accepts_non_string_bucket_keys():
    # bucket.key is always a str in practice, but the filter is called
    # from Jinja where a stray int/None wouldn't be shocking - make
    # sure it degrades gracefully rather than raising.
    assert _slugify(2026) == "2026"
