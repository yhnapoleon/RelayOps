"""Eval gate — schedule adherence must MODEL extra/missed runs, not emit a
multi-day phantom deviation (the 2nd-round regression: a mid-week run on a
weekly job was reported as ~71h "early").
"""


def test_weekly_extra_run_is_classified_not_phantom_deviation(eval_session, admin):
    from core.agent import tools

    d = tools.job_schedule_adherence(eval_session, admin, job_id=5, days=3650)["data"]
    by_day = {r["at_utc"][:10]: r for r in d["runs"]}

    extra = by_day["2026-05-26"]
    assert extra["classification"] == "extra"
    assert extra["deviation_minutes"] is None          # NOT -4272

    assert d["extra_run_count"] >= 1
    # aggregate is computed over aligned runs only → small, sane numbers
    assert d["max_abs_deviation_minutes"] is not None
    assert d["max_abs_deviation_minutes"] < 120


def test_weekly_aligned_runs_get_small_signed_deviation(eval_session, admin):
    from core.agent import tools

    d = tools.job_schedule_adherence(eval_session, admin, job_id=5, days=3650)["data"]
    aligned = [r for r in d["runs"]
               if r["classification"] in ("on_time", "early", "late")]
    assert len(aligned) == 4
    assert all(abs(r["deviation_minutes"]) < 120 for r in aligned)


def test_weekday_daily_job_all_aligned(eval_session, admin):
    from core.agent import tools

    d = tools.job_schedule_adherence(eval_session, admin, job_id=1, days=3650)["data"]
    assert d["extra_run_count"] == 0
    assert all(r["classification"] in ("on_time", "early", "late") for r in d["runs"])
