"""Compose and send the on-duty Ops email for a newly created issue.

This is the bridge between issue creation and the transport-agnostic
:mod:`core.integrations.email_connector`. It only fires for operational issue
types (job / app / MMP) that carry an assignee — i.e. the on-duty Ops member
resolved from the schedule — matching the requirement to "email the Ops on
duty about the issues".

Everything here is best-effort: any failure is logged and swallowed so a mail
problem can never block issue creation.
"""

from __future__ import annotations

import concurrent.futures
from typing import Optional

from core.config import get_config
from core.logging import get_logger
from core.models.database import Database, get_db
from core.models.entities import Issue, Notification
from core.services.user_service import get_user_by_id, resolve_user_email_candidates

logger = get_logger(__name__)

# Outcome of an on-duty email attempt, surfaced in the assignee's in-app
# notification so the on-duty member knows whether to also expect an email.
EMAIL_SENT = "sent"        # delivered to the transport without error
EMAIL_FAILED = "failed"    # attempted (ops issue + assignee) but couldn't send
EMAIL_SKIPPED = "skipped"  # not attempted: non-ops, no assignee, or email disabled


def _humanize(issue_type: str) -> str:
    return (issue_type or "").replace("_", " ").title()


def _compose_subject(issue: Issue) -> str:
    prefix = get_config().email_subject_prefix
    return f"{prefix} {_humanize(issue.type)}: {issue.title}".strip()


def _build_relayops_link(db: Database, issue: Optional[Issue]) -> Optional[str]:
    """Absolute deep link to this issue's product in RelayOps, or None.

    Returns ``None`` when ``app.base_url`` isn't configured or the product /
    project can't be resolved. The link opens the product's assets view, the
    same URL the browser uses (``?tab=projects&projectView=assets&projectId=
    &productId=``). project_id is read off the issue when present, else
    resolved from the product. Best-effort: never raises.
    """
    base = get_config().app_base_url
    if not base or issue is None:
        return None
    product_id = getattr(issue, "product_id", None)
    project_id = getattr(issue, "project_id", None)
    if product_id is None:
        return None
    if project_id is None:
        try:
            from core.models.entities import Product

            session = db.get_session()
            try:
                product = session.query(Product).filter(Product.id == product_id).first()
                project_id = product.project_id if product else None
            finally:
                session.close()
        except Exception:
            project_id = None
    if project_id is None:
        return None
    return (
        f"{base}/?tab=projects&projectView=assets"
        f"&projectId={project_id}&productId={product_id}"
    )


def _compose_bodies(issue: Issue, assignee_display: str, link: Optional[str] = None) -> tuple[str, str]:
    """Return (plain_text, html) bodies for the issue email.

    ``link`` is an optional absolute RelayOps deep link to the issue's
    product; when given it's surfaced as a line / button so the assignee can
    jump straight to the asset.
    """
    sla = issue.sla_deadline.strftime("%Y-%m-%d %H:%M UTC") if issue.sla_deadline else "not set"

    rows = [
        ("Issue ID", f"#{issue.id}"),
        ("Type", _humanize(issue.type)),
        ("Title", issue.title or ""),
        ("SLA deadline", sla),
    ]
    if issue.support_group_name:
        rows.append(("Support group", issue.support_group_name))
    if issue.product_id is not None:
        rows.append(("Product ID", str(issue.product_id)))
    if issue.job_id is not None:
        rows.append(("Job ID", str(issue.job_id)))
    if issue.app_id is not None:
        rows.append(("App ID", str(issue.app_id)))

    text_lines = [
        f"Hi {assignee_display},",
        "",
        "A new operational issue has been assigned to you as the on-duty Ops member.",
        "",
    ]
    text_lines += [f"{label}: {value}" for label, value in rows]
    if issue.description:
        text_lines += ["", "Details:", issue.description]
    text_lines += ["", "Please review and action this issue in RelayOps."]
    if link:
        text_lines += [f"Open this product in RelayOps: {link}"]
    text_lines += ["— RelayOps"]
    text_body = "\n".join(text_lines)

    table_rows = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#555;'>{label}</td>"
        f"<td style='padding:2px 0;'><strong>{value}</strong></td></tr>"
        for label, value in rows
    )
    desc_html = (
        f"<p style='margin:12px 0 0;'><strong>Details</strong></p>"
        f"<pre style='white-space:pre-wrap;font-family:inherit;margin:4px 0;'>{issue.description}</pre>"
        if issue.description
        else ""
    )
    link_html = (
        f"<p style='margin-top:16px;'>"
        f"<a href='{link}' style='display:inline-block;padding:8px 16px;"
        f"background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;'>"
        f"Open this product in RelayOps</a></p>"
        if link
        else ""
    )
    html_body = (
        f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#222;'>"
        f"<p>Hi {assignee_display},</p>"
        f"<p>A new operational issue has been assigned to you as the "
        f"<strong>on-duty Ops member</strong>.</p>"
        f"<table style='border-collapse:collapse;'>{table_rows}</table>"
        f"{desc_html}"
        f"<p style='margin-top:16px;'>Please review and action this issue in RelayOps.</p>"
        f"{link_html}"
        f"<p style='color:#888;'>— RelayOps</p>"
        f"</div>"
    )
    return text_body, html_body


def email_on_duty_about_issue(db: Database, issue: Optional[Issue]) -> tuple[str, Optional[str]]:
    """Email the on-duty Ops assignee about ``issue``.

    Tries each resolved address in turn (primary display-name form first, then
    the username fallback, then the stored email address) and stops at the first one the
    transport accepts. Returns ``(status, sent_address)`` where status is one of
    :data:`EMAIL_SENT` / :data:`EMAIL_FAILED` / :data:`EMAIL_SKIPPED` and
    ``sent_address`` is the address the email actually went to (None unless
    sent). ``EMAIL_SKIPPED`` means it wasn't attempted (non-operational issue,
    no assignee, or email globally disabled). Never raises.
    """
    if issue is None or issue.assignee_id is None:
        return EMAIL_SKIPPED, None

    # Lazy import to avoid a circular dependency at module load
    # (issue_engine imports this module's caller).
    from core.issue_management.issue_engine import OPS_ISSUE_TYPES

    if issue.type not in OPS_ISSUE_TYPES:
        return EMAIL_SKIPPED, None

    if not get_config().email_enabled:
        return EMAIL_SKIPPED, None

    try:
        assignee = get_user_by_id(db, issue.assignee_id)
        candidates = resolve_user_email_candidates(assignee)
        if not candidates:
            logger.warning(
                "[email] no address for on-duty assignee user_id={} (issue #{})",
                issue.assignee_id,
                issue.id,
            )
            return EMAIL_FAILED, None

        from core.integrations.email_connector import (
            SEND_OK,
            SEND_TIMEOUT,
            get_email_connector,
        )

        display = (assignee.display_name or assignee.username) if assignee else "Ops member"
        text_body, html_body = _compose_bodies(issue, display, _build_relayops_link(db, issue))
        connector = get_email_connector()
        subject = _compose_subject(issue)
        for address in candidates:
            result = connector.send_email_status(
                to=[address],
                subject=subject,
                body_text=text_body,
                body_html=html_body,
            )
            if result == SEND_OK:
                logger.info(
                    "[email] on-duty issue email sent: issue #{} type={} to={}",
                    issue.id,
                    issue.type,
                    address,
                )
                return EMAIL_SENT, address
            if result == SEND_TIMEOUT:
                # The transport itself is unhealthy (relay unreachable / slow):
                # the next address would just time out again and tie up another
                # send worker, so stop walking candidates here.
                logger.warning(
                    "[email] on-duty send to {} timed out (issue #{}); transport "
                    "unhealthy, not trying remaining candidates",
                    address,
                    issue.id,
                )
                break
            logger.warning(
                "[email] on-duty send to {} failed (issue #{}); trying next candidate",
                address,
                issue.id,
            )
        return EMAIL_FAILED, None
    except Exception:
        logger.opt(exception=True).error(
            "[email] failed to dispatch on-duty email for issue #{}", getattr(issue, "id", "?")
        )
        return EMAIL_FAILED, None


def assignee_email_note(status: str, to_address: Optional[str] = None) -> str:
    """Human-readable suffix appended to the assignee's issue notification.

    On success the line names the address the email actually went to so the
    on-duty member (and anyone reading the notification) can confirm the
    hand-off landed. ``to_address`` is optional for back-compat; when absent the
    line omits the address. Rendered with leading blank lines because the
    notification body is shown with ``whitespace-pre-line`` in the UI.
    """
    if status == EMAIL_SENT:
        if to_address:
            return f"\n\n✅ A notification email was sent to the assignee at {to_address}."
        return "\n\n✅ A notification email was sent to the assignee."
    if status == EMAIL_FAILED:
        return "\n\n⚠️ The notification email could not be sent — please keep an eye on RelayOps."
    return ""


def _annotate_assignee_notification(
    db: Database, issue_id: int, assignee_id: int, status: str, to_address: Optional[str] = None
) -> None:
    """Append the email outcome to the assignee's issue notification.

    ``to_address`` is the address the email actually went to (from the send
    step) so the success line can name the real recipient — including when the
    username fallback was used. Runs in its own short-lived session so it never
    piggybacks on (or holds open) the issue-creation transaction. Best-effort:
    logs and swallows.
    """
    note = assignee_email_note(status, to_address)
    if not note or issue_id is None or assignee_id is None:
        return
    session = db.get_session()
    try:
        notif = (
            session.query(Notification)
            .filter(
                Notification.related_entity_type == "issue",
                Notification.related_entity_id == issue_id,
                Notification.user_id == assignee_id,
                # Covers both the first assignment and a later reassignment —
                # both send the on-duty email and want the outcome appended.
                Notification.type.in_(("issue_assigned", "issue_reassigned")),
            )
            .order_by(Notification.id.desc())
            .first()
        )
        if notif is not None and note.strip() not in (notif.message or ""):
            notif.message = (notif.message or "") + note
            session.commit()
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning(
            "[email] failed to annotate notification for issue #{}", issue_id
        )
    finally:
        session.close()


# On-duty emails are sent on this pool, not on the caller's thread. A slow /
# unreachable relay used to back-pressure the caller (an API request worker or
# the monitoring tick) for up to send_timeout × candidate-count seconds, which —
# as those threads piled up — stalled unrelated requests like login. Dispatch is
# now fire-and-forget so issue creation / reassignment / the tick return at once.
_dispatch_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="email-dispatch"
)


def dispatch_issue_email(issue: Optional[Issue]) -> Optional["concurrent.futures.Future"]:
    """Schedule the on-duty email for a *committed* issue, fire-and-forget.

    Returns immediately: the send + notification annotation run on a background
    thread so the caller is never held by a slow/unreachable mail relay. Call
    this AFTER the issue transaction has committed and its connection released —
    the background work opens its own short-lived sessions.

    Returns the scheduling ``Future`` (``None`` when there's nothing to send) so
    callers that need to await the outcome — e.g. tests — can; the production
    callers ignore it. Never raises.
    """
    if issue is None:
        return None
    return _dispatch_executor.submit(_dispatch_issue_email_blocking, issue)


def _reload_issue_for_email(db: Database, issue_id: Optional[int]) -> Optional[Issue]:
    """Load a fresh, fully-populated copy of the issue by id, detached.

    The caller passes the ORM ``issue`` it created, but by the time this
    background send runs that instance is detached and its ``assignee_id`` is
    whatever was cached at creation time — which goes STALE if the issue was
    reassigned in between (e.g. created during a schedule gap → assigned to the
    fallback → reassigned to the now-on-duty member). Re-reading by id makes the
    email recipient track the COMMITTED assignee, exactly like the in-app
    notification. ``expunge`` detaches the row with its columns already loaded so
    the email composer can read them after the session closes.
    """
    if issue_id is None:
        return None
    session = db.get_session()
    try:
        issue = session.query(Issue).filter(Issue.id == issue_id).first()
        if issue is not None:
            session.expunge(issue)
        return issue
    except Exception:
        logger.opt(exception=True).warning(
            "[email] failed to reload issue #{} for send; using detached copy", issue_id
        )
        return None
    finally:
        session.close()


def _dispatch_issue_email_blocking(issue: Issue) -> str:
    """Send the on-duty email and record the outcome on the assignee's
    notification. Runs on :data:`_dispatch_executor`; returns the email status."""
    db = get_db()
    # Re-read the issue fresh so the recipient is the current committed assignee,
    # not a value cached on the detached instance handed to us at creation time.
    fresh = _reload_issue_for_email(db, getattr(issue, "id", None)) or issue
    status, to_address = email_on_duty_about_issue(db, fresh)
    _annotate_assignee_notification(
        db, getattr(fresh, "id", None), getattr(fresh, "assignee_id", None), status, to_address
    )
    _record_email_audit(fresh, status, to_address)
    return status


def _record_email_audit(issue: Issue, status: str, to_address: Optional[str]) -> None:
    """Record the on-duty email outcome as an issue audit event so the timeline
    shows whether the notification email reached the assigned member.

    Only attempted (SENT / FAILED) outcomes are logged — a SKIPPED send (non-ops
    issue, no assignee, email disabled) isn't a timeline-worthy event. Renders on
    the timeline as "Email Sent" / "Email Failed" (see audit_logs router).
    """
    if status not in (EMAIL_SENT, EMAIL_FAILED):
        return
    issue_id = getattr(issue, "id", None)
    assignee_id = getattr(issue, "assignee_id", None)
    if issue_id is None or assignee_id is None:
        return
    try:
        from core.services.audit_service import log_audit, resolve_audit_user_id

        actor_id = resolve_audit_user_id(getattr(issue, "created_by", None) or assignee_id)
        if actor_id is None:
            return
        log_audit(
            user_id=actor_id,
            action="email_dispatch",
            entity_type="issue",
            entity_id=issue_id,
            new_value={
                "email_status": status,
                "to_address": to_address,
                "assignee_id": assignee_id,
            },
        )
    except Exception:
        logger.opt(exception=True).warning(
            "[email] failed to record email-dispatch audit for issue #{}", issue_id
        )
