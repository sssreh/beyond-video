from blackvue.archive.asset import Asset
from blackvue.cli.bv_ls import _asset_group_spans


def test_asset_group_spans_merges_consecutive_same_group_assets():
    spans = _asset_group_spans(
        [
            Asset.DURATION,
            Asset.TRANSCRIPT,
            Asset.TRANSCRIPT_DIARIZED,
            Asset.TRANSLATION,
            Asset.TRANSLATION_DIARIZED,
            Asset.SUMMARY,
        ]
    )

    assert spans == [
        (None, [Asset.DURATION]),
        ("Transcript", [Asset.TRANSCRIPT, Asset.TRANSCRIPT_DIARIZED]),
        ("Translate", [Asset.TRANSLATION, Asset.TRANSLATION_DIARIZED]),
        (None, [Asset.SUMMARY]),
    ]


def test_asset_group_spans_keeps_ungrouped_assets_separate():
    # Two consecutive ungrouped assets must not be merged into one
    # span just because they're both group=None.
    spans = _asset_group_spans([Asset.DURATION, Asset.GPX])

    assert spans == [
        (None, [Asset.DURATION]),
        (None, [Asset.GPX]),
    ]


def test_asset_group_spans_does_not_merge_a_group_split_by_a_gap():
    # If a differently-grouped (or ungrouped) asset sits between two
    # assets that share a group label, they must not be merged - only
    # genuinely consecutive same-group assets share a span.
    spans = _asset_group_spans(
        [Asset.TRANSCRIPT, Asset.DURATION, Asset.TRANSCRIPT_DIARIZED]
    )

    assert spans == [
        ("Transcript", [Asset.TRANSCRIPT]),
        (None, [Asset.DURATION]),
        ("Transcript", [Asset.TRANSCRIPT_DIARIZED]),
    ]


def test_full_display_order_group_spans_are_well_formed():
    # Sanity check against the real, current display order - every
    # grouped span should have exactly the two members we expect, and
    # group labels should fit within the combined column width so the
    # header row stays aligned.
    assets = Asset.display_order()
    widths = {asset: max(len(asset.label), 3) for asset in assets}

    spans = _asset_group_spans(assets)

    grouped = {label: members for label, members in spans if label}

    assert set(grouped) == {"Transcript", "Translate"}

    for label, members in grouped.items():
        span_width = sum(widths[a] for a in members) + (len(members) - 1)
        assert len(label) <= span_width
