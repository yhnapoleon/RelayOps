"""Unit tests for SLA preset / custom-minutes reconciliation.

``sla_preset`` and ``sla_custom_minutes`` are independent columns, but the
staleness checker lets a non-null custom value silently override the preset.
``_reconcile_sla`` collapses them to a single source of truth so a job can't
read e.g. "Normal" while actually running on a leftover flat threshold (which
caused daily false "miss" alerts on once-a-day jobs).
"""

from core.services.job_service import _reconcile_sla


def test_custom_minutes_wins_and_clears_preset():
    # A flat custom threshold is "custom mode" — the preset is dropped so it
    # can't be displayed as the governing setting.
    assert _reconcile_sla("normal", 120) == (None, 120)
    assert _reconcile_sla("loose", 30) == (None, 30)
    assert _reconcile_sla(None, 240) == (None, 240)


def test_preset_kept_when_no_custom():
    assert _reconcile_sla("normal", None) == ("normal", None)
    assert _reconcile_sla("strict", None) == ("strict", None)
    assert _reconcile_sla(None, None) == (None, None)


def test_never_returns_both_non_null():
    for preset in (None, "strict", "normal", "loose"):
        for custom in (None, 1, 120, 5000):
            out_preset, out_custom = _reconcile_sla(preset, custom)
            assert not (out_preset is not None and out_custom is not None)
