"""Explicit SMTP or log-only notification transport, with timeout and circuit-breaker handling."""

from __future__ import annotations

import concurrent.futures
import smtplib
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage as _MimeMessage
from typing import Callable, List, Optional

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)

# Per-send outcome returned by :meth:`EmailConnector.send_status`. Lets a caller
# that tries several addresses tell a transport-level timeout (relay unhealthy —
# stop, every further address will time out too and poison another worker) apart
# from a plain rejection (e.g. a bad recipient — the next candidate is still
# worth a shot).
SEND_OK = "ok"
SEND_REJECTED = "rejected"
SEND_TIMEOUT = "timeout"

# Worker count for the send pool. Bounded so stuck sends can't spawn unbounded
# threads; a hung send still occupies its worker until the OS drops the socket,
# which is exactly why the circuit breaker below abandons a poisoned pool.
_SEND_WORKERS = 4


class _SendCircuit:
    """Circuit breaker around the send thread pool.

    A runtime/SMTP send can hang with no socket timeout; the pool worker running it
    can't be cancelled, so each hang permanently poisons a worker. Left alone, a
    dead relay eventually occupies all workers and every send wedges — and even
    after the relay recovers nothing sends until the process restarts.

    This breaker bounds that:
      * CLOSED    — sends run normally. ``fail_threshold`` consecutive timeouts
                    trip it OPEN.
      * OPEN      — for ``cooldown_seconds`` every send short-circuits instantly
                    (no worker used, no waiting). Tripping also swaps in a fresh
                    pool and abandons the poisoned one, so a later probe has free
                    workers to run on.
      * HALF-OPEN — after the cooldown, exactly one send is let through as a
                    probe. Success closes the circuit (sends resume, self-healed);
                    another timeout re-opens it for a fresh cooldown.

    Thread-safe: state is guarded by a lock; the actual send (and its blocking
    ``result(timeout=...)`` wait) happens outside the lock.
    """

    def __init__(self, *, fail_threshold: int, cooldown_seconds: float, monotonic=time.monotonic):
        self._fail_threshold = max(1, int(fail_threshold))
        self._cooldown = float(cooldown_seconds)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._executor = self._new_executor()
        self._consecutive_timeouts = 0
        self._opened_at: Optional[float] = None  # monotonic time we opened; None == CLOSED
        self._probe_in_flight = False  # a half-open probe is being tested

    @staticmethod
    def _new_executor() -> concurrent.futures.ThreadPoolExecutor:
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=_SEND_WORKERS, thread_name_prefix="email-send"
        )

    def acquire(self):
        """Decide whether a send may proceed.

        Returns ``(executor, is_probe)``: ``executor`` is ``None`` when the
        circuit is open and still cooling down (the caller must short-circuit as
        a timeout); ``is_probe`` is True when this call is the single half-open
        probe and must report its outcome via :meth:`record`.
        """
        with self._lock:
            if self._opened_at is None:
                return self._executor, False
            if self._monotonic() - self._opened_at < self._cooldown:
                return None, False
            if self._probe_in_flight:
                return None, False
            self._probe_in_flight = True
            return self._executor, True

    def record(self, *, timed_out: bool, was_probe: bool) -> None:
        """Fold a finished send's outcome back into the circuit state."""
        with self._lock:
            if was_probe:
                self._probe_in_flight = False
            if timed_out:
                self._consecutive_timeouts += 1
                # Trip on first crossing the threshold; re-open if a half-open
                # probe timed out (relay still down).
                if self._opened_at is None:
                    if self._consecutive_timeouts >= self._fail_threshold:
                        self._trip_locked()
                else:
                    self._trip_locked()
            else:
                # Any prompt return (success or a fast rejection) means the
                # transport is responsive — reset and, if we were open, heal.
                self._consecutive_timeouts = 0
                if self._opened_at is not None:
                    self._opened_at = None
                    logger.info("[email] send circuit CLOSED — relay healthy again, sends resumed")

    def _trip_locked(self) -> None:
        self._opened_at = self._monotonic()
        self._consecutive_timeouts = 0
        self._probe_in_flight = False
        # Abandon the (possibly poisoned) pool so a later probe runs on fresh
        # workers. Stuck threads in the old pool are left to drain / die at exit;
        # queued-but-unstarted sends are cancelled.
        old, self._executor = self._executor, self._new_executor()
        old.shutdown(wait=False, cancel_futures=True)
        logger.error(
            "[email] send circuit OPEN — relay unhealthy; pausing sends for {}s "
            "and abandoning the poisoned send pool",
            self._cooldown,
        )


_send_circuit: Optional[_SendCircuit] = None


def _get_send_circuit() -> _SendCircuit:
    """Process-wide send circuit breaker, built lazily from config."""
    global _send_circuit
    if _send_circuit is None:
        cfg = get_config()
        _send_circuit = _SendCircuit(
            fail_threshold=cfg.email_circuit_fail_threshold,
            cooldown_seconds=cfg.email_circuit_cooldown_seconds,
        )
    return _send_circuit


@dataclass
class EmailMessage:
    """A transport-agnostic outbound message."""

    to: List[str]
    subject: str
    body_text: str
    body_html: Optional[str] = None
    cc: List[str] = field(default_factory=list)

    def recipients(self) -> List[str]:
        """All non-empty addresses (to + cc) for envelope delivery."""
        return [addr for addr in (*self.to, *self.cc) if addr]


# ──────────────────────────────────────────────────────────────────────────
# Backends
# ──────────────────────────────────────────────────────────────────────────


class EmailBackend:
    """Backend contract. ``send`` returns True on success, False on failure."""

    name = "base"

    def send(self, message: EmailMessage, *, from_address: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class LoggingEmailBackend(EmailBackend):
    """Writes the message to the log instead of sending it.

    Safe default for dev / CI — lets the whole notification path run and be
    asserted on without a mail server.
    """

    name = "log"

    def send(self, message: EmailMessage, *, from_address: str) -> bool:
        """Log the message and report success without sending anything."""
        logger.info(
            "[email:log] would send from={} to={} cc={} subject={!r}\n{}",
            from_address,
            message.to,
            message.cc,
            message.subject,
            message.body_text,
        )
        return True


class SmtpEmailBackend(EmailBackend):
    """Standard-library SMTP submission (STARTTLS + optional auth)."""

    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        use_tls: bool = True,
        timeout: int = 10,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, message: EmailMessage, *, from_address: str) -> bool:
        """Submit the message over SMTP (STARTTLS + optional auth)."""
        mime = _MimeMessage()
        mime["From"] = from_address
        mime["To"] = ", ".join(message.to)
        if message.cc:
            mime["Cc"] = ", ".join(message.cc)
        mime["Subject"] = message.subject
        mime.set_content(message.body_text)
        if message.body_html:
            mime.add_alternative(message.body_html, subtype="html")

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
            if self.use_tls:
                server.starttls()
            if self.username:
                server.login(self.username, self.password)
            server.send_message(mime, from_addr=from_address, to_addrs=message.recipients())
        logger.info(
            "[email:smtp] sent to={} subject={!r} via {}:{}",
            message.recipients(),
            message.subject,
            self.host,
            self.port,
        )
        return True


class EmailConnector:
    """Public entry point. Selects a backend and guarantees ``send`` never raises."""

    def __init__(
        self,
        backend: EmailBackend,
        *,
        from_address: str,
        enabled: bool = True,
        send_timeout: int = 10,
    ):
        self.backend = backend
        self.from_address = from_address
        self.enabled = enabled
        self.send_timeout = send_timeout

    def send_status(self, message: EmailMessage) -> str:
        """Send ``message`` and report the outcome as one of :data:`SEND_OK` /
        :data:`SEND_REJECTED` / :data:`SEND_TIMEOUT`. Never raises.

        The send runs on a small shared pool, bounded by ``send_timeout`` seconds
        so a slow/unreachable relay can never block the caller beyond that. On
        timeout the worker can't be cancelled mid-send (it drains in the
        background), so :data:`SEND_TIMEOUT` is surfaced distinctly — a caller
        walking several addresses should stop on it rather than poison another
        worker per address.

        A circuit breaker (:class:`_SendCircuit`) sits in front of the pool: once
        the relay starts hanging it short-circuits sends as :data:`SEND_TIMEOUT`
        without using a worker, and self-heals via a probe when the relay
        recovers — so a dead relay never exhausts the pool and email comes back
        without a restart."""
        if not self.enabled:
            logger.debug("[email] disabled; skipping send subject={!r}", message.subject)
            return SEND_REJECTED
        if not message.recipients():
            logger.warning("[email] no recipients resolved; skipping subject={!r}", message.subject)
            return SEND_REJECTED

        circuit = _get_send_circuit()
        executor, is_probe = circuit.acquire()
        if executor is None:
            # Circuit open and cooling down: fail fast as a timeout (so a
            # multi-address caller stops) without using a worker or waiting.
            logger.warning(
                "[email] send circuit open; skipping send (backend={}) subject={!r} to={}",
                self.backend.name,
                message.subject,
                message.to,
            )
            return SEND_TIMEOUT

        try:
            future = executor.submit(self.backend.send, message, from_address=self.from_address)
        except RuntimeError:
            # Raced with a concurrent trip that shut this pool down; the circuit
            # is already (re)opening. Treat as a timeout.
            circuit.record(timed_out=True, was_probe=is_probe)
            return SEND_TIMEOUT

        try:
            ok = bool(future.result(timeout=self.send_timeout))
            circuit.record(timed_out=False, was_probe=is_probe)
            return SEND_OK if ok else SEND_REJECTED
        except concurrent.futures.TimeoutError:
            future.cancel()
            circuit.record(timed_out=True, was_probe=is_probe)
            logger.error(
                "[email] send timed out after {}s (backend={}) subject={!r} to={}",
                self.send_timeout,
                self.backend.name,
                message.subject,
                message.to,
            )
            return SEND_TIMEOUT
        except Exception:
            # Backend raised promptly — not a hang, so it doesn't poison a worker
            # and shouldn't count toward tripping the circuit.
            circuit.record(timed_out=False, was_probe=is_probe)
            logger.opt(exception=True).error(
                "[email] backend={} failed to send subject={!r} to={}",
                self.backend.name,
                message.subject,
                message.to,
            )
            return SEND_REJECTED

    def send(self, message: EmailMessage) -> bool:
        """Boolean wrapper over :meth:`send_status` — True only when the
        transport accepted the message. Kept for callers that don't need to tell
        a timeout from a rejection."""
        return self.send_status(message) == SEND_OK

    def send_email(
        self,
        *,
        to: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        cc: Optional[List[str]] = None,
    ) -> bool:
        """Convenience wrapper that builds an :class:`EmailMessage` and sends it."""
        return self.send_status(self._build_message(to, subject, body_text, body_html, cc)) == SEND_OK

    def send_email_status(
        self,
        *,
        to: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        cc: Optional[List[str]] = None,
    ) -> str:
        """Status-returning sibling of :meth:`send_email` (see :meth:`send_status`)."""
        return self.send_status(self._build_message(to, subject, body_text, body_html, cc))

    @staticmethod
    def _build_message(
        to: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str],
        cc: Optional[List[str]],
    ) -> EmailMessage:
        return EmailMessage(
            to=list(to),
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=list(cc or []),
        )


def _build_backend(config) -> EmailBackend:
    """Only an explicit SMTP selection can deliver mail."""
    if config.email_backend == "smtp" and config.email_smtp_host:
        return SmtpEmailBackend(host=config.email_smtp_host, port=config.email_smtp_port,
            username=config.email_smtp_username, password=config.email_smtp_password,
            use_tls=config.email_smtp_use_tls, timeout=config.email_smtp_timeout_seconds)
    return LoggingEmailBackend()


_connector: Optional[EmailConnector] = None


def get_email_connector() -> EmailConnector:
    """Return the process-wide :class:`EmailConnector` singleton."""
    global _connector
    if _connector is None:
        config = get_config()
        _connector = EmailConnector(
            _build_backend(config),
            from_address=config.email_from_address,
            enabled=config.email_enabled,
            send_timeout=config.email_send_timeout_seconds,
        )
    return _connector


def reset_email_connector() -> None:
    """Drop the cached connector and send circuit so the next call rebuilds
    them. For tests — keeps a tripped circuit from leaking across cases."""
    global _connector, _send_circuit
    _connector = None
    _send_circuit = None
