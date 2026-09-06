"""Daily duty morning report (值班晨报) — the scheduled-agent pipeline.

Every day shortly after the configured send time (default 08:30 local,
UTC+8 by default) the monitoring controller's tick calls
:func:`maybe_generate_duty_report`, which:

1. Builds the five report sections **deterministically** (open issues,
   SLA at-risk/overdue, pending MMP approvals, last-24h anomalies &
   recoveries, audit-log activity) — the LLM never touches the numbers.
2. Optionally asks the LLM for a short executive summary on top
   (``core.agent.duty_summary``); any LLM failure degrades to a
   template-only report and never blocks generation.
3. Persists one :class:`~core.models.entities.DutyReport` row per local
   day (UNIQUE ``report_date`` doubles as the restart / multi-worker
   dedup) plus one in-app ``duty_report`` notification per on-duty
   recipient (its ``is_read`` is the popup's "dismissed" state).
4. Fire-and-forget emails the report to the on-duty members through the
   existing email connector, after the report row has committed.

Everything here is best-effort in the same spirit as
:mod:`core.issue_management.email_dispatch`: a mail or LLM problem can
never take the monitoring tick down.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from core.config import get_config
from core.logging import get_logger
from core.models.database import get_db
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
from core.models.user import User
from core.services.audit_service import (
    log_audit,
    resolve_audit_user_id,
    serialize_notification,
)
from core.services.user_service import get_user_by_id, resolve_user_email_candidates

logger = get_logger(__name__)

# Issue types that count as "MMP 待审批" — the active model-governance
# signals actioned on the MMP platform itself (fairness / unapproved-exp
# are retired, see mmp_concern_cleanup_backfill).
MMP_PENDING_TYPES = (IssueType.MMP_RUN_PENDING_APPROVAL, IssueType.MMP_PENDING_REVIEW)

# Audit actions surfaced in the "notable events" list even for non-issue
# entities — the moments a handover reader actually cares about.
_NOTABLE_ACTIONS = ("escalate", "resolve", "false_positive", "approve", "reject", "handover")
_NOTABLE_EVENTS_LIMIT = 30

# Rows shown per section in the *email* body (the stored report keeps up
# to ``duty_report.max_items``; the email stays scannable).
_EMAIL_ROWS_PER_SECTION = 10


# ── local-time helpers (single source of truth for "today") ──────────────


def local_now(now_utc: Optional[datetime] = None) -> datetime:
    """Naive local wall-clock time per ``duty_report.tz_offset_minutes``."""
    now_utc = now_utc or datetime.utcnow()
    return now_utc + timedelta(minutes=get_config().duty_report_tz_offset_minutes)


def local_today(now_utc: Optional[datetime] = None) -> str:
    """Local calendar day (ISO ``YYYY-MM-DD``) — shared by the generator
    and the ``/today`` endpoint so the popup never disagrees with the
    scheduler about which day it is."""
    return local_now(now_utc).date().isoformat()


# ── section builder (deterministic; no LLM, no email) ────────────────────


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(sep=" ", timespec="minutes") if value else None


def _issue_brief(issue: Issue, users_by_id: dict) -> dict:
    assignee = users_by_id.get(issue.assignee_id)
    return {
        "id": issue.id,
        "type": issue.type,
        "status": issue.status,
        "title": issue.title or "",
        "assignee_id": issue.assignee_id,
        "assignee_name": (assignee.display_name or assignee.username) if assignee else None,
        "sla_deadline": _iso(issue.sla_deadline),
        "created_at": _iso(issue.created_at),
        "resolved_at": _iso(issue.resolved_at),
        "product_id": issue.product_id,
        "job_id": issue.job_id,
        "app_id": issue.app_id,
        "external_url": issue.external_url,
    }


def _capped(items: list, max_items: int) -> dict:
    return {"total": len(items), "items": items[:max_items]}


def build_report_data(
    session,
    window_start: datetime,
    window_end: datetime,
    *,
    max_items: Optional[int] = None,
) -> dict:
    """Assemble the five report sections + stats from one session.

    Pure queries — safe to call from tests with a seeded in-memory DB.
    All datetimes are serialized to strings so the result can go straight
    into the ``DutyReport.sections`` JSON column.
    """
    # Local import: issue_engine pulls in the whole issue-management stack;
    # keep module import light so the controller's lazy import stays cheap.
    from core.agent.knowledge import classify_handling_state
    from core.issue_management.issue_engine import OPS_ISSUE_TYPES

    max_items = max_items if max_items is not None else get_config().duty_report_max_items
    open_statuses = (IssueStatus.OPEN, IssueStatus.IN_PROGRESS)

    open_issues = (
        session.query(Issue)
        .filter(Issue.status.in_(open_statuses))
        .order_by(Issue.sla_deadline.asc().nullslast(), Issue.created_at.asc())
        .all()
    )
    created_24h = (
        session.query(Issue)
        .filter(Issue.created_at >= window_start, Issue.type.in_(OPS_ISSUE_TYPES))
        .order_by(Issue.created_at.desc())
        .all()
    )
    recovered_24h = (
        session.query(Issue)
        .filter(
            Issue.resolved_at >= window_start,
            Issue.status.in_((IssueStatus.RESOLVED, IssueStatus.CLOSED)),
            Issue.type.in_(OPS_ISSUE_TYPES),
        )
        .order_by(Issue.resolved_at.desc())
        .all()
    )
    active_schedules = (
        session.query(Schedule)
        .filter(Schedule.start_time <= window_end, Schedule.end_time >= window_end)
        .order_by(Schedule.duty_role.asc(), Schedule.assignee_id.asc())
        .all()
    )

    audit_count_rows = (
        session.query(AuditLog.user_id, AuditLog.action, AuditLog.entity_type, func.count(AuditLog.id))
        .filter(AuditLog.timestamp >= window_start)
        .group_by(AuditLog.user_id, AuditLog.action, AuditLog.entity_type)
        .all()
    )
    notable_rows = (
        session.query(AuditLog)
        .filter(
            AuditLog.timestamp >= window_start,
            (AuditLog.entity_type == "issue") | AuditLog.action.in_(_NOTABLE_ACTIONS),
        )
        .order_by(AuditLog.timestamp.desc())
        .limit(_NOTABLE_EVENTS_LIMIT)
        .all()
    )

    # One batched username lookup for every id the sections reference.
    user_ids = {i.assignee_id for i in open_issues + created_24h + recovered_24h if i.assignee_id}
    user_ids |= {row[0] for row in audit_count_rows}
    user_ids |= {row.user_id for row in notable_rows}
    user_ids |= {s.assignee_id for s in active_schedules}
    users_by_id = {
        u.id: u
        for u in (session.query(User).filter(User.id.in_(user_ids)).all() if user_ids else [])
    }

    def _name(user_id) -> Optional[str]:
        user = users_by_id.get(user_id)
        return (user.display_name or user.username) if user else None

    # SLA partition reuses the platform's single risk definition
    # (classify_handling_state, 30-min window) instead of re-deriving it.
    sla_overdue, sla_at_risk = [], []
    open_briefs = []
    for issue in open_issues:
        brief = _issue_brief(issue, users_by_id)
        if issue.sla_deadline:
            brief["minutes_to_deadline"] = int(
                (issue.sla_deadline - window_end).total_seconds() // 60
            )
        open_briefs.append(brief)
        state = classify_handling_state(issue, now=window_end)
        if state.get("sla_overdue"):
            sla_overdue.append(brief)
        elif state.get("sla_at_risk"):
            sla_at_risk.append(brief)

    mmp_pending = [b for b in open_briefs if b["type"] in MMP_PENDING_TYPES]

    recovered_briefs = []
    for issue in recovered_24h:
        brief = _issue_brief(issue, users_by_id)
        brief["auto_closed"] = (issue.resolution_description or "").strip() == AUTO_CLOSE_RESOLUTION
        recovered_briefs.append(brief)

    activity_counts = sorted(
        (
            {
                "user_id": uid,
                "username": _name(uid),
                "action": action,
                "entity_type": entity_type,
                "count": count,
            }
            for uid, action, entity_type, count in audit_count_rows
        ),
        key=lambda row: -row["count"],
    )
    notable_events = [
        {
            "timestamp": _iso(row.timestamp),
            "user_id": row.user_id,
            "username": _name(row.user_id),
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
        }
        for row in notable_rows
    ]

    on_duty = [
        {
            "user_id": sched.assignee_id,
            "display_name": _name(sched.assignee_id),
            "duty_role": sched.duty_role,
            "shift_end": _iso(sched.end_time),
        }
        for sched in active_schedules
    ]

    stats = {
        "open_total": len(open_issues),
        "in_progress_total": sum(1 for i in open_issues if i.status == IssueStatus.IN_PROGRESS),
        "sla_overdue_count": len(sla_overdue),
        "sla_at_risk_count": len(sla_at_risk),
        "mmp_pending_count": len(mmp_pending),
        "created_24h": len(created_24h),
        "recovered_24h": len(recovered_24h),
        "auto_closed_24h": sum(1 for b in recovered_briefs if b["auto_closed"]),
        "audit_events_24h": sum(row[3] for row in audit_count_rows),
    }

    return {
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "open_issues": _capped(open_briefs, max_items),
        "sla": {
            "overdue": _capped(sla_overdue, max_items),
            "at_risk": _capped(sla_at_risk, max_items),
        },
        "mmp_pending": _capped(mmp_pending, max_items),
        "last_24h": {
            "created": _capped([_issue_brief(i, users_by_id) for i in created_24h], max_items),
            "recovered": _capped(recovered_briefs, max_items),
        },
        "duty_activity": {
            "total_events": stats["audit_events_24h"],
            "activity_counts": activity_counts[:max_items],
            "notable_events": notable_events,
        },
        "on_duty": on_duty,
        "stats": stats,
    }


# ── scheduled trigger (called from the controller tick) ──────────────────


def maybe_generate_duty_report(*, now_utc: Optional[datetime] = None) -> Optional[DutyReport]:
    """Generate today's report iff it's past send time and none exists yet.

    Called every controller tick, so the effective trigger lateness is at
    most one tick interval. The UNIQUE ``report_date`` claim row makes
    this safe across restarts and multiple workers; a failed generation
    deletes its claim so the next tick retries. If the app was down at
    send time, the first tick after restart catches up the same day.
    """
    cfg = get_config()
    if not cfg.duty_report_enabled:
        return None
    now_utc = now_utc or datetime.utcnow()
    now_local = local_now(now_utc)
    if (now_local.hour, now_local.minute) < cfg.duty_report_send_time_parts:
        return None
    report_date = now_local.date().isoformat()

    session = get_db().get_session()
    try:
        if session.query(DutyReport).filter(DutyReport.report_date == report_date).first():
            return None
        claim = DutyReport(report_date=report_date, status=DutyReport.STATUS_GENERATING)
        session.add(claim)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
        try:
            report = _generate(session, claim, now_utc)
        except Exception:
            session.rollback()
            _delete_claim(report_date)
            raise
        dispatch_report_email(report.id)
        return report
    finally:
        session.close()


def force_generate(*, now_utc: Optional[datetime] = None) -> DutyReport:
    """Drop and regenerate today's report synchronously (admin/ops path)."""
    now_utc = now_utc or datetime.utcnow()
    report_date = local_today(now_utc)
    session = get_db().get_session()
    try:
        stale_ids = [
            row.id
            for row in session.query(DutyReport)
            .filter(DutyReport.report_date == report_date)
            .all()
        ]
        if stale_ids:
            session.query(Notification).filter(
                Notification.related_entity_type == "duty_report",
                Notification.related_entity_id.in_(stale_ids),
            ).delete(synchronize_session=False)
            session.query(DutyReport).filter(DutyReport.id.in_(stale_ids)).delete(
                synchronize_session=False
            )
            session.commit()
        claim = DutyReport(report_date=report_date, status=DutyReport.STATUS_GENERATING)
        session.add(claim)
        session.commit()
        report = _generate(session, claim, now_utc)
        dispatch_report_email(report.id)
        return report
    finally:
        session.close()


def _delete_claim(report_date: str) -> None:
    """Best-effort removal of a failed generation's claim row."""
    session = get_db().get_session()
    try:
        session.query(DutyReport).filter(
            DutyReport.report_date == report_date,
            DutyReport.status == DutyReport.STATUS_GENERATING,
        ).delete()
        session.commit()
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning(
            "duty report: could not clean up claim row for {}", report_date
        )
    finally:
        session.close()


def _generate(session, claim: DutyReport, now_utc: datetime) -> DutyReport:
    """Fill the claimed row: sections, LLM summary, recipients, notifications."""
    cfg = get_config()
    window_end = now_utc
    window_start = now_utc - timedelta(hours=cfg.duty_report_lookback_hours)

    data = build_report_data(
        session, window_start, window_end, max_items=cfg.duty_report_max_items
    )
    summary, llm_status = _llm_summary(data)

    # Email/popup audience: the on-duty roster at generation time, falling
    # back to the platform admin so the report always reaches someone.
    recipient_ids: list[int] = []
    for entry in data["on_duty"]:
        if entry["user_id"] not in recipient_ids:
            recipient_ids.append(entry["user_id"])
    if not recipient_ids:
        from core.issue_management.issue_engine import get_fallback_admin

        admin = get_fallback_admin(get_db())
        if admin is not None:
            recipient_ids = [admin.id]

    claim.window_start = window_start
    claim.window_end = window_end
    claim.sections = data
    claim.llm_summary = summary
    claim.llm_status = llm_status
    claim.recipient_user_ids = recipient_ids
    claim.status = DutyReport.STATUS_READY

    stats = data["stats"]
    headline = (summary or {}).get("headline") or (
        f"Open {stats['open_total']} · SLA overdue {stats['sla_overdue_count']} · "
        f"SLA at-risk {stats['sla_at_risk_count']} · MMP pending {stats['mmp_pending_count']} · "
        f"24h new {stats['created_24h']} / recovered {stats['recovered_24h']}"
    )

    created_notifs: list[Notification] = []
    for uid in recipient_ids:
        notif = Notification(
            user_id=uid,
            title=f"Duty Morning Report {claim.report_date}",
            message=headline,
            type="duty_report",
            related_entity_type="duty_report",
            related_entity_id=claim.id,
        )
        session.add(notif)
        created_notifs.append(notif)
    session.flush()
    session.commit()

    audit_user_id = resolve_audit_user_id(recipient_ids[0] if recipient_ids else None)
    if audit_user_id is not None:
        for notif in created_notifs:
            log_audit(
                user_id=audit_user_id,
                action="create",
                entity_type="notification",
                entity_id=notif.id,
                new_value=serialize_notification(notif),
            )
    logger.info(
        "duty report {} generated: open={} sla_risk={} mmp={} recipients={} llm={}",
        claim.report_date,
        stats["open_total"],
        stats["sla_at_risk_count"] + stats["sla_overdue_count"],
        stats["mmp_pending_count"],
        recipient_ids,
        llm_status,
    )
    return claim


def _llm_summary(data: dict) -> tuple[Optional[dict], str]:
    """Executive summary via the agent layer; degrades, never raises."""
    try:
        from core.agent.duty_summary import generate_summary

        summary, status = generate_summary(data)
        return (summary.model_dump() if summary is not None else None), status
    except Exception:
        logger.opt(exception=True).warning(
            "duty report: LLM summary unavailable; using template-only report"
        )
        return None, "failed"


# ── email rendering & dispatch ────────────────────────────────────────────


def _compose_subject(report: DutyReport) -> str:
    stats = (report.sections or {}).get("stats", {})
    prefix = get_config().email_subject_prefix
    return (
        f"{prefix} Duty Morning Report {report.report_date} — "
        f"Open {stats.get('open_total', 0)} / "
        f"SLA risk {stats.get('sla_overdue_count', 0) + stats.get('sla_at_risk_count', 0)} / "
        f"MMP pending {stats.get('mmp_pending_count', 0)}"
    ).strip()


def _issue_line(item: dict) -> str:
    bits = [f"#{item['id']}", f"[{item['type']}]", item.get("title") or ""]
    if item.get("assignee_name"):
        bits.append(f"→ {item['assignee_name']}")
    if item.get("minutes_to_deadline") is not None:
        m = item["minutes_to_deadline"]
        bits.append(f"(SLA overdue {-m} min)" if m < 0 else f"(SLA {m} min left)")
    return " ".join(b for b in bits if b)


def _section_rows_html(items: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "".join(
        f"<th style='text-align:left;padding:7px 14px;color:#475569;font-weight:600;"
        f"font-size:12px;text-transform:uppercase;letter-spacing:.03em;"
        f"border-bottom:2px solid #e2e8f0;'>{label}</th>"
        for _, label in columns
    )
    body = "".join(
        f"<tr style='background:{'#ffffff' if i % 2 == 0 else '#f8fafc'};'>"
        + "".join(
            f"<td style='padding:6px 14px;border-bottom:1px solid #eef2f6;color:#1e293b;'>"
            f"{item.get(key) if item.get(key) is not None else ''}</td>"
            for key, _ in columns
        )
        + "</tr>"
        for i, item in enumerate(items)
    )
    return (
        "<table style='border-collapse:collapse;font-size:13px;width:100%;"
        "border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;'>"
        f"<thead><tr style='background:#f1f5f9;'>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


# Stat pills shown under the header: (label, value-key, tone). Tone drives
# the pill colour only when the value is non-zero, mirroring the in-app chips.
_STAT_PILLS = [
    ("Open issues", "open_total", "neutral"),
    ("SLA overdue", "sla_overdue_count", "red"),
    ("SLA at-risk", "sla_at_risk_count", "amber"),
    ("MMP pending", "mmp_pending_count", "amber"),
    ("New 24h", "created_24h", "neutral"),
    ("Recovered 24h", "recovered_24h", "neutral"),
]
_PILL_TONES = {
    "red": ("#fef2f2", "#b91c1c", "#fecaca"),
    "amber": ("#fffbeb", "#b45309", "#fde68a"),
    "neutral": ("#f1f5f9", "#334155", "#e2e8f0"),
}


def _stat_pills_html(stats: dict) -> str:
    pills = []
    for label, key, tone in _STAT_PILLS:
        value = stats.get(key, 0)
        bg, fg, border = _PILL_TONES[tone if value else "neutral"]
        pills.append(
            f"<td style='padding:0 6px 8px 0;'>"
            f"<span style='display:inline-block;background:{bg};color:{fg};"
            f"border:1px solid {border};border-radius:8px;padding:6px 10px;font-size:13px;'>"
            f"<strong style='font-size:15px;'>{value}</strong> "
            f"<span style='color:#64748b;font-size:12px;'>{label}</span></span></td>"
        )
    return f"<table style='border-collapse:collapse;margin:14px 0 4px;'><tr>{''.join(pills)}</tr></table>"


def render_report_bodies(report: DutyReport) -> tuple[str, str]:
    """Return ``(plain_text, html)`` for the report email.

    Follows the inline-style conventions of
    ``email_dispatch._compose_bodies``. Each section shows at most
    :data:`_EMAIL_ROWS_PER_SECTION` rows plus an "…and N more" line; the
    full lists live in the stored report / the UI.
    """
    data = report.sections or {}
    stats = data.get("stats", {})
    summary = report.llm_summary or {}

    issue_columns = [
        ("id", "ID"),
        ("type", "Type"),
        ("title", "Title"),
        ("assignee_name", "Assignee"),
        ("sla_deadline", "SLA deadline"),
    ]

    def _section(title: str, section: dict, columns=None) -> tuple[list[str], str]:
        items = section.get("items", [])[:_EMAIL_ROWS_PER_SECTION]
        total = section.get("total", len(items))
        more = total - len(items)
        text_lines = [f"== {title} ({total}) =="]
        text_lines += [f"  {_issue_line(item)}" for item in items] or ["  (none)"]
        if more > 0:
            text_lines.append(f"  …and {more} more, see RelayOps")
        html_parts = [
            f"<h3 style='margin:22px 0 8px;font-size:15px;color:#0f172a;'>{title} "
            f"<span style='color:#94a3b8;font-weight:normal;'>({total})</span></h3>"
        ]
        if items:
            html_parts.append(_section_rows_html(items, columns or issue_columns))
            if more > 0:
                html_parts.append(
                    f"<p style='color:#94a3b8;margin:6px 0;font-size:12px;'>…and {more} more, see RelayOps</p>"
                )
        else:
            html_parts.append("<p style='color:#94a3b8;margin:4px 0;font-size:13px;'>(none)</p>")
        return text_lines, "".join(html_parts)

    on_duty = data.get("on_duty", [])
    duty_line = (
        ", ".join(
            f"{d.get('display_name') or d.get('user_id')} ({d.get('duty_role')})" for d in on_duty
        )
        or "Unscheduled (fell back to platform admin)"
    )

    text_lines = [
        f"Duty Morning Report {report.report_date}",
        f"On duty today: {duty_line}",
        f"Stats: Open {stats.get('open_total', 0)} · SLA overdue {stats.get('sla_overdue_count', 0)}"
        f" · SLA at-risk {stats.get('sla_at_risk_count', 0)} · MMP pending {stats.get('mmp_pending_count', 0)}"
        f" · 24h new {stats.get('created_24h', 0)} / recovered {stats.get('recovered_24h', 0)}",
        "",
    ]
    html_summary = ""
    if summary.get("headline"):
        text_lines += ["[Summary] " + summary["headline"]]
        text_lines += [f"  ⚠ {line}" for line in summary.get("risk_highlights", [])]
        text_lines += [f"  → {line}" for line in summary.get("handover_notes", [])]
        text_lines.append("")
        risks = "".join(
            f"<li style='margin:3px 0;'>{line}</li>" for line in summary.get("risk_highlights", [])
        )
        notes = "".join(
            f"<li style='margin:3px 0;'>{line}</li>" for line in summary.get("handover_notes", [])
        )
        html_summary = (
            f"<div style='background:#eff6ff;border-left:4px solid #2563eb;border-radius:6px;"
            f"padding:12px 16px;margin:16px 0;'>"
            f"<p style='margin:0 0 6px;color:#1e293b;'><strong>{summary['headline']}</strong></p>"
            + (f"<ul style='margin:6px 0;padding-left:20px;color:#b45309;'>{risks}</ul>" if risks else "")
            + (f"<ul style='margin:6px 0;padding-left:20px;color:#334155;'>{notes}</ul>" if notes else "")
            + "</div>"
        )

    sla = data.get("sla", {})
    sections_spec = [
        ("SLA Overdue", sla.get("overdue", {}), None),
        ("SLA At-Risk", sla.get("at_risk", {}), None),
        ("MMP Pending Approval", data.get("mmp_pending", {}), None),
        ("Open Issues", data.get("open_issues", {}), None),
        ("New Anomalies (last 24h)", data.get("last_24h", {}).get("created", {}), None),
        ("Recovered (last 24h)", data.get("last_24h", {}).get("recovered", {}), None),
    ]
    html_sections = []
    for title, section, columns in sections_spec:
        t_lines, h = _section(title, section, columns)
        text_lines += t_lines + [""]
        html_sections.append(h)

    activity = data.get("duty_activity", {})
    notable = activity.get("notable_events", [])[:_EMAIL_ROWS_PER_SECTION]
    text_lines.append(f"== Duty Activity ({activity.get('total_events', 0)} audit events) ==")
    text_lines += [
        f"  {ev.get('timestamp')} {ev.get('username') or ev.get('user_id')} "
        f"{ev.get('action')} {ev.get('entity_type')}#{ev.get('entity_id')}"
        for ev in notable
    ] or ["  (none)"]
    activity_columns = [
        ("timestamp", "Time"),
        ("username", "User"),
        ("action", "Action"),
        ("entity_type", "Entity"),
        ("entity_id", "ID"),
    ]
    html_sections.append(
        f"<h3 style='margin:22px 0 8px;font-size:15px;color:#0f172a;'>Duty Activity "
        f"<span style='color:#94a3b8;font-weight:normal;'>({activity.get('total_events', 0)} audit events)</span></h3>"
        + (_section_rows_html(notable, activity_columns) if notable else "<p style='color:#94a3b8;margin:4px 0;font-size:13px;'>(none)</p>")
    )

    link = get_config().app_base_url
    if link:
        text_lines += ["", f"Open RelayOps: {link}"]
    text_lines += ["", "— RelayOps"]

    link_html = (
        f"<p style='margin-top:20px;text-align:center;'>"
        f"<a href='{link}' style='display:inline-block;padding:10px 20px;"
        f"background:#2563eb;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;'>"
        f"Open RelayOps</a></p>"
        if link
        else ""
    )
    html_body = (
        f"<div style='margin:0;padding:24px 12px;background:#f1f5f9;"
        f"font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'>"
        f"<div style='max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;"
        f"overflow:hidden;border:1px solid #e2e8f0;'>"
        f"<div style='background:linear-gradient(135deg,#1d4ed8,#3b82f6);padding:22px 28px;color:#fff;'>"
        f"<div style='font-size:13px;opacity:.85;letter-spacing:.04em;'>🌅 Ops CENTER</div>"
        f"<h1 style='margin:4px 0 0;font-size:22px;font-weight:700;'>Duty Morning Report</h1>"
        f"<div style='font-size:14px;opacity:.9;margin-top:2px;'>{report.report_date}</div>"
        f"</div>"
        f"<div style='padding:20px 28px 28px;font-size:14px;color:#1e293b;'>"
        f"<p style='margin:0;color:#475569;'><strong>On duty today:</strong> {duty_line}</p>"
        f"{_stat_pills_html(stats)}"
        f"{html_summary}"
        f"{''.join(html_sections)}"
        f"{link_html}"
        f"<p style='color:#94a3b8;font-size:12px;text-align:center;margin:18px 0 0;'>"
        f"— RelayOps · automated duty report</p>"
        f"</div></div></div>"
    )
    return "\n".join(text_lines), html_body


def _send_to_user(connector, user, subject: str, body_text: str, body_html: str) -> bool:
    """Walk the user's candidate addresses; stop on transport timeout.

    Same loop as ``email_dispatch.email_on_duty_about_issue``: a timeout
    means the relay itself is unhealthy, so trying further candidates
    would only tie up more send workers.
    """
    from core.integrations.email_connector import SEND_OK, SEND_TIMEOUT

    candidates = resolve_user_email_candidates(user)
    if not candidates:
        logger.warning("[duty-report] no address for recipient user_id={}", user.id)
        return False
    for address in candidates:
        result = connector.send_email_status(
            to=[address], subject=subject, body_text=body_text, body_html=body_html
        )
        if result == SEND_OK:
            logger.info("[duty-report] sent to {}", address)
            return True
        if result == SEND_TIMEOUT:
            logger.warning(
                "[duty-report] send to {} timed out; transport unhealthy, "
                "not trying remaining candidates",
                address,
            )
            return False
        logger.warning("[duty-report] send to {} failed; trying next candidate", address)
    return False


# Report emails go through their own single worker, not the issue-email
# pool: a morning report is one batched send and must never compete with
# (or be poisoned by) the latency-sensitive on-duty issue alerts.
_report_email_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="duty-report-email"
)


def dispatch_report_email(report_id: int) -> Optional[concurrent.futures.Future]:
    """Fire-and-forget email of a *committed* report. Returns the Future
    so tests can await the outcome; production callers ignore it."""
    try:
        return _report_email_executor.submit(_send_report_email_blocking, report_id)
    except Exception:
        logger.opt(exception=True).error("[duty-report] could not schedule email send")
        return None


def _send_report_email_blocking(report_id: int) -> str:
    """Send the report to every recipient; record the outcome on the row."""
    cfg = get_config()
    db = get_db()
    session = db.get_session()
    try:
        report = session.query(DutyReport).filter(DutyReport.id == report_id).first()
        if report is None:
            return "skipped"
        if not (cfg.email_enabled and cfg.duty_report_email_enabled):
            report.email_status = "skipped"
            session.commit()
            return "skipped"

        users = [get_user_by_id(db, uid) for uid in (report.recipient_user_ids or [])]
        users = [u for u in users if u is not None]
        if not users:
            report.email_status = "failed"
            session.commit()
            return "failed"

        from core.integrations.email_connector import get_email_connector

        subject = _compose_subject(report)
        body_text, body_html = render_report_bodies(report)
        connector = get_email_connector()
        sent = sum(
            1 for user in users if _send_to_user(connector, user, subject, body_text, body_html)
        )
        status = "sent" if sent == len(users) else ("partial" if sent else "failed")
        report.email_status = status
        session.commit()
        logger.info(
            "[duty-report] email dispatch for {}: {} ({}/{} recipients)",
            report.report_date,
            status,
            sent,
            len(users),
        )
        return status
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("[duty-report] email dispatch failed")
        try:
            session.query(DutyReport).filter(DutyReport.id == report_id).update(
                {"email_status": "failed"}
            )
            session.commit()
        except Exception:
            session.rollback()
        return "failed"
    finally:
        session.close()
