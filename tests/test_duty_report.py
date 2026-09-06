"""Duty morning report — deterministic builder, 08:30 trigger + dedup,
LLM degradation, email dispatch outcomes, popup dismissal state."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register every table on Base
from core.agent import duty_summary
from core.agent.duty_summary import DutyReportSummary, generate_summary
from core.models.database import Base
from core.models.entities import (
    AUTO_CLOSE_RESOLUTION,
    AuditLog,
    DutyReport,
    Issue,
    IssueStatus,
    IssueType,
    Notification,
    Schedule,
)
from core.models.user import User, UserRole
from core.report import duty_report as dr

# Generation moment used across tests: 2026-06-12 08:31 local (UTC+8).
NOW_UTC = datetime(2026, 6, 12, 0, 31)
WINDOW_START = NOW_UTC - timedelta(hours=24)


class _FakeConfig:
    duty_report_enabled = True
    duty_report_send_time_parts = (8, 30)
    duty_report_tz_offset_minutes = 480
    duty_report_lookback_hours = 24
    duty_report_max_items = 50
    duty_report_llm_summary_enabled = True
    duty_report_email_enabled = True
    email_enabled = True
    email_subject_prefix = "[RelayOps]"
    app_base_url = ""
    llm_configured = False
    platform_owners = []


@pytest.fixture
def cfg(monkeypatch):
    config = _FakeConfig()
    monkeypatch.setattr(dr, "get_config", lambda: config)
    monkeypatch.setattr(duty_summary, "get_config", lambda: config)
    return config


@pytest.fixture
def db(monkeypatch, cfg):
    """In-memory DB patched into the report module + audit side-channel."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    session = factory()
    _seed(session)
    session.commit()
    session.close()

    class _Db:
        def get_session(self):
            return factory()

    fake = _Db()
    monkeypatch.setattr(dr, "get_db", lambda: fake)
    import core.services.audit_service as audit_service

    monkeypatch.setattr(audit_service, "get_db", lambda: fake)
    # Keep the fire-and-forget email thread out of unit tests; the email
    # tests call _send_report_email_blocking directly instead.
    monkeypatch.setattr(dr, "dispatch_report_email", lambda report_id: None)
    return factory


def _seed(s) -> None:
    s.add(User(id=1, username="boss", display_name="Boss", role=UserRole.ADMIN))
    s.add(User(id=2, username="dora", display_name="Dora", role=UserRole.RELAYOPS_MEMBER))

    # Dora is on duty across the generation moment.
    s.add(Schedule(
        id=1, assignee_id=2, created_by=1, duty_role="Primary",
        start_time=NOW_UTC - timedelta(hours=12), end_time=NOW_UTC + timedelta(hours=12),
    ))

    # #1 open, SLA 20 min out → at-risk (30-min window)
    s.add(Issue(id=1, type=IssueType.JOB_FAILED, status=IssueStatus.OPEN,
                title="ns_daily failed", created_by=1, assignee_id=2,
                created_at=NOW_UTC - timedelta(hours=2),
                sla_deadline=NOW_UTC + timedelta(minutes=20)))
    # #2 in_progress, SLA already blown → overdue
    s.add(Issue(id=2, type=IssueType.APP_OFFLINE, status=IssueStatus.IN_PROGRESS,
                title="scoring app down", created_by=1, assignee_id=2,
                created_at=NOW_UTC - timedelta(hours=5),
                sla_deadline=NOW_UTC - timedelta(minutes=10)))
    # #3 open MMP pending approval, comfortable SLA → mmp section only
    s.add(Issue(id=3, type=IssueType.MMP_RUN_PENDING_APPROVAL, status=IssueStatus.OPEN,
                title="run 77 pending approval", created_by=1,
                created_at=NOW_UTC - timedelta(hours=3),
                sla_deadline=NOW_UTC + timedelta(hours=20),
                external_url="https://mmp/model/7"))
    # #4 auto-closed inside the window → recovered + auto_closed
    s.add(Issue(id=4, type=IssueType.JOB_FAILED, status=IssueStatus.CLOSED,
                title="blip recovered", created_by=1,
                created_at=NOW_UTC - timedelta(hours=8),
                resolved_at=NOW_UTC - timedelta(hours=7),
                resolution_description=AUTO_CLOSE_RESOLUTION))
    # #5 manually resolved inside the window → recovered, not auto
    s.add(Issue(id=5, type=IssueType.MMP_DRIFT, status=IssueStatus.RESOLVED,
                title="drift handled", created_by=1, assignee_id=2,
                created_at=NOW_UTC - timedelta(hours=6),
                resolved_at=NOW_UTC - timedelta(hours=1),
                resolution_description="retrained"))
    # #6 resolved BEFORE the window → excluded everywhere
    s.add(Issue(id=6, type=IssueType.JOB_FAILED, status=IssueStatus.RESOLVED,
                title="ancient history", created_by=1,
                created_at=NOW_UTC - timedelta(days=3),
                resolved_at=NOW_UTC - timedelta(days=2)))
    # #7 handover review (non-ops type) created in window → NOT in created-24h
    s.add(Issue(id=7, type=IssueType.HANDOVER_REVIEW, status=IssueStatus.OPEN,
                title="handover v3", created_by=1,
                created_at=NOW_UTC - timedelta(hours=4)))

    s.add(AuditLog(user_id=2, action="resolve", entity_type="issue", entity_id=5,
                   timestamp=NOW_UTC - timedelta(hours=1)))
    s.add(AuditLog(user_id=2, action="update", entity_type="issue", entity_id=2,
                   timestamp=NOW_UTC - timedelta(hours=2)))
    s.add(AuditLog(user_id=1, action="create", entity_type="schedule", entity_id=1,
                   timestamp=NOW_UTC - timedelta(hours=10)))
    s.add(AuditLog(user_id=1, action="update", entity_type="project", entity_id=1,
                   timestamp=NOW_UTC - timedelta(days=2)))  # outside window


# ── section builder ───────────────────────────────────────────────────


def test_builder_sections(db, cfg):
    session = db()
    data = dr.build_report_data(session, WINDOW_START, NOW_UTC)
    session.close()

    open_ids = {i["id"] for i in data["open_issues"]["items"]}
    assert open_ids == {1, 2, 3, 7}
    assert data["open_issues"]["total"] == 4

    assert [i["id"] for i in data["sla"]["overdue"]["items"]] == [2]
    assert [i["id"] for i in data["sla"]["at_risk"]["items"]] == [1]
    at_risk = data["sla"]["at_risk"]["items"][0]
    assert at_risk["minutes_to_deadline"] == 20
    assert at_risk["assignee_name"] == "Dora"

    mmp = data["mmp_pending"]["items"]
    assert [i["id"] for i in mmp] == [3]
    assert mmp[0]["external_url"] == "https://mmp/model/7"

    created_ids = {i["id"] for i in data["last_24h"]["created"]["items"]}
    # Every ops issue born in the window — including the two that already
    # recovered (#4, #5). #7 (handover review, non-ops) is excluded.
    assert created_ids == {1, 2, 3, 4, 5}
    recovered = {i["id"]: i for i in data["last_24h"]["recovered"]["items"]}
    assert set(recovered) == {4, 5}
    assert recovered[4]["auto_closed"] is True
    assert recovered[5]["auto_closed"] is False

    activity = data["duty_activity"]
    assert activity["total_events"] == 3  # the day-old project update is out
    # Only issue rows / key actions are notable; the schedule create shows
    # up in activity_counts but not here.
    assert {ev["entity_id"] for ev in activity["notable_events"]} == {5, 2}
    assert any(row["username"] == "Dora" and row["count"] == 1
               and row["action"] == "resolve" for row in activity["activity_counts"])

    assert data["on_duty"] == [{
        "user_id": 2, "display_name": "Dora", "duty_role": "Primary",
        "shift_end": dr._iso(NOW_UTC + timedelta(hours=12)),
    }]

    stats = data["stats"]
    assert stats == {
        "open_total": 4, "in_progress_total": 1,
        "sla_overdue_count": 1, "sla_at_risk_count": 1,
        "mmp_pending_count": 1, "created_24h": 5,
        "recovered_24h": 2, "auto_closed_24h": 1, "audit_events_24h": 3,
    }


def test_builder_caps_keep_exact_totals(db, cfg):
    session = db()
    data = dr.build_report_data(session, WINDOW_START, NOW_UTC, max_items=1)
    session.close()
    assert data["open_issues"]["total"] == 4
    assert len(data["open_issues"]["items"]) == 1
    # Sorted by sla_deadline asc → the overdue issue leads the open list.
    assert data["open_issues"]["items"][0]["id"] == 2


# ── trigger + dedup ───────────────────────────────────────────────────


def test_trigger_before_send_time_no_report(db, cfg):
    assert dr.maybe_generate_duty_report(now_utc=datetime(2026, 6, 12, 0, 0)) is None
    session = db()
    assert session.query(DutyReport).count() == 0
    session.close()


def test_trigger_generates_once_per_day(db, cfg):
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert report is not None
    assert report.report_date == "2026-06-12"
    assert report.status == DutyReport.STATUS_READY
    assert report.recipient_user_ids == [2]
    assert report.llm_status == "skipped"  # llm_configured=False
    assert report.llm_summary is None

    # Same day, later tick → no duplicate.
    assert dr.maybe_generate_duty_report(now_utc=NOW_UTC + timedelta(minutes=5)) is None
    session = db()
    assert session.query(DutyReport).count() == 1
    session.close()


def test_trigger_catches_up_after_downtime(db, cfg):
    # App was down at 08:30; first tick at 11:00 local generates same-day.
    report = dr.maybe_generate_duty_report(now_utc=datetime(2026, 6, 12, 3, 0))
    assert report is not None and report.report_date == "2026-06-12"


def test_trigger_next_day_generates_again(db, cfg):
    assert dr.maybe_generate_duty_report(now_utc=NOW_UTC) is not None
    next_day = dr.maybe_generate_duty_report(now_utc=NOW_UTC + timedelta(days=1))
    assert next_day is not None and next_day.report_date == "2026-06-13"
    session = db()
    assert session.query(DutyReport).count() == 2
    session.close()


def test_trigger_loses_claim_race(db, cfg):
    session = db()
    session.add(DutyReport(report_date="2026-06-12", status=DutyReport.STATUS_GENERATING))
    session.commit()
    session.close()
    assert dr.maybe_generate_duty_report(now_utc=NOW_UTC) is None


def test_disabled_switch(db, cfg):
    cfg.duty_report_enabled = False
    assert dr.maybe_generate_duty_report(now_utc=NOW_UTC) is None


def test_failed_generation_releases_claim_for_retry(db, cfg, monkeypatch):
    real_builder = dr.build_report_data

    def _boom(*args, **kwargs):
        raise RuntimeError("section query exploded")

    monkeypatch.setattr(dr, "build_report_data", _boom)
    with pytest.raises(RuntimeError):
        dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    session = db()
    assert session.query(DutyReport).count() == 0  # claim cleaned up
    session.close()

    # Next tick retries and succeeds once the transient failure is gone.
    monkeypatch.setattr(dr, "build_report_data", real_builder)
    assert dr.maybe_generate_duty_report(now_utc=NOW_UTC + timedelta(minutes=5)) is not None


def test_force_generate_replaces_today(db, cfg):
    first = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    second = dr.force_generate(now_utc=NOW_UTC + timedelta(minutes=30))
    assert second.report_date == first.report_date
    session = db()
    assert session.query(DutyReport).count() == 1
    # Old notifications were replaced, not duplicated.
    assert (
        session.query(Notification)
        .filter(Notification.type == "duty_report", Notification.user_id == 2)
        .count()
        == 1
    )
    session.close()


# ── notifications / popup dismissal state ─────────────────────────────


def test_generation_writes_recipient_notifications(db, cfg):
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    session = db()
    notifs = session.query(Notification).filter(Notification.type == "duty_report").all()
    assert [(n.user_id, n.related_entity_type, n.related_entity_id, n.is_read)
            for n in notifs] == [(2, "duty_report", report.id, 0)]
    assert "Open 4" in notifs[0].message  # template headline carries the stats
    session.close()


def test_fallback_admin_when_no_on_duty(db, cfg, monkeypatch):
    session = db()
    session.query(Schedule).delete()
    session.commit()
    session.close()

    import core.issue_management.issue_engine as issue_engine

    boss = type("U", (), {"id": 1})()
    monkeypatch.setattr(issue_engine, "get_fallback_admin", lambda _db: boss)
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert report.recipient_user_ids == [1]


# ── LLM summary degradation ───────────────────────────────────────────


class _FakeStructured:
    def __init__(self, result):
        self.result = result
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeModel:
    def __init__(self, result):
        self.structured = _FakeStructured(result)

    def with_structured_output(self, schema):
        return self.structured


def test_llm_summary_ok(db, cfg):
    cfg.llm_configured = True
    summary = DutyReportSummary(headline="一切正常。", risk_highlights=[], handover_notes=[])
    result, status = generate_summary({"stats": {}}, model=_FakeModel(summary))
    assert status == "ok" and result.headline == "一切正常。"


def test_llm_summary_failure_degrades(db, cfg):
    cfg.llm_configured = True
    result, status = generate_summary({"stats": {}}, model=_FakeModel(TimeoutError("gw")))
    assert (result, status) == (None, "failed")


def test_llm_skipped_when_not_configured(cfg):
    cfg.llm_configured = False
    assert generate_summary({"stats": {}}) == (None, "skipped")


def test_report_ready_even_when_llm_raises(db, cfg, monkeypatch):
    def _explode(data):
        raise RuntimeError("llm import blew up")

    monkeypatch.setattr(duty_summary, "generate_summary", _explode)
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert report.status == DutyReport.STATUS_READY
    assert report.llm_summary is None and report.llm_status == "failed"


def test_llm_summary_used_in_report_and_notification(db, cfg, monkeypatch):
    summary = DutyReportSummary(headline="今天有 1 单 SLA 已超时。",
                                risk_highlights=["#2 scoring app down 已超时"],
                                handover_notes=["优先处理 #2"])
    monkeypatch.setattr(duty_summary, "generate_summary", lambda data: (summary, "ok"))
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert report.llm_status == "ok"
    assert report.llm_summary["headline"].startswith("今天有")
    session = db()
    notif = session.query(Notification).filter(Notification.type == "duty_report").one()
    assert notif.message == "今天有 1 单 SLA 已超时。"
    session.close()


# ── email rendering & dispatch ────────────────────────────────────────


class _RecorderConnector:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.sent = []

    def send_email_status(self, *, to, subject, body_text, body_html=None, cc=None):
        self.sent.append({"to": to, "subject": subject,
                          "text": body_text, "html": body_html})
        return self.results.pop(0) if self.results else "ok"


@pytest.fixture
def connector(monkeypatch):
    import core.integrations.email_connector as ec

    recorder = _RecorderConnector()
    monkeypatch.setattr(ec, "get_email_connector", lambda: recorder)
    return recorder


def test_email_sent_to_on_duty(db, cfg, connector):
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    status = dr._send_report_email_blocking(report.id)
    assert status == "sent"
    assert connector.sent, "no email left the connector"
    first = connector.sent[0]
    assert "Duty Morning Report 2026-06-12" in first["subject"]
    assert "dora" in first["to"][0].lower()
    assert "SLA Overdue" in first["text"] and "#2" in first["text"]
    assert "MMP Pending Approval" in first["html"]
    session = db()
    row = session.query(DutyReport).filter(DutyReport.id == report.id).one()
    assert row.email_status == "sent"
    session.close()


def test_email_transport_failure_keeps_report_ready(db, cfg, connector):
    # Every candidate address rejected → failed, but the report stays usable.
    connector.results = ["rejected"] * 10
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert dr._send_report_email_blocking(report.id) == "failed"
    session = db()
    row = session.query(DutyReport).filter(DutyReport.id == report.id).one()
    assert row.status == DutyReport.STATUS_READY
    assert row.email_status == "failed"
    session.close()


def test_email_timeout_stops_candidate_walk(db, cfg, connector):
    connector.results = ["timeout"]
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert dr._send_report_email_blocking(report.id) == "failed"
    assert len(connector.sent) == 1  # did not try the fallback address


def test_email_disabled_marks_skipped(db, cfg, connector):
    cfg.duty_report_email_enabled = False
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    assert dr._send_report_email_blocking(report.id) == "skipped"
    assert connector.sent == []


def test_render_template_only_omits_summary_block(db, cfg):
    report = dr.maybe_generate_duty_report(now_utc=NOW_UTC)
    text, html = dr.render_report_bodies(report)
    assert "[Summary]" not in text
    assert "Duty Morning Report 2026-06-12" in text
    assert "ns_daily failed" in html
