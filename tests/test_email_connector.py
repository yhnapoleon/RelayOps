"""Unit tests for the email connector, address resolution, and on-duty dispatch.

No DB or mail server is touched: the SMTP backend is exercised against a fake
``smtplib.SMTP``, and the dispatch helper's user lookup / connector are
monkeypatched.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from core.integrations import email_connector as ec
from core.integrations.email_connector import (
    EmailConnector,
    EmailMessage,
    LoggingEmailBackend,
    SmtpEmailBackend,
    _build_backend,
)


def _cfg(**overrides):
    base = dict(
        email_backend="auto",
        email_smtp_host="",
        email_smtp_port=587,
        email_smtp_username="",
        email_smtp_password="",
        email_smtp_use_tls=True,
        email_smtp_timeout_seconds=10,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── backend selection ──────────────────────────────────────────────────────


def test_auto_without_smtp_or_runtime_falls_back_to_log(monkeypatch):
    backend = _build_backend(_cfg(email_backend="auto"))
    assert backend.name == "log"


def test_auto_does_not_enable_smtp_implicitly():
    backend = _build_backend(_cfg(email_backend="auto", email_smtp_host="smtp.example.com"))
    assert backend.name == "log"




def test_explicit_smtp_without_host_falls_back_to_log(monkeypatch):
    backend = _build_backend(_cfg(email_backend="smtp", email_smtp_host=""))
    assert backend.name == "log"




# ── connector guarantees ────────────────────────────────────────────────────


def test_disabled_connector_does_not_send():
    sent = {"called": False}

    class _Spy(LoggingEmailBackend):
        def send(self, message, *, from_address):
            sent["called"] = True
            return True

    conn = EmailConnector(_Spy(), from_address="noreply@corp", enabled=False)
    assert conn.send_email(to=["a@corp"], subject="s", body_text="b") is False
    assert sent["called"] is False


def test_no_recipients_returns_false():
    conn = EmailConnector(LoggingEmailBackend(), from_address="noreply@corp")
    assert conn.send(EmailMessage(to=[], subject="s", body_text="b")) is False


def test_backend_exception_is_swallowed():
    class _Boom(LoggingEmailBackend):
        def send(self, message, *, from_address):
            raise RuntimeError("smtp down")

    conn = EmailConnector(_Boom(), from_address="noreply@corp")
    assert conn.send_email(to=["a@corp"], subject="s", body_text="b") is False


def test_logging_backend_sends():
    conn = EmailConnector(LoggingEmailBackend(), from_address="noreply@corp")
    assert conn.send_email(to=["a@corp"], subject="s", body_text="b") is True


def test_send_times_out_returns_false():
    import time as _time

    class _Slow(LoggingEmailBackend):
        def send(self, message, *, from_address):
            _time.sleep(1.0)
            return True

    conn = EmailConnector(_Slow(), from_address="noreply@corp", send_timeout=0.2)
    assert conn.send_email(to=["a@corp"], subject="s", body_text="b") is False


# ── send circuit breaker ─────────────────────────────────────────────────────


def test_send_circuit_trips_and_self_heals():
    now = {"t": 1000.0}
    cb = ec._SendCircuit(fail_threshold=2, cooldown_seconds=30, monotonic=lambda: now["t"])

    # CLOSED: acquire hands out the pool, not a probe.
    executor, is_probe = cb.acquire()
    assert executor is not None and is_probe is False

    # One timeout isn't enough to trip (threshold is 2).
    cb.record(timed_out=True, was_probe=False)
    assert cb.acquire()[0] is not None

    # Second consecutive timeout trips it OPEN -> sends short-circuit (no pool).
    cb.record(timed_out=True, was_probe=False)
    executor, is_probe = cb.acquire()
    assert executor is None and is_probe is False

    # After the cooldown exactly one send is let through as a probe...
    now["t"] += 31
    executor, is_probe = cb.acquire()
    assert executor is not None and is_probe is True
    # ...while a concurrent send is still short-circuited.
    assert cb.acquire()[0] is None

    # Probe succeeds -> CLOSED, sends resume.
    cb.record(timed_out=False, was_probe=True)
    executor, is_probe = cb.acquire()
    assert executor is not None and is_probe is False


def test_send_circuit_reopens_when_probe_times_out():
    now = {"t": 0.0}
    cb = ec._SendCircuit(fail_threshold=1, cooldown_seconds=10, monotonic=lambda: now["t"])

    cb.record(timed_out=True, was_probe=False)  # threshold 1 -> trips at once
    assert cb.acquire()[0] is None

    now["t"] += 11
    assert cb.acquire()[1] is True  # half-open probe

    cb.record(timed_out=True, was_probe=True)  # probe fails -> re-open, fresh cooldown
    assert cb.acquire()[0] is None
    now["t"] += 11
    assert cb.acquire()[1] is True  # probes again only after the new cooldown


def test_connector_short_circuits_after_timeout(monkeypatch):
    import time as _time

    calls = {"n": 0}

    class _Slow(LoggingEmailBackend):
        def send(self, message, *, from_address):
            calls["n"] += 1
            _time.sleep(0.5)
            return True

    # Trip after a single timeout, long cooldown so the next send short-circuits.
    monkeypatch.setattr(
        ec, "_send_circuit", ec._SendCircuit(fail_threshold=1, cooldown_seconds=100)
    )
    conn = EmailConnector(_Slow(), from_address="n@corp", send_timeout=0.1)

    # First send times out (backend did start) and trips the circuit.
    assert conn.send_email(to=["a@corp"], subject="s", body_text="b") is False
    assert calls["n"] == 1

    # Second send is short-circuited: returns fast, backend NOT invoked again.
    start = _time.monotonic()
    assert conn.send_email(to=["a@corp"], subject="s", body_text="b") is False
    assert _time.monotonic() - start < 0.1
    assert calls["n"] == 1


# ── SMTP backend (fake transport) ───────────────────────────────────────────


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, mime, from_addr=None, to_addrs=None):
        self.sent.append((mime, from_addr, to_addrs))


def test_smtp_backend_starttls_login_and_send(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(ec.smtplib, "SMTP", _FakeSMTP)
    backend = SmtpEmailBackend(
        host="smtp.corp", port=587, username="u", password="p", use_tls=True, timeout=5
    )
    msg = EmailMessage(to=["a@corp"], subject="s", body_text="b", body_html="<p>b</p>", cc=["c@corp"])
    assert backend.send(msg, from_address="noreply@corp") is True

    inst = _FakeSMTP.instances[-1]
    assert inst.started_tls is True
    assert inst.logged_in == ("u", "p")
    mime, from_addr, to_addrs = inst.sent[-1]
    assert from_addr == "noreply@corp"
    assert set(to_addrs) == {"a@corp", "c@corp"}
    assert mime["Subject"] == "s"


def test_smtp_backend_skips_login_when_no_username(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(ec.smtplib, "SMTP", _FakeSMTP)
    backend = SmtpEmailBackend(host="smtp.corp", port=25, use_tls=False)
    backend.send(EmailMessage(to=["a@corp"], subject="s", body_text="b"), from_address="n@corp")
    inst = _FakeSMTP.instances[-1]
    assert inst.started_tls is False
    assert inst.logged_in is None


# ── runtime backend mapping (runtime not importable locally, so use a fake) ──────










# ── on-duty dispatch gating ─────────────────────────────────────────────────


def _issue(**overrides):
    base = dict(
        id=42,
        type="app_offline",
        title="App down",
        description="serving url 503",
        sla_deadline=datetime(2026, 5, 28, 12, 0, 0),
        support_group_name="Inventory Ops",
        product_id=7,
        job_id=None,
        app_id=3,
        assignee_id=99,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingConnector:
    def __init__(self):
        self.calls = []

    def send_email_status(self, **kwargs):
        self.calls.append(kwargs)
        return ec.SEND_OK


@pytest.fixture
def patched_dispatch(monkeypatch):
    from core.issue_management import email_dispatch as ed

    recorder = _RecordingConnector()
    monkeypatch.setattr(ed, "get_user_by_id", lambda db, uid: SimpleNamespace(
        id=uid, username="relayopsmember1", display_name="Ops One", email="relayops1@example.com"
    ))
    monkeypatch.setattr(ec, "get_email_connector", lambda: recorder)
    return ed, recorder


def test_dispatch_sends_for_ops_issue_with_assignee(patched_dispatch):
    ed, recorder = patched_dispatch
    status, addr = ed.email_on_duty_about_issue(db=None, issue=_issue())
    assert status == ed.EMAIL_SENT
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    # A stored directory address is authoritative; do not guess from the name.
    primary = "relayops1@example.com"
    assert call["to"] == [primary]
    assert addr == primary
    assert "App down" in call["subject"]
    assert "#42" in call["body_text"]
    assert call["body_html"]


def test_dispatch_skips_non_ops_issue(patched_dispatch):
    ed, recorder = patched_dispatch
    status, _ = ed.email_on_duty_about_issue(db=None, issue=_issue(type="handover_review"))
    assert status == ed.EMAIL_SKIPPED
    assert recorder.calls == []


def test_dispatch_skips_issue_without_assignee(patched_dispatch):
    ed, recorder = patched_dispatch
    status, _ = ed.email_on_duty_about_issue(db=None, issue=_issue(assignee_id=None))
    assert status == ed.EMAIL_SKIPPED
    assert recorder.calls == []


def test_dispatch_failed_when_backend_returns_false(monkeypatch, patched_dispatch):
    ed, recorder = patched_dispatch

    class _Failing:
        def send_email_status(self, **kwargs):
            return ec.SEND_REJECTED

    monkeypatch.setattr(ec, "get_email_connector", lambda: _Failing())
    status, addr = ed.email_on_duty_about_issue(db=None, issue=_issue())
    assert status == ed.EMAIL_FAILED
    assert addr is None


def test_dispatch_stops_on_transport_timeout(monkeypatch, patched_dispatch):
    ed, _ = patched_dispatch

    class _Timing:
        def __init__(self):
            self.calls = 0

        def send_email_status(self, **kwargs):
            self.calls += 1
            return ec.SEND_TIMEOUT

    timing = _Timing()
    monkeypatch.setattr(ec, "get_email_connector", lambda: timing)
    status, addr = ed.email_on_duty_about_issue(db=None, issue=_issue())
    assert status == ed.EMAIL_FAILED
    assert addr is None
    # A timeout means the transport is unhealthy: stop after the first candidate
    # instead of burning a send worker on every fallback address.
    assert timing.calls == 1


def test_assignee_email_note_text():
    from core.issue_management import email_dispatch as ed

    assert "sent" in ed.assignee_email_note(ed.EMAIL_SENT).lower()
    assert "could not be sent" in ed.assignee_email_note(ed.EMAIL_FAILED).lower()
    assert ed.assignee_email_note(ed.EMAIL_SKIPPED) == ""
    # The success line names the address the email went to when known.
    note = ed.assignee_email_note(ed.EMAIL_SENT, "demo001@example.com")
    assert "demo001@example.com" in note
    # Skipped/failed never leak an address even if one is passed.
    assert ed.assignee_email_note(ed.EMAIL_SKIPPED, "demo001@example.com") == ""


def test_dispatch_issue_email_sends_then_annotates(monkeypatch):
    from core.issue_management import email_dispatch as ed

    seen = {}
    # Avoid touching a real DB: stub get_db and the two collaborators.
    monkeypatch.setattr(ed, "get_db", lambda: "DB")
    monkeypatch.setattr(ed, "_reload_issue_for_email", lambda db, issue_id: None)
    monkeypatch.setattr(ed, "_record_email_audit", lambda *args: None)
    monkeypatch.setattr(
        ed, "email_on_duty_about_issue", lambda db, issue: (ed.EMAIL_SENT, "SamTaylor@example.com")
    )
    monkeypatch.setattr(
        ed,
        "_annotate_assignee_notification",
        lambda db, issue_id, assignee_id, status, to_address=None: seen.update(
            db=db, issue_id=issue_id, assignee_id=assignee_id, status=status, to_address=to_address
        ),
    )
    # Dispatch is fire-and-forget: it returns a Future for the background
    # send + annotate, so await it before asserting the outcome.
    future = ed.dispatch_issue_email(_issue())
    assert future.result(timeout=5) == ed.EMAIL_SENT
    assert seen == {
        "db": "DB",
        "issue_id": 42,
        "assignee_id": 99,
        "status": ed.EMAIL_SENT,
        "to_address": "SamTaylor@example.com",
    }


def test_compose_bodies_includes_link_when_present():
    from core.issue_management import email_dispatch as ed

    url = "https://relayops.example/?tab=projects&projectView=assets&projectId=11&productId=8"
    text, html = ed._compose_bodies(_issue(), "Ops One", url)
    assert url in text
    assert url in html
    # Omitted -> no deep link surfaced.
    text2, html2 = ed._compose_bodies(_issue(), "Ops One")
    assert "Open this product" not in text2
    assert "Open this product" not in html2


def test_build_relayops_link(monkeypatch):
    from core.issue_management import email_dispatch as ed

    # No base_url configured -> no link.
    monkeypatch.setattr(ed, "get_config", lambda: SimpleNamespace(app_base_url=""))
    assert ed._build_relayops_link(db=None, issue=_issue(project_id=11)) is None

    # base_url + product/project on the issue -> full deep link (no DB needed).
    monkeypatch.setattr(ed, "get_config", lambda: SimpleNamespace(app_base_url="https://relayops.example"))
    link = ed._build_relayops_link(db=None, issue=_issue(product_id=8, project_id=11))
    assert link == "https://relayops.example/?tab=projects&projectView=assets&projectId=11&productId=8"

    # No product -> no link even with a base_url.
    assert ed._build_relayops_link(db=None, issue=_issue(product_id=None, project_id=None)) is None


def test_dispatch_issue_email_handles_none():
    from core.issue_management import email_dispatch as ed

    # Nothing to send -> no work scheduled, no Future.
    assert ed.dispatch_issue_email(None) is None


def test_resolve_user_email_candidates_order(monkeypatch):
    import core.config as config_module
    from core.services.user_service import resolve_user_email, resolve_user_email_candidates

    domain, fb = "example.com", "directory.example.com"
    monkeypatch.setattr(config_module, "get_config", lambda: SimpleNamespace(
        email_domain=domain, email_fallback_domain=fb))
    # Prefer stored directory mail, then explicitly configured name/id fallbacks.
    user = SimpleNamespace(
        username="demo001", display_name="Sam Taylor", email="sam@external.example.com"
    )
    cands = resolve_user_email_candidates(user)
    assert cands == ["sam@external.example.com", f"SamTaylor@{domain}", f"demo001@{fb}"]
    assert resolve_user_email(user) == "sam@external.example.com"
    # No display name -> primary skipped, the username fallback becomes first.
    no_name = SimpleNamespace(username="demo001", display_name="", email=None)
    assert resolve_user_email(no_name) == f"demo001@{fb}"
    # Nothing derivable -> empty / None.
    assert resolve_user_email_candidates(None) == []
    assert resolve_user_email(None) is None


def test_dispatch_skips_when_no_address(monkeypatch):
    from core.issue_management import email_dispatch as ed

    recorder = _RecordingConnector()
    # user with no display_name, username, or email -> no candidates at all
    monkeypatch.setattr(ed, "get_user_by_id", lambda db, uid: SimpleNamespace(
        id=uid, username="", display_name="", email=None
    ))
    monkeypatch.setattr(ec, "get_email_connector", lambda: recorder)
    status, addr = ed.email_on_duty_about_issue(db=None, issue=_issue())
    assert status == ed.EMAIL_FAILED
    assert recorder.calls == []
