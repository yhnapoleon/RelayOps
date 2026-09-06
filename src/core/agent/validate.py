"""Code-only validation for onboarding drafts — no LLM anywhere in here.

Produces a :class:`ValidationReport`:
  * errors          — block submit (invalid cron / unknown enum / missing names)
  * warnings        — advisory (duplicate-looking rows, empty sections)
  * clarifications  — questions the agent proactively asks the reviewer for
                      key fields the document didn't give (e.g. a hyperlink
                      pasted as text without its URL). Answers are written
                      back with :func:`apply_answer` — a deterministic setter
                      keyed by field_path, so an answer can only ever change
                      the one field it was asked about.
"""

from __future__ import annotations

import re
from typing import List, Optional

from croniter import croniter

from core.models.constants import ApplicationRecoveryScenarioType, JobFailureScenarioType
from core.agent.schemas import (
    Clarification,
    ClarificationAnswer,
    FieldIssue,
    OnboardingDraftPayload,
    ValidationReport,
)

_APP_TYPES = ("fastapi", "runtime", "ray", "generic")


def _check_cron(report: ValidationReport, path: str, value: str) -> None:
    if value and not croniter.is_valid(value):
        report.errors.append(FieldIssue(field_path=path, message=f"Invalid cron expression: {value!r}"))


def _ask(report: ValidationReport, path: str, question: str, reason: str) -> None:
    report.clarifications.append(
        Clarification(id=f"q::{path}", field_path=path, question=question, reason=reason)
    )


def validate_payload(payload: OnboardingDraftPayload) -> ValidationReport:
    report = ValidationReport()
    p = payload.project

    if not p.name.strip():
        report.errors.append(FieldIssue(field_path="project.name", message="Project name is required"))
    if not p.cml_project_name.strip():
        _ask(report, "project.cml_project_name",
             "The CML project name this project binds to is not in the document; "
             "please provide it (used for run monitoring; may be left blank for now)",
             "missing-cml-binding")

    if not payload.products:
        report.warnings.append(FieldIssue(field_path="products", message="No Product/Job/App was extracted"))

    for pi, product in enumerate(payload.products):
        ppath = f"products[{pi}]"
        if not product.name.strip():
            report.errors.append(FieldIssue(field_path=f"{ppath}.name", message="Product name is required"))

        for ji, job in enumerate(product.jobs):
            jpath = f"{ppath}.jobs[{ji}]"
            label = job.cml_job_name or job.control_m_job_name or f"Job #{ji + 1}"
            if not (job.cml_job_name.strip() or job.control_m_job_name.strip() or job.mmp_model_id.strip()):
                # mmp_project_id alone is not enough — a project binding with
                # no model is the auto-created stub awaiting completion; the
                # enrichment step asks for the MMP model and/or a CML job.
                hint = (" — this job has only an MMP project binding; please add an "
                        "MMP model or pick a CML job"
                        if job.mmp_project_id.strip() else "")
                report.errors.append(FieldIssue(
                    field_path=jpath,
                    message=f"A job needs at least one of: CML job name, Control-M name, or MMP model{hint}",
                ))
            _check_cron(report, f"{jpath}.schedule_cron", job.schedule_cron)
            _check_cron(report, f"{jpath}.control_m_cron", job.control_m_cron)
            if job.cml_job_name.strip() and not job.control_m_job_name.strip():
                _ask(report, f"{jpath}.control_m_job_name",
                     f"The Control-M name for job 「{label}」 is not in the document; "
                     "please provide it (may be left blank if there is no Control-M schedule)",
                     "missing-controlm-name")
            if not job.owner_contact.strip():
                _ask(report, f"{jpath}.owner_contact",
                     f"The owner contact for job 「{label}」 (project POC/lead, Ops's "
                     "default escalation target) is not in the document; please provide it",
                     "missing-owner-contact")
            if not job.schedule_cron.strip() and not job.control_m_cron.strip() and job.cml_job_name.strip():
                _ask(report, f"{jpath}.schedule_cron",
                     f"The expected schedule cron for job 「{label}」 is not in the "
                     "document (monitoring uses it to detect missed runs); please "
                     "provide it from the actual Control-M schedule",
                     "missing-cron")
            for si, sc in enumerate(job.scenarios):
                if sc.scenario_type and sc.scenario_type not in JobFailureScenarioType.ALL:
                    report.errors.append(FieldIssue(
                        field_path=f"{jpath}.scenarios[{si}].scenario_type",
                        message=f"Unknown job scenario type: {sc.scenario_type!r}",
                    ))

        for ai, app in enumerate(product.apps):
            apath = f"{ppath}.apps[{ai}]"
            label = app.cml_application_name or f"App #{ai + 1}"
            if not app.cml_application_name.strip():
                report.errors.append(FieldIssue(
                    field_path=f"{apath}.cml_application_name", message="The App's CML application name is required",
                ))
            if app.cml_app_type and app.cml_app_type not in _APP_TYPES:
                report.errors.append(FieldIssue(
                    field_path=f"{apath}.cml_app_type",
                    message=f"Unknown app type: {app.cml_app_type!r} (allowed: {', '.join(_APP_TYPES)})",
                ))
            if not (app.application_url.strip() or app.cml_subdomain.strip()):
                _ask(report, f"{apath}.application_url",
                     f"The access URL for app 「{label}」 is not in the document "
                     "(perhaps only the hyperlink text was copied and the URL was "
                     "lost); please paste the full URL or provide the CML subdomain",
                     "missing-url")
            if not app.owner_contact.strip():
                _ask(report, f"{apath}.owner_contact",
                     f"The owner contact for app 「{label}」 is not in the document; please provide it",
                     "missing-owner-contact")
            for si, sc in enumerate(app.scenarios):
                if sc.scenario_type and sc.scenario_type not in ApplicationRecoveryScenarioType.ALL:
                    report.errors.append(FieldIssue(
                        field_path=f"{apath}.scenarios[{si}].scenario_type",
                        message=f"Unknown app scenario type: {sc.scenario_type!r}",
                    ))

    # Duplicate-looking jobs (same cml_job_name twice) — advisory only.
    seen: dict = {}
    for pi, product in enumerate(payload.products):
        for ji, job in enumerate(product.jobs):
            key = job.cml_job_name.strip().lower()
            if key and key in seen:
                report.warnings.append(FieldIssue(
                    field_path=f"products[{pi}].jobs[{ji}].cml_job_name",
                    message=f"CML job name duplicates {seen[key]}: {job.cml_job_name!r}",
                ))
            elif key:
                seen[key] = f"products[{pi}].jobs[{ji}]"

    # Surface extraction-time "document didn't give X" notes as warnings.
    for note in payload.warnings:
        report.warnings.append(FieldIssue(field_path="", message=note))

    return report


# ── Deterministic answer write-back ──────────────────────────────────

_PATH_TOKEN = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)(?:\[(\d+)\])?")


def apply_answer(payload: OnboardingDraftPayload, answer: ClarificationAnswer) -> OnboardingDraftPayload:
    """Write one answered clarification into the payload, addressed purely by
    ``field_path`` (e.g. ``products[0].apps[1].application_url``). Raises
    ``ValueError`` on a malformed/unknown path so a bad answer can never be
    silently dropped or land on the wrong field."""
    tokens = []
    pos = 0
    path = answer.field_path.strip()
    while pos < len(path):
        m = _PATH_TOKEN.match(path, pos)
        if not m:
            raise ValueError(f"Cannot parse field_path: {path!r}")
        tokens.append((m.group(1), int(m.group(2)) if m.group(2) is not None else None))
        pos = m.end()
        if pos < len(path):
            if path[pos] != ".":
                raise ValueError(f"Cannot parse field_path: {path!r}")
            pos += 1

    target = payload
    for attr, idx in tokens[:-1]:
        target = getattr(target, attr)
        if idx is not None:
            target = target[idx]
    attr, idx = tokens[-1]
    if idx is not None:
        getattr(target, attr)[idx] = answer.answer
    else:
        current = getattr(target, attr)
        if not isinstance(current, str):
            raise ValueError(f"field_path is not a leaf field: {path!r}")
        setattr(target, attr, answer.answer.strip())
    return payload


def apply_answers(
    payload: OnboardingDraftPayload, answers: Optional[List[ClarificationAnswer]]
) -> OnboardingDraftPayload:
    for ans in answers or []:
        # Synthetic action cards (e.g. the cross-project split prompt at
        # "__split__") are not real fields — they are handled by their own
        # endpoint, never written back into the payload.
        if ans.field_path.strip().startswith("__"):
            continue
        if ans.answer.strip():
            apply_answer(payload, ans)
    return payload
