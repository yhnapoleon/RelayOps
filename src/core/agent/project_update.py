"""Add assets from a document to an *existing* Ops project.

The second onboarding mode: instead of creating a project, the reviewer picks
a project that already exists and the document is folded into it —

    snapshot(live project → draft shape)
        → merge(extraction ∪ snapshot)      # proposed state, once at extract
        → annotate(payload vs snapshot)     # what changed, on every save
        → update(apply only the diffs)      # at "Update the project"

Three rules make the whole thing safe to re-run:

* **Nothing is ever deleted.** Assets the document doesn't mention stay
  untouched (and are shown as ``unchanged`` so the reviewer sees the whole
  project); removing a row in the preview only drops it from the review.
* **Identity fields are fill-blank-only.** ``cml_job_name`` /
  ``control_m_job_name`` / ``cml_application_name`` / ``cml_subdomain`` /
  ``scenario_type`` are what matching keys off *and* what CML binding keys off.
  The merge only fills them when the platform row has none; a document that
  disagrees with a set value raises a warning instead of silently rebinding.
* **The diff is derived, never stored in the payload.** It is recomputed from
  the live project every time — including inside :func:`update_payload` — so a
  reviewer edit, an LLM re-order or a concurrent change on the platform can
  never make an update land on the wrong row.

A consequence worth knowing (and surfaced by the live NEW badge): renaming an
asset's CML/Control-M name in the preview makes it a *new* asset rather than a
rename, because that name is the identity the match runs on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from api.schemas.app_schemas import (
    AppCreate,
    AppUpdate,
    ApplicationRecoveryScenarioCreate,
    ApplicationRecoveryScenarioUpdate,
)
from api.schemas.job_schemas import (
    JobCreate,
    JobFailureScenarioCreate,
    JobFailureScenarioUpdate,
    JobUpdate,
)
from core.auth.jwt import CurrentUser
from core.exceptions import NotFoundError, ValidationError
from core.logging import get_logger
from core.models.database import Database
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    Job,
    JobFailureScenario,
    Product,
    Project,
)
from core.services import app_service, job_service, product_service, project_service
from core.agent.schemas import (
    CHANGE_NEW,
    CHANGE_UNCHANGED,
    CHANGE_UPDATE,
    AppDraft,
    AppScenarioDraft,
    EmailTemplateDraft,
    JobDraft,
    JobScenarioDraft,
    NodeDiff,
    OnboardingDraftPayload,
    ProductDraft,
    ProjectDiff,
)

logger = get_logger(__name__)


# ── field policy ──────────────────────────────────────────────────────
#
# "identity" = matched on and CML-bound on → fill-blank only.
# "data"     = free to be updated from the document.

_JOB_IDENTITY = ("cml_job_name", "control_m_job_name")
_JOB_DATA = (
    "cml_project_name", "control_m_cron", "schedule_cron", "mmp_project_id",
    "mmp_model_id", "owner_contact", "description", "dependency_notes",
)
_APP_IDENTITY = ("cml_application_name", "cml_subdomain")
_APP_DATA = (
    "cml_project_name", "cml_app_type", "application_url", "health_check_url",
    "owner_contact", "description",
)
_SCENARIO_IDENTITY = ("scenario_type", "scenario_name")
_SCENARIO_DATA = ("condition_description", "escalation_target")
_JOB_STEPS = ("diagnostic_steps", "action_steps", "verification_steps")
_APP_STEPS = ("action_steps", "verification_steps")
# The Control-M rerun-request conduit (see schemas.JobDraft): not platform
# columns and not diffed — they only feed the scenario email-body composition,
# which the enrichment step runs (and then clears them). Carried from the
# document onto the merged asset so that composition has the doc's values; a
# freshly loaded snapshot leaves them blank (the values already live in the
# existing email bodies).
_JOB_EMAIL_FIELDS = ("control_m_application", "control_m_group", "control_m_table", "change_number")
_APP_EMAIL_FIELDS = ("control_m_application", "change_number")
# Column groups the service mirrors into one another on write (see
# job_service.update / app_service.update) — patched as a unit, never alone.
_JOB_UNIFIED = (("schedule_cron", "control_m_cron"),)
_APP_UNIFIED = (("application_url", "health_check_url"),)
_PROJECT_FIELDS = ("description", "cml_project_name", "mmp_project_id", "prod_stat_url")


def _norm(value: str) -> str:
    """Fold case, whitespace and _/- so ``NORTHSTAR_Forecast`` == ``northstar-forecast``."""
    return re.sub(r"[\s_\-]+", "", (value or "").strip().lower())


def _url_key(value: str) -> str:
    """Compare URLs on host+path only (scheme / trailing slash / port drift)."""
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v)
    return v.rstrip("/")


def _steps(value) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(s) for s in value if str(s).strip()]


# ── snapshot: the live project in draft shape ─────────────────────────


@dataclass
class ProjectSnapshot:
    """The project as it exists on the platform right now, expressed in draft
    shape so matching and diffing compare like with like."""

    project_id: int
    name: str
    payload: OnboardingDraftPayload
    # field_path → platform row id, for every node of ``payload``.
    ids: Dict[str, int] = field(default_factory=dict)


def _job_to_draft(job: Job, scenarios: Sequence[JobFailureScenario]) -> JobDraft:
    return JobDraft(
        cml_project_name=job.cml_project_name or "",
        cml_job_name=job.cml_job_name or "",
        control_m_job_name=job.control_m_job_name or "",
        control_m_cron=job.control_m_cron or "",
        schedule_cron=job.schedule_cron or "",
        mmp_project_id=job.mmp_project_id or "",
        mmp_model_id=job.mmp_model_id or "",
        owner_contact=job.owner_contact or "",
        description=job.description or "",
        dependency_notes=job.dependency_notes or "",
        scenarios=[
            JobScenarioDraft(
                scenario_type=sc.scenario_type or "",
                scenario_name=sc.scenario_name or "",
                condition_description=sc.condition_description or "",
                diagnostic_steps=_steps(sc.diagnostic_steps),
                action_steps=_steps(sc.action_steps),
                verification_steps=_steps(sc.verification_steps),
                escalation_target=sc.escalation_target or "",
                email_template=EmailTemplateDraft(**{
                    k: str((sc.email_template or {}).get(k, "") or "")
                    for k in ("to", "cc", "subject", "body")
                }),
            )
            for sc in scenarios
        ],
    )


def _app_to_draft(app: Application, scenarios: Sequence[ApplicationRecoveryScenario]) -> AppDraft:
    return AppDraft(
        cml_project_name=app.cml_project_name or "",
        cml_application_name=app.cml_application_name or "",
        cml_subdomain=app.cml_subdomain or "",
        cml_app_type=app.cml_app_type or "generic",
        application_url=app.application_url or "",
        health_check_url=app.health_check_url or "",
        owner_contact=app.owner_contact or "",
        description=app.description or "",
        scenarios=[
            AppScenarioDraft(
                scenario_type=sc.scenario_type or "",
                scenario_name=sc.scenario_name or "",
                condition_description=sc.condition_description or "",
                action_steps=_steps(sc.action_steps),
                verification_steps=_steps(sc.verification_steps),
                escalation_target=sc.escalation_target or "",
                email_template=EmailTemplateDraft(**{
                    k: str((sc.email_template or {}).get(k, "") or "")
                    for k in ("to", "cc", "subject", "body")
                }),
            )
            for sc in scenarios
        ],
    )


def load_snapshot(session, project_id: int) -> ProjectSnapshot:
    """Read the project + every product/job/app/scenario under it into draft
    shape. Read-only; the caller owns the session and the access check."""
    project = session.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise NotFoundError("Project not found")

    snapshot = ProjectSnapshot(
        project_id=project.id,
        name=project.name or "",
        payload=OnboardingDraftPayload(),
    )
    snapshot.payload.project.name = project.name or ""
    snapshot.payload.project.description = project.description or ""
    snapshot.payload.project.cml_project_name = project.cml_project_name or ""
    snapshot.payload.project.mmp_project_id = project.mmp_project_id or ""
    snapshot.payload.project.prod_stat_url = project.prod_stat_url or ""
    snapshot.ids["project"] = project.id

    products = (
        session.query(Product)
        .filter(Product.project_id == project_id)
        .order_by(Product.id.asc())
        .all()
    )
    for pi, product in enumerate(products):
        ppath = f"products[{pi}]"
        draft = ProductDraft(name=product.name or "")
        snapshot.ids[ppath] = product.id

        jobs = (
            session.query(Job)
            .filter(Job.product_id == product.id)
            .order_by(Job.id.asc())
            .all()
        )
        for ji, job in enumerate(jobs):
            scenarios = (
                session.query(JobFailureScenario)
                .filter(JobFailureScenario.job_id == job.id)
                .order_by(JobFailureScenario.id.asc())
                .all()
            )
            draft.jobs.append(_job_to_draft(job, scenarios))
            snapshot.ids[f"{ppath}.jobs[{ji}]"] = job.id
            for si, sc in enumerate(scenarios):
                snapshot.ids[f"{ppath}.jobs[{ji}].scenarios[{si}]"] = sc.id

        apps = (
            session.query(Application)
            .filter(Application.product_id == product.id)
            .order_by(Application.id.asc())
            .all()
        )
        for ai, app in enumerate(apps):
            scenarios = (
                session.query(ApplicationRecoveryScenario)
                .filter(ApplicationRecoveryScenario.application_id == app.id)
                .order_by(ApplicationRecoveryScenario.id.asc())
                .all()
            )
            draft.apps.append(_app_to_draft(app, scenarios))
            snapshot.ids[f"{ppath}.apps[{ai}]"] = app.id
            for si, sc in enumerate(scenarios):
                snapshot.ids[f"{ppath}.apps[{ai}].scenarios[{si}]"] = sc.id

        snapshot.payload.products.append(draft)
    return snapshot


def load_snapshot_for_project(db: Database, project_id: int) -> ProjectSnapshot:
    session = db.get_session()
    try:
        return load_snapshot(session, project_id)
    finally:
        session.close()


# ── matching ──────────────────────────────────────────────────────────


def _job_keys(job: JobDraft) -> List[str]:
    keys = []
    for name in (job.cml_job_name, job.control_m_job_name):
        if _norm(name):
            keys.append(f"n:{_norm(name)}")
    if job.mmp_model_id.strip():
        keys.append(f"m:{_norm(job.mmp_project_id)}/{_norm(job.mmp_model_id)}")
    return keys


def _app_keys(app: AppDraft) -> List[str]:
    keys = []
    if _norm(app.cml_application_name):
        keys.append(f"n:{_norm(app.cml_application_name)}")
    if _norm(app.cml_subdomain):
        keys.append(f"s:{_norm(app.cml_subdomain)}")
    if _url_key(app.application_url):
        keys.append(f"u:{_url_key(app.application_url)}")
    return keys


def _index(payload: OnboardingDraftPayload, kind: str) -> Dict[str, str]:
    """key → field_path, for every job (or app) in ``payload``. The first
    holder of a key wins, so an ambiguous key can never re-point a match."""
    out: Dict[str, str] = {}
    for pi, product in enumerate(payload.products):
        assets = product.jobs if kind == "jobs" else product.apps
        for i, asset in enumerate(assets):
            path = f"products[{pi}].{kind}[{i}]"
            keys = _job_keys(asset) if kind == "jobs" else _app_keys(asset)
            for key in keys:
                out.setdefault(key, path)
    return out


def _find(index: Dict[str, str], keys: Sequence[str], taken: set) -> Optional[str]:
    for key in keys:
        path = index.get(key)
        if path is not None and path not in taken:
            return path
    return None


def _product_of(path: str) -> int:
    m = re.match(r"products\[(\d+)\]", path)
    return int(m.group(1)) if m else -1


def _match_scenarios(draft_scenarios, snap_scenarios) -> Dict[int, int]:
    """draft index → snapshot index. Name first (when both sides have one),
    then first-unused scenario of the same type."""
    out: Dict[int, int] = {}
    taken: set = set()

    def _claim(di: int, si: int) -> None:
        out[di] = si
        taken.add(si)

    for di, dsc in enumerate(draft_scenarios):
        name = _norm(dsc.scenario_name)
        if name:
            for si, ssc in enumerate(snap_scenarios):
                if si not in taken and _norm(ssc.scenario_name) == name:
                    _claim(di, si)
                    break
        if di in out:
            continue
        stype = (dsc.scenario_type or "").strip().lower()
        if not stype:
            continue
        for si, ssc in enumerate(snap_scenarios):
            if si not in taken and (ssc.scenario_type or "").strip().lower() == stype:
                _claim(di, si)
                break
    return out


@dataclass
class _Matches:
    """draft field_path → snapshot field_path, for every level."""

    products: Dict[str, str] = field(default_factory=dict)
    jobs: Dict[str, str] = field(default_factory=dict)
    apps: Dict[str, str] = field(default_factory=dict)
    scenarios: Dict[str, str] = field(default_factory=dict)


def _match_products(
    payload: OnboardingDraftPayload,
    snapshot: ProjectSnapshot,
    matches: _Matches,
) -> None:
    """Place each draft product on a snapshot product: majority vote of where
    its already-matched assets live, else name equality, else close name."""
    snap_names = [_norm(p.name) for p in snapshot.payload.products]
    taken = set(matches.products.values())
    for pi, product in enumerate(payload.products):
        ppath = f"products[{pi}]"
        if ppath in matches.products:
            continue
        votes: Dict[int, int] = {}
        for kind, table in (("jobs", matches.jobs), ("apps", matches.apps)):
            assets = product.jobs if kind == "jobs" else product.apps
            for i in range(len(assets)):
                target = table.get(f"{ppath}.{kind}[{i}]")
                if target:
                    votes[_product_of(target)] = votes.get(_product_of(target), 0) + 1
        if votes:
            matches.products[ppath] = f"products[{max(votes, key=votes.get)}]"
            continue
        name = _norm(product.name)
        if not name:
            continue
        best_i, best_score = -1, 0.0
        for si, sname in enumerate(snap_names):
            if f"products[{si}]" in taken or not sname:
                continue
            score = 1.0 if sname == name else SequenceMatcher(None, name, sname).ratio()
            if score > best_score:
                best_i, best_score = si, score
        if best_i >= 0 and best_score >= 0.86:
            matches.products[ppath] = f"products[{best_i}]"
            taken.add(f"products[{best_i}]")


def match_payload(payload: OnboardingDraftPayload, snapshot: ProjectSnapshot) -> _Matches:
    """Resolve every draft node to its snapshot counterpart (or nothing).

    Assets are matched project-wide, not per product — a document routinely
    files a job under a different product name than the platform does, and
    re-creating it there would duplicate live monitoring.
    """
    matches = _Matches()
    job_index = _index(snapshot.payload, "jobs")
    app_index = _index(snapshot.payload, "apps")
    taken_jobs: set = set()
    taken_apps: set = set()

    for pi, product in enumerate(payload.products):
        ppath = f"products[{pi}]"
        for ji, job in enumerate(product.jobs):
            hit = _find(job_index, _job_keys(job), taken_jobs)
            if hit:
                taken_jobs.add(hit)
                matches.jobs[f"{ppath}.jobs[{ji}]"] = hit
        for ai, app in enumerate(product.apps):
            hit = _find(app_index, _app_keys(app), taken_apps)
            if hit:
                taken_apps.add(hit)
                matches.apps[f"{ppath}.apps[{ai}]"] = hit

    _match_products(payload, snapshot, matches)

    # Scenarios only make sense inside a matched asset.
    for kind, table in (("jobs", matches.jobs), ("apps", matches.apps)):
        for draft_path, snap_path in table.items():
            draft_asset = _resolve(payload, draft_path)
            snap_asset = _resolve(snapshot.payload, snap_path)
            for di, si in _match_scenarios(draft_asset.scenarios, snap_asset.scenarios).items():
                matches.scenarios[f"{draft_path}.scenarios[{di}]"] = f"{snap_path}.scenarios[{si}]"
    return matches


_PATH_RE = re.compile(r"([a-zA-Z_]+)\[(\d+)\]")


def _resolve(payload: OnboardingDraftPayload, path: str):
    """``products[0].jobs[1].scenarios[2]`` → the model at that path."""
    node = payload
    for attr, idx in _PATH_RE.findall(path):
        node = getattr(node, attr)[int(idx)]
    return node


# ── merge: extraction ∪ snapshot → the proposed state ─────────────────


def _fill_only(target, source, fields: Sequence[str]) -> List[Tuple[str, str, str]]:
    """Copy ``fields`` from source to target only where target is blank.
    Returns the conflicts (field, kept, offered) for the warning trail."""
    conflicts: List[Tuple[str, str, str]] = []
    for name in fields:
        offered = (getattr(source, name, "") or "").strip()
        if not offered:
            continue
        current = (getattr(target, name, "") or "").strip()
        if not current:
            setattr(target, name, offered)
        elif _norm(current) != _norm(offered):
            conflicts.append((name, current, offered))
    return conflicts


def _overwrite(target, source, fields: Sequence[str]) -> None:
    """Copy every non-empty ``fields`` value from source onto target."""
    for name in fields:
        offered = (getattr(source, name, "") or "").strip()
        if offered:
            setattr(target, name, offered)


def _merge_scenarios(target_asset, source_asset, kind: str, label: str,
                     warnings: List[str]) -> None:
    step_fields = _JOB_STEPS if kind == "job" else _APP_STEPS
    matched = _match_scenarios(source_asset.scenarios, target_asset.scenarios)
    for di, src in enumerate(source_asset.scenarios):
        ti = matched.get(di)
        if ti is None:
            target_asset.scenarios.append(src.model_copy(deep=True))
            continue
        dst = target_asset.scenarios[ti]
        for name, kept, offered in _fill_only(dst, src, _SCENARIO_IDENTITY):
            if name == "scenario_type":
                warnings.append(
                    f"{label} scenario 「{dst.scenario_name or kept}」: the document reads as "
                    f"'{offered}' but the platform has '{kept}' — kept the platform value "
                    "(the type drives issue routing; change it by hand if the document is right)"
                )
        _overwrite(dst, src, _SCENARIO_DATA)
        for name in step_fields:
            offered = [s for s in getattr(src, name, []) if s.strip()]
            if offered and offered != getattr(dst, name):
                setattr(dst, name, offered)
        if dst.email_template.is_empty and not src.email_template.is_empty:
            dst.email_template = src.email_template.model_copy(deep=True)
        if src.source_quote.strip():
            dst.source_quote = src.source_quote


def merge_into_snapshot(
    extracted: OnboardingDraftPayload, snapshot: ProjectSnapshot
) -> OnboardingDraftPayload:
    """Fold the extracted document into the live project.

    Returns the *proposed* state: every existing asset (so the reviewer sees
    the whole project) with the document's data merged in, plus the assets the
    document adds. Conflicts on identity fields become payload warnings rather
    than silent rewrites.
    """
    merged = snapshot.payload.model_copy(deep=True)
    merged.warnings = list(extracted.warnings)
    matches = match_payload(extracted, snapshot)

    # Project row: fill blanks only — a document must not silently re-point a
    # live project's CML/MMP binding (that cascades onto every child asset).
    for name, kept, offered in _fill_only(merged.project, extracted.project, _PROJECT_FIELDS):
        merged.warnings.append(
            f"Project {name}: the document says 「{offered}」 but this project is set to "
            f"「{kept}」 — kept the existing value"
        )
    merged.project.owner_name = extracted.project.owner_name or merged.project.owner_name

    project_binding = _norm(merged.project.cml_project_name)

    for pi, product in enumerate(extracted.products):
        ppath = f"products[{pi}]"
        target_path = matches.products.get(ppath)
        if target_path is None:
            target_product = ProductDraft(name=product.name)
            merged.products.append(target_product)
            target_path = f"products[{len(merged.products) - 1}]"
        else:
            target_product = _resolve(merged, target_path)
            if _norm(target_product.name) != _norm(product.name) and product.name.strip():
                merged.warnings.append(
                    f"The document calls product 「{target_product.name}」 "
                    f"「{product.name}」 — kept the existing name"
                )

        for ji, job in enumerate(product.jobs):
            snap_path = matches.jobs.get(f"{ppath}.jobs[{ji}]")
            if snap_path is None:
                new_job = job.model_copy(deep=True)
                # An asset-level cml_project_name equal to the project binding
                # is inheritance, not an override — keep the sentinel empty.
                if _norm(new_job.cml_project_name) == project_binding:
                    new_job.cml_project_name = ""
                target_product.jobs.append(new_job)
                continue
            dst = _resolve(merged, snap_path)
            for name, kept, offered in _fill_only(dst, job, _JOB_IDENTITY):
                merged.warnings.append(
                    f"Job 「{kept}」: the document reads as 「{offered}」 — kept the platform "
                    "name (it is what the CML binding resolves on)"
                )
            data_fields = tuple(
                f for f in _JOB_DATA
                if not (f == "cml_project_name"
                        and not dst.cml_project_name.strip()
                        and _norm(job.cml_project_name) == project_binding)
            )
            _overwrite(dst, job, data_fields)
            # Carry the doc's Control-M rerun-request identifiers so the email
            # composition (run later, on the merged payload) can fold them into
            # any new scenario bodies. Not diffed/written — they're a conduit.
            _overwrite(dst, job, _JOB_EMAIL_FIELDS)
            if job.source_quote.strip():
                dst.source_quote = job.source_quote
            _merge_scenarios(dst, job, "job",
                             f"Job 「{dst.cml_job_name or dst.control_m_job_name}」",
                             merged.warnings)

        for ai, app in enumerate(product.apps):
            snap_path = matches.apps.get(f"{ppath}.apps[{ai}]")
            if snap_path is None:
                new_app = app.model_copy(deep=True)
                if _norm(new_app.cml_project_name) == project_binding:
                    new_app.cml_project_name = ""
                target_product.apps.append(new_app)
                continue
            dst = _resolve(merged, snap_path)
            for name, kept, offered in _fill_only(dst, app, _APP_IDENTITY):
                merged.warnings.append(
                    f"App 「{kept}」: the document reads as 「{offered}」 — kept the platform "
                    "name (it is what the CML binding resolves on)"
                )
            data_fields = tuple(
                f for f in _APP_DATA
                if not (f == "cml_project_name"
                        and not dst.cml_project_name.strip()
                        and _norm(app.cml_project_name) == project_binding)
            )
            _overwrite(dst, app, data_fields)
            _overwrite(dst, app, _APP_EMAIL_FIELDS)
            if app.source_quote.strip():
                dst.source_quote = app.source_quote
            _merge_scenarios(dst, app, "app",
                             f"App 「{dst.cml_application_name}」", merged.warnings)

    return merged


# ── annotate: payload vs snapshot → the diff the UI renders ───────────


def _diff_fields(draft, snap, fields: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name in fields:
        new = (getattr(draft, name, "") or "").strip()
        old = (getattr(snap, name, "") or "").strip()
        if new != old:
            out[name] = old
    return out


def _diff_steps(draft, snap, fields: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name in fields:
        new = [s for s in getattr(draft, name, []) if s.strip()]
        old = [s for s in getattr(snap, name, []) if s.strip()]
        if new != old:
            out[name] = "\n".join(old)
    return out


def _scenario_previous(draft, snap, kind: str) -> Dict[str, str]:
    fields = _SCENARIO_IDENTITY + _SCENARIO_DATA
    previous = _diff_fields(draft, snap, fields)
    previous.update(_diff_steps(draft, snap, _JOB_STEPS if kind == "job" else _APP_STEPS))
    if draft.email_template.model_dump() != snap.email_template.model_dump():
        previous["email_template"] = (
            "(no template)" if snap.email_template.is_empty else snap.email_template.subject
        )
    return previous


def annotate(payload: OnboardingDraftPayload, snapshot: ProjectSnapshot) -> ProjectDiff:
    """Compare the draft against the live project. Pure — ``payload`` is not
    touched; the result is what the preview badges and :func:`update_payload`
    both read."""
    diff = ProjectDiff(project_id=snapshot.project_id, project_name=snapshot.name)
    matches = match_payload(payload, snapshot)

    project_prev = _diff_fields(payload.project, snapshot.payload.project, _PROJECT_FIELDS)
    diff.nodes["project"] = NodeDiff(
        existing_id=snapshot.project_id,
        change=CHANGE_UPDATE if project_prev else CHANGE_UNCHANGED,
        previous=project_prev,
    )

    for pi, product in enumerate(payload.products):
        ppath = f"products[{pi}]"
        snap_path = matches.products.get(ppath)
        product_changed = False

        for kind, table, identity, data in (
            ("jobs", matches.jobs, _JOB_IDENTITY, _JOB_DATA),
            ("apps", matches.apps, _APP_IDENTITY, _APP_DATA),
        ):
            assets = product.jobs if kind == "jobs" else product.apps
            for i, asset in enumerate(assets):
                apath = f"{ppath}.{kind}[{i}]"
                asset_snap_path = table.get(apath)
                if asset_snap_path is None:
                    diff.nodes[apath] = NodeDiff(change=CHANGE_NEW)
                    for si in range(len(asset.scenarios)):
                        diff.nodes[f"{apath}.scenarios[{si}]"] = NodeDiff(change=CHANGE_NEW)
                    product_changed = True
                    continue
                snap_asset = _resolve(snapshot.payload, asset_snap_path)
                previous = _diff_fields(asset, snap_asset, identity + data)
                asset_changed = bool(previous)
                for si, sc in enumerate(asset.scenarios):
                    spath = f"{apath}.scenarios[{si}]"
                    sc_snap_path = matches.scenarios.get(spath)
                    if sc_snap_path is None:
                        diff.nodes[spath] = NodeDiff(change=CHANGE_NEW)
                        asset_changed = True
                        continue
                    sc_prev = _scenario_previous(
                        sc, _resolve(snapshot.payload, sc_snap_path),
                        "job" if kind == "jobs" else "app",
                    )
                    diff.nodes[spath] = NodeDiff(
                        existing_id=snapshot.ids.get(sc_snap_path),
                        change=CHANGE_UPDATE if sc_prev else CHANGE_UNCHANGED,
                        previous=sc_prev,
                    )
                    asset_changed = asset_changed or bool(sc_prev)
                diff.nodes[apath] = NodeDiff(
                    existing_id=snapshot.ids.get(asset_snap_path),
                    change=CHANGE_UPDATE if asset_changed else CHANGE_UNCHANGED,
                    previous=previous,
                )
                product_changed = product_changed or asset_changed

        if snap_path is None:
            diff.nodes[ppath] = NodeDiff(change=CHANGE_NEW)
        else:
            diff.nodes[ppath] = NodeDiff(
                existing_id=snapshot.ids.get(snap_path),
                change=CHANGE_UPDATE if product_changed else CHANGE_UNCHANGED,
            )
    return diff


def annotate_for_project(
    db: Database, payload: OnboardingDraftPayload, project_id: int
) -> ProjectDiff:
    return annotate(payload, load_snapshot_for_project(db, project_id))


# ── update: apply the diff ────────────────────────────────────────────


def _job_create_body(job: JobDraft) -> JobCreate:
    return JobCreate(
        mmp_project_id=job.mmp_project_id,
        mmp_model_id=job.mmp_model_id,
        control_m_job_name=job.control_m_job_name,
        control_m_cron=job.control_m_cron,
        cml_project_name=job.cml_project_name,
        cml_job_name=job.cml_job_name,
        schedule_cron=job.schedule_cron,
        description=job.description,
        dependency_notes=job.dependency_notes,
        owner_contact=job.owner_contact,
    )


def _app_create_body(app: AppDraft) -> AppCreate:
    return AppCreate(
        application_url=app.application_url,
        health_check_url=app.health_check_url,
        description=app.description,
        owner_contact=app.owner_contact,
        cml_project_name=app.cml_project_name,
        cml_application_name=app.cml_application_name,
        cml_subdomain=app.cml_subdomain,
        cml_app_type=app.cml_app_type or "generic",
    )


def _email_or_none(template: EmailTemplateDraft) -> Optional[dict]:
    return None if template.is_empty else template.model_dump()


def _job_scenario_create(sc: JobScenarioDraft) -> JobFailureScenarioCreate:
    return JobFailureScenarioCreate(
        scenario_type=sc.scenario_type.strip() or "other",
        scenario_name=sc.scenario_name,
        condition_description=sc.condition_description,
        diagnostic_steps=sc.diagnostic_steps,
        action_steps=sc.action_steps,
        verification_steps=sc.verification_steps,
        escalation_target=sc.escalation_target,
        email_template=_email_or_none(sc.email_template),
    )


def _app_scenario_create(sc: AppScenarioDraft) -> ApplicationRecoveryScenarioCreate:
    return ApplicationRecoveryScenarioCreate(
        scenario_type=sc.scenario_type.strip() or "other",
        scenario_name=sc.scenario_name,
        condition_description=sc.condition_description,
        action_steps=sc.action_steps,
        verification_steps=sc.verification_steps,
        escalation_target=sc.escalation_target,
        email_template=_email_or_none(sc.email_template),
    )


def _changed_patch(
    draft, previous: Dict[str, str], *,
    steps: Sequence[str] = (), unified: Sequence[Sequence[str]] = (),
) -> dict:
    """Only the fields the diff says changed, taken from the draft. Keeping the
    body minimal means an unchanged binding is never re-resolved against CML.

    ``unified`` names column groups the service mirrors into each other (one
    input in the manual UI drives both ``schedule_cron`` and ``control_m_cron``,
    both app URLs, …). Touching one of those makes the service recompute the
    whole group, so send the siblings too — otherwise the write-back lands on a
    value the patch never mentioned and the field reads as still-pending."""
    patch: dict = {}
    for name in previous:
        if name == "email_template":
            patch["email_template"] = _email_or_none(draft.email_template)
        elif name in steps:
            patch[name] = [s for s in getattr(draft, name) if s.strip()]
        else:
            patch[name] = getattr(draft, name)
    for group in unified:
        if any(name in patch for name in group):
            for name in group:
                patch.setdefault(name, getattr(draft, name))
    return patch


def _apply_scenarios(session, *, kind: str, asset_id: int, asset_path: str,
                     scenarios, diff: ProjectDiff, actor: CurrentUser,
                     report: dict) -> None:
    create_scenario = job_service.create_scenario if kind == "job" else app_service.create_scenario
    update_scenario = job_service.update_scenario if kind == "job" else app_service.update_scenario
    steps = _JOB_STEPS if kind == "job" else _APP_STEPS
    for si, sc in enumerate(scenarios):
        node = diff.nodes.get(f"{asset_path}.scenarios[{si}]") or NodeDiff()
        if node.existing_id is None:
            body = _job_scenario_create(sc) if kind == "job" else _app_scenario_create(sc)
            kwargs = {"job_id": asset_id} if kind == "job" else {"app_id": asset_id}
            create_scenario(session, body=body, actor=actor, **kwargs)
            report["created"]["scenarios"] += 1
        elif node.previous:
            patch = _changed_patch(sc, node.previous, steps=steps)
            if "scenario_type" in patch:
                patch["scenario_type"] = sc.scenario_type.strip() or "other"
            body = (
                JobFailureScenarioUpdate(**patch) if kind == "job"
                else ApplicationRecoveryScenarioUpdate(**patch)
            )
            update_scenario(session, scenario_id=node.existing_id, body=body, actor=actor)
            report["updated"]["scenarios"] += 1


def update_payload(
    db: Database, *, payload: OnboardingDraftPayload, project_id: int, actor: CurrentUser
) -> dict:
    """Apply a reviewed update draft to its target project.

    Creates what is new, patches only the fields that actually differ from the
    platform right now (the diff is recomputed here, so reviewer edits made
    after the last save are honoured), and never deletes. Products/jobs/apps
    all go through the normal service layer, so access checks, the handover
    version lock and CML binding resolution behave exactly as they do for a
    manual edit.

    Everything below the project row runs in one session: a failure rolls the
    whole batch back rather than leaving the project half-updated.
    """
    session = db.get_session()
    try:
        snapshot = load_snapshot(session, project_id)
        diff = annotate(payload, snapshot)

        report: dict = {
            "project_id": project_id,
            "project_name": snapshot.name,
            "mode": "update",
            "products": [],
            "bindings": [],
            "created": {"products": 0, "jobs": 0, "apps": 0, "scenarios": 0},
            "updated": {"products": 0, "jobs": 0, "apps": 0, "scenarios": 0},
        }

        for pi, product_draft in enumerate(payload.products):
            ppath = f"products[{pi}]"
            node = diff.nodes.get(ppath) or NodeDiff()
            if node.existing_id is None:
                product = product_service.create(
                    session, project_id=project_id,
                    name=product_draft.name.strip(), actor=actor,
                )
                session.flush()
                report["created"]["products"] += 1
            else:
                product = session.query(Product).filter(Product.id == node.existing_id).first()
                if product is None or product.project_id != project_id:
                    raise ValidationError(
                        f"Product #{node.existing_id} no longer belongs to this project; "
                        "reload the draft and review it again"
                    )
            product_report = {
                "product_id": product.id, "name": product.name, "jobs": [], "apps": [],
            }

            for ji, job_draft in enumerate(product_draft.jobs):
                jpath = f"{ppath}.jobs[{ji}]"
                jnode = diff.nodes.get(jpath) or NodeDiff()
                if jnode.existing_id is None:
                    job = job_service.create(
                        session, product_id=product.id,
                        body=_job_create_body(job_draft), actor=actor,
                    )
                    session.flush()
                    report["created"]["jobs"] += 1
                else:
                    job = session.query(Job).filter(Job.id == jnode.existing_id).first()
                    if job is None:
                        raise ValidationError(
                            "A job in this draft no longer exists; reload the draft")
                    if jnode.previous:
                        job = job_service.update(
                            session, job_id=job.id,
                            body=JobUpdate(**_changed_patch(
                                job_draft, jnode.previous, unified=_JOB_UNIFIED)),
                            actor=actor,
                        )
                        report["updated"]["jobs"] += 1
                _apply_scenarios(
                    session, kind="job", asset_id=job.id, asset_path=jpath,
                    scenarios=job_draft.scenarios, diff=diff, actor=actor, report=report,
                )
                product_report["jobs"].append(
                    {"job_id": job.id, "cml_job_name": job.cml_job_name,
                     "change": jnode.change})
                report["bindings"].append(_job_binding(job))

            for ai, app_draft in enumerate(product_draft.apps):
                apath = f"{ppath}.apps[{ai}]"
                anode = diff.nodes.get(apath) or NodeDiff()
                if anode.existing_id is None:
                    app = app_service.create(
                        session, product_id=product.id,
                        body=_app_create_body(app_draft), actor=actor,
                    )
                    session.flush()
                    report["created"]["apps"] += 1
                else:
                    app = session.query(Application).filter(
                        Application.id == anode.existing_id).first()
                    if app is None:
                        raise ValidationError(
                            "An app in this draft no longer exists; reload the draft")
                    if anode.previous:
                        app = app_service.update(
                            session, app_id=app.id,
                            body=AppUpdate(**_changed_patch(
                                app_draft, anode.previous, unified=_APP_UNIFIED)),
                            actor=actor,
                        )
                        report["updated"]["apps"] += 1
                _apply_scenarios(
                    session, kind="app", asset_id=app.id, asset_path=apath,
                    scenarios=app_draft.scenarios, diff=diff, actor=actor, report=report,
                )
                product_report["apps"].append(
                    {"app_id": app.id, "cml_application_name": app.cml_application_name,
                     "change": anode.change})
                report["bindings"].append(_app_binding(app))

            report["products"].append(product_report)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _apply_project_fields(db, payload, diff, project_id, actor, report)
    return report


def _apply_project_fields(
    db: Database, payload: OnboardingDraftPayload, diff: ProjectDiff,
    project_id: int, actor: CurrentUser, report: dict,
) -> None:
    """Project-row changes go last and through their own service (it manages
    its own transaction). The name is deliberately never touched — renaming a
    live project from a document import is not what "add assets" means."""
    previous = (diff.nodes.get("project") or NodeDiff()).previous
    fields = {k: v for k, v in previous.items() if k in _PROJECT_FIELDS}
    if not fields:
        return
    from core.models.user import is_elevated_role
    from core.services.audit_service import serialize_project

    result = project_service.update_project(
        db,
        project_id=project_id,
        actor_user_id=actor.user_id,
        is_admin=is_elevated_role(actor.role),
        name=None,
        description=payload.project.description if "description" in fields else None,
        owner_group_id=None,
        cml_project_name=(
            payload.project.cml_project_name if "cml_project_name" in fields else None),
        mmp_project_id=(
            payload.project.mmp_project_id if "mmp_project_id" in fields else None),
        prod_stat_url=(
            payload.project.prod_stat_url if "prod_stat_url" in fields else None),
        serializer=serialize_project,
    )
    if result.status != "ok":
        raise ValidationError(
            f"Assets were updated, but the project fields could not be saved ({result.status})"
        )
    report["updated"]["project_fields"] = sorted(fields)


def _job_binding(job) -> dict:
    has_cml = bool((job.cml_job_name or "").strip())
    return {
        "entity": "job",
        "id": job.id,
        "name": job.cml_job_name or job.control_m_job_name or f"job-{job.id}",
        "applicable": has_cml,
        "bound": bool(job.cml_job_id) if has_cml else None,
        "error": job.cml_binding_error or "",
    }


def _app_binding(app) -> dict:
    return {
        "entity": "app",
        "id": app.id,
        "name": app.cml_application_name or f"app-{app.id}",
        "applicable": True,
        "bound": bool(app.cml_application_id),
        "error": app.cml_binding_error or "",
    }
