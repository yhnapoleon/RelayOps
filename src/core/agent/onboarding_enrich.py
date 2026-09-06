"""CML / MMP cross-checks for onboarding drafts (the network-aware layer).

``validate_payload`` stays pure and offline; this step runs right after it
and upgrades the report with what the platforms actually contain:

  * pulls the CML project names the Ops service identity can see — the same
    ``/api/v2/projectnames`` call the Create-Project picker uses — and the
    MMP project directory;
  * ``project.cml_project_name`` missing → the existing clarification gets
    ``kind=cml_project`` plus best-match candidates against the document's
    project name, so the reviewer confirms from a dropdown instead of typing
    blind;
  * ``cml_project_name`` present (project or per-job/app override) but with
    no exact CML match → a new clarification asking the reviewer to confirm
    the binding, with the closest visible CML projects as options;
  * exact CML match → an advisory "verified" warning so the reviewer knows
    the name was checked against the live platform;
  * ``project.mmp_project_id`` gets the same treatment against the MMP
    directory (``kind=mmp_project``; options carry business name + model
    count). When the document gave no MMP binding we only ask if the
    directory has a plausible name match — most projects simply have none.

Beyond upgrading the report, this step also *backfills the payload* from the
live platforms (the payload is persisted after enrichment on both the
extract and save paths, so backfills stick):

  * an MMP binding given as the numeric id from the document's
    ``…/project/<id>/projectDetails`` link is swapped for the repo name the
    platform stores (jobs too);
  * once the CML project binding is confirmed (exact match), the project's
    actual jobs/applications are fetched: exact-named draft jobs get their
    ``schedule_cron`` filled from the CML job's schedule, apps are matched by
    subdomain and get ``cml_application_name``/``cml_subdomain`` filled, and
    anything unmatched becomes a clarification with the real CML assets as
    options;
  * once the MMP binding is confirmed, draft jobs' ``mmp_model_id`` values
    are verified/canonicalized against the project's model list, and
    production models no draft job covers are surfaced as a warning.

Every network failure degrades to the plain text question (kind still set so
the UI renders the live-search combobox) — CML/MMP being unreachable must
never block or fail a draft.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from core.logging import get_logger
from core.agent.schemas import (
    AppDraft,
    Clarification,
    ClarificationOption,
    FieldIssue,
    OnboardingDraftPayload,
    ProductDraft,
    ValidationReport,
)

logger = get_logger(__name__)

_MATCH_FLOOR = 0.45        # below this a candidate is noise, not a suggestion
_UNPROMPTED_FLOOR = 0.6    # stricter when the document gave nothing to verify
_MAX_OPTIONS = 5


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-./]+", "", (s or "").lower())


def _score(query: str, candidate: str) -> float:
    a, b = _norm(query), _norm(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    base = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        base = max(base, 0.8)
    return base


def _top_matches(query: str, names: Iterable[str], *, floor: float = _MATCH_FLOOR,
                 limit: int = _MAX_OPTIONS) -> List[Tuple[float, str]]:
    scored = sorted(((_score(query, n), n) for n in set(names)), reverse=True)
    return [(s, n) for s, n in scored if s >= floor][:limit]


def _match_detail(score: float) -> str:
    if score >= 1.0:
        return "exact match"
    if score >= 0.8:
        return "highly similar"
    return "possibly related"


# ── platform fetchers (each failure returns None, never raises) ───────


def fetch_cml_project_names(extra_queries: Iterable[str] = ()) -> Optional[List[str]]:
    """First page of visible CML project names; when CML signals more pages,
    add targeted searches for the names we actually want to verify."""
    try:
        from core.services.cml_binding_resolver import build_control_interface

        control = build_control_interface()
        items, has_more = control.list_projects_page()
        pool = {str(it.get("name") or "") for it in items}
        if has_more:
            for q in {q.strip() for q in extra_queries if (q or "").strip()}:
                try:
                    more, _ = control.list_projects_page(name_filter=q)
                    pool.update(str(it.get("name") or "") for it in more)
                except Exception:  # noqa: BLE001 — partial pool is still useful
                    logger.opt(exception=True).warning(
                        "onboarding enrich: targeted CML search {!r} failed", q)
        pool.discard("")
        return sorted(pool)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning("onboarding enrich: CML project list unavailable")
        return None


def fetch_cml_project_assets(project_name: str) -> Optional[dict]:
    """Jobs + applications of one visible CML project, for payload backfill.

    Returns ``{"jobs": [{"name", "schedule"}], "apps": [{"name", "subdomain"}]}``
    or None when CML is unreachable / the project can't be resolved.
    """
    try:
        from core.services.cml_binding_resolver import build_control_interface

        control = build_control_interface()
        project_id = control.resolve_project_id(project_name)
        jobs = [{
            "name": str(j.get("name") or ""),
            "schedule": str(j.get("schedule") or ""),
        } for j in control.list_jobs(project_id)]
        apps = [{
            "name": str(a.get("name") or ""),
            "subdomain": str(a.get("subdomain") or ""),
        } for a in control.list_applications(project_id)]
        return {"jobs": jobs, "apps": apps}
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "onboarding enrich: CML assets for {!r} unavailable", project_name)
        return None


def fetch_mmp_projects() -> Optional[List[dict]]:
    """MMP shallow directory → [{repo_name, id, business_name, model_count, models}]."""
    try:
        from core.config import get_config
        from core.integrations.mmp_interface import MmpInterface

        cfg = get_config()
        iface = MmpInterface(
            base_url=cfg.mmp_base_url,
            bearer_token=cfg.mmp_bearer_token,
            refresh_token=cfg.mmp_refresh_token,
            verify_ssl=cfg.mmp_verify_ssl,
            ca_bundle=cfg.mmp_ca_bundle_path or None,
            timeout=float(cfg.mmp_timeout_seconds),
        )
        if not iface.is_configured():
            return None
        directory = iface.list_projects_shallow()
        return [{
            "repo_name": repo_name,
            "id": info.get("id"),
            "business_name": (info.get("business_name") or "") or "",
            "model_count": len(info.get("models") or []),
            "models": [{
                "id": m.get("id"),
                "name": str(m.get("name") or ""),
                "is_production": bool(m.get("is_production")),
            } for m in (info.get("models") or [])],
        } for repo_name, info in directory.items()]
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning("onboarding enrich: MMP directory unavailable")
        return None


def _mmp_entry_from_project(payload: dict) -> Optional[dict]:
    """RelayOps  mmp entry from project."""
    proj = payload.get("project") if isinstance(payload.get("project"), dict) else payload
    repo_name = proj.get("project_repo_name") or ""
    if not repo_name:
        base = (proj.get("project_name") or proj.get("repo_name")
                or proj.get("name") or "")
        entity = proj.get("entity") or ""
        repo_name = f"{base}@{entity}" if (base and entity) else base
    if not repo_name:
        return None
    models = [{
        "id": m.get("id"),
        "name": str(m.get("model_name") or m.get("name") or ""),
        "is_production": bool(m.get("is_production")),
    } for m in (proj.get("models") or [])]
    return {
        "repo_name": repo_name,
        "id": proj.get("id"),
        "business_name": proj.get("business_understanding_project_name") or "",
        "model_count": len(models),
        "models": models,
    }


def fetch_mmp_project_by_id(numeric_id: int) -> Optional[dict]:
    """Resolve one MMP project directly by its numeric id (the value in the
    document's ``…/project/<id>/…`` link). Used when the project isn't on the
    shallow directory's page — projects the list endpoint *hides* (inactive /
    permission-scoped, e.g. 160/174) are reachable only this way. Same shape as
    one :func:`fetch_mmp_projects` entry, or None."""
    try:
        from core.config import get_config
        from core.integrations.mmp_interface import MmpInterface

        cfg = get_config()
        iface = MmpInterface(
            base_url=cfg.mmp_base_url,
            bearer_token=cfg.mmp_bearer_token,
            refresh_token=cfg.mmp_refresh_token,
            verify_ssl=cfg.mmp_verify_ssl,
            ca_bundle=cfg.mmp_ca_bundle_path or None,
            timeout=float(cfg.mmp_timeout_seconds),
        )
        if not iface.is_configured():
            return None
        payload = iface.get_project(int(numeric_id))
        if not isinstance(payload, dict):
            return None
        entry = _mmp_entry_from_project(payload)
        if entry is None:
            logger.warning(
                "onboarding enrich: MMP project id={} has no repo_name field; payload keys={}",
                numeric_id, list(payload.keys())[:20])
        return entry
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "onboarding enrich: MMP project id={} direct lookup failed", numeric_id)
        return None


# ── MMP multi-track binding resolver ───────────────────────────────────

_MMP_URL_PROJECT_RE = re.compile(r"/project/(\d+)", re.IGNORECASE)
_MMP_URL_MODEL_RE = re.compile(r"/model/(\d+)", re.IGNORECASE)
_MMP_NAME_FLOOR = 0.7         # fuzzy-name auto-bind threshold (repo/stem/business)
_MMP_AMBIGUITY_DELTA = 0.06   # within this of the top → a tie → ask, don't guess

_MMP_TRACK_LABELS = {
    "url": "by link id", "id": "by numeric id",
    "name-exact": "by exact name match", "name-fuzzy": "by fuzzy name match",
}


@dataclass
class MmpMatch:
    """Outcome of resolving one documented MMP project reference.

    ``repo_name`` is set (binding decided) only for a confident, unambiguous
    match. ``ambiguous`` flags a bare name that hit several ``@entity`` siblings
    — the caller must ASK (with ``candidates``) rather than pick one. ``track``
    records which signal won."""
    repo_name: str = ""
    numeric_id: Optional[int] = None
    track: str = ""
    confidence: float = 0.0
    ambiguous: bool = False
    candidates: List[str] = field(default_factory=list)   # ranked repo names


def _is_numeric_mmp_ref(raw: str) -> bool:
    """True when the binding is a bare number or an MMP **web link** id
    (``runtime-mmp-web…/project/<id>/projectDetails``). Such a value can only be
    resolved by a direct API id lookup — never by name. The MMP *platform*
    (web UI) and the MMP *API* use different id spaces, so a platform-link id
    that the API can't read by direct lookup is unresolvable by us: it must be
    filled by the reviewer, not guessed from a fuzzy name match."""
    raw = (raw or "").strip()
    return bool(raw.isdigit() or _MMP_URL_PROJECT_RE.search(raw))


def _mmp_name_candidates(query: str, projects: List[dict]) -> List[Tuple[float, str]]:
    """Score ``query`` against every MMP project on three name surfaces — the
    full repo (``demo-x@entity``), its stem before ``@`` (``demo-x``), and the
    business name — keeping the best score per repo. The stem surface is what
    lets a documented CML name (``material-classifier``) reach an MMP repo whose
    real key is ``demo-material-classifier@batch-inventory-scoring`` — an exact
    match never could."""
    best: dict = {}
    for p in projects:
        repo = p.get("repo_name") or ""
        if not repo:
            continue
        stem = repo.split("@", 1)[0]
        score = max(_score(query, repo), _score(query, stem),
                    _score(query, p.get("business_name") or ""))
        if score > best.get(repo, 0.0):
            best[repo] = score
    return sorted(((s, r) for r, s in best.items()), reverse=True)


def resolve_mmp_project(
    raw: str,
    name_fallbacks: Tuple[str, ...] = (),
    *,
    projects: List[dict],
    by_id: dict,
    repo_names: set,
    fetch_by_id=None,
) -> MmpMatch:
    """Multi-track resolution of a documented MMP project reference into the
    canonical ``project_repo_name``.

    Tracks, highest-trust first:
      1. **url**  — a ``…/project/<id>/…`` link → numeric id → repo (the id
         disambiguates which ``@entity`` sibling, which a bare name cannot);
      2. **id**   — the value is a bare number → same id lookup;
      3. **name-exact** — the value (or a CML/display fallback) equals a repo
         name ignoring case/separators;
      4. **name-fuzzy** — scored against repo / stem / business name. A single
         clear winner binds; several near-tied ``@entity`` siblings → return
         ``ambiguous`` with candidates so the caller asks instead of guessing.

    The id tracks may grow ``projects`` / ``by_id`` / ``repo_names`` in place via
    a direct by-id lookup when the project isn't on the shallow first page."""
    fetch_by_id = fetch_by_id or fetch_mmp_project_by_id
    raw = (raw or "").strip()

    # Tracks 1 & 2 — numeric id from a URL or a bare number.
    m = _MMP_URL_PROJECT_RE.search(raw)
    num = m.group(1) if m else (raw if raw.isdigit() else None)
    if num is not None:
        n = int(num)
        repo = by_id.get(n)
        if repo is None:
            entry = fetch_by_id(n)
            if entry is not None:
                projects.append(entry)
                repo_names.add(entry["repo_name"])
                if entry.get("id") is not None:
                    by_id[int(entry["id"])] = entry["repo_name"]
                repo = entry["repo_name"]
        if repo is not None:
            return MmpMatch(repo_name=repo, numeric_id=n,
                            track="url" if m else "id", confidence=1.0)

    queries = [q for q in (raw, *name_fallbacks) if (q or "").strip()]

    # Track 3 — exact normalized name on the full repo@entity key.
    for q in queries:
        hit = _canonical_in_pool(q, repo_names)
        if hit:
            return MmpMatch(repo_name=hit, track="name-exact", confidence=1.0)

    # Track 4 — fuzzy name across repo / stem / business, ambiguity-guarded.
    ranked: List[Tuple[float, str]] = []
    for q in queries:
        ranked = _mmp_name_candidates(q, projects)
        if ranked and ranked[0][0] >= _MMP_NAME_FLOOR:
            break
    cand_names = [r for _, r in ranked[:_MAX_OPTIONS]]
    if not ranked or ranked[0][0] < _MMP_NAME_FLOOR:
        return MmpMatch(candidates=cand_names)
    top = ranked[0][0]
    tied = [r for s, r in ranked if top - s <= _MMP_AMBIGUITY_DELTA]
    if len(tied) > 1:
        return MmpMatch(track="name-fuzzy", confidence=top,
                        ambiguous=True, candidates=cand_names)
    return MmpMatch(repo_name=ranked[0][1], track="name-fuzzy",
                    confidence=top, candidates=cand_names)


def _llm_pick_mmp(text: str, context: str, candidates: List[str], *,
                  projects: List[dict]) -> Optional[str]:
    """LLM picks the matching MMP project repo from a bounded candidate list
    (or None). Output is constrained to the list — a pick that isn't a
    candidate is discarded, so the model can never invent a binding. Each
    candidate is shown with its business name + model names so the model can
    disambiguate ``@entity`` siblings (…@batch- vs …@dynamic-) from the
    document's semantics, which string similarity cannot."""
    if not candidates:
        return None
    try:
        from pydantic import BaseModel

        from core.agent.llm import get_chat_model
        from core.agent.prompts import MMP_MATCH_SYSTEM
        from core.agent.structured import invoke_structured

        class _Pick(BaseModel):
            project_repo_name: str = ""

        by_repo = {p["repo_name"]: p for p in projects}

        def _line(repo: str) -> str:
            p = by_repo.get(repo, {})
            biz = p.get("business_name") or ""
            models = "、".join(m.get("name", "") for m in (p.get("models") or [])[:6]
                               if m.get("name"))
            return (f"- {repo}" + (f" (business name: {biz})" if biz else "")
                    + (f" models: {models}" if models else ""))

        listing = "\n".join(_line(c) for c in candidates)
        chat = get_chat_model(temperature=0)
        result = invoke_structured(chat, _Pick, [
            ("system", MMP_MATCH_SYSTEM),
            ("human", f"{context}\n\nDocument snippet:\n{text[:4000]}\n\n"
                      f"Candidate MMP projects (pick only from these):\n{listing}"),
        ])
        pick = result.project_repo_name.strip()
        if not pick:
            return None
        by_norm = {_norm(c): c for c in candidates}
        return by_norm.get(_norm(pick))   # constrain to the offered list
    except Exception:  # noqa: BLE001 — matching must never fail the pipeline
        logger.opt(exception=True).warning("onboarding: LLM MMP match failed")
        return None


def resolve_mmp_binding(payload: OnboardingDraftPayload, text: str) -> None:
    """LangGraph step (text-aware): settle MMP project bindings, with an LLM
    fallback for the cases difflib can't safely decide.

    Order of trust mirrors the CML path:

      1. the deterministic multi-track resolver (url / id / exact-name) is
         ground truth and is taken untouched;
      2. a *confident, unique* fuzzy match is taken too;
      3. only when difflib is **ambiguous** (a bare name hitting several
         ``@entity`` siblings — ``…@batch-inventory-scoring`` vs
         ``…@dynamic-inventory-scoring``) or below the floor do we ask the LLM to
         pick from those exact candidates — the right entity is a semantic call
         (the document's "DNS" / "NS GE" / business name), not a string-distance
         one. The pick is constrained to the offered repos; an abstain leaves
         the field for the enrich-layer clarification.

    Runs in the text-aware node (``node_resolve_mmp``); the offline enrich pass
    then verifies whatever this settled and only asks when still unresolved.
    Mutates ``payload`` in place; never raises."""
    mmp_projects = fetch_mmp_projects()
    if not mmp_projects:
        return  # MMP unreachable → leave to the offline / clarification path
    by_id = {int(p["id"]): p["repo_name"]
             for p in mmp_projects if p.get("id") is not None}
    repo_names = {p["repo_name"] for p in mmp_projects}

    def _settle(raw: str, fallbacks: Tuple[str, ...], ctx: str) -> str:
        match = resolve_mmp_project(
            raw, fallbacks, projects=mmp_projects, by_id=by_id,
            repo_names=repo_names)
        if match.repo_name and not match.ambiguous:
            return match.repo_name                       # tracks 1/2/3 + confident fuzzy
        # Never let the LLM "resolve" an unreadable numeric MMP-platform id by
        # guessing from loose fuzzy candidates — a number carries no semantics.
        # The LLM only earns a say when there is a real NAME to disambiguate
        # among @entity siblings (match.ambiguous). Otherwise leave it for the
        # reviewer's manual pick (enrich emits the platform-id clarification).
        if _is_numeric_mmp_ref(raw) and not match.ambiguous:
            return ""
        return _llm_pick_mmp(text, ctx, match.candidates, projects=mmp_projects) or ""

    project = payload.project
    raw = project.mmp_project_id.strip()
    if raw:
        repo = _settle(raw, (project.cml_project_name, project.name),
                       f"Project display name: {project.name or '(not given)'}")
        if repo and repo != raw:
            project.mmp_project_id = repo
            payload.warnings.append(
                f"MMP binding 「{raw}」 resolved to 「{repo}」 via multi-track "
                "matching (with LLM fallback); auto-bound — please verify in the "
                "MMP validation area")
    for prod in payload.products:
        for job in prod.jobs:
            jraw = job.mmp_project_id.strip()
            if not jraw:
                continue
            ctx = "Job clue: " + (job.cml_job_name or job.mmp_model_id
                                  or job.description or job.source_quote or "(none)")
            repo = _settle(jraw, (job.cml_project_name, project.cml_project_name,
                                  project.name), ctx)
            if repo and repo != jraw:
                job.mmp_project_id = repo
                payload.warnings.append(
                    f"Job's MMP binding 「{jraw}」 resolved to 「{repo}」 via "
                    "multi-track matching (with LLM fallback); auto-bound — please verify")


# ── report enrichment ─────────────────────────────────────────────────


def _clar_by_path(report: ValidationReport) -> dict:
    return {c.field_path: c for c in report.clarifications}


def _cml_options(query: str, pool: List[str], *, floor: float = _MATCH_FLOOR) -> List[ClarificationOption]:
    return [ClarificationOption(value=n, detail=_match_detail(s))
            for s, n in _top_matches(query, pool, floor=floor)]


def _canonical_in_pool(given: str, pool: Iterable[str]) -> Optional[str]:
    """The pool name equal to ``given`` ignoring case and separators
    (``- _ . / space``), when exactly one matches. Lets a documented
    ``northstar-forecast`` bind to a CML ``northstar_forecast`` / ``NORTHSTAR-FORECAST`` instead of
    being rejected over a single hyphen — and returns the CML spelling so the
    stored binding matches what submit-time resolution (exact match) needs."""
    pool = list(pool)
    if given in pool:
        return given
    ng = _norm(given)
    if not ng:
        return None
    matches = [n for n in pool if _norm(n) == ng]
    return matches[0] if len(matches) == 1 else None


def _add_clarification(report: ValidationReport, *, path: str, question: str,
                       reason: str, kind: str,
                       options: List[ClarificationOption]) -> None:
    existing = _clar_by_path(report).get(path)
    if existing is not None:
        existing.kind = kind
        if options:
            existing.options = options
        return
    report.clarifications.append(Clarification(
        id=f"q::{path}", field_path=path, question=question, reason=reason,
        kind=kind, options=options,
    ))


def _subdomain_from_url(url: str) -> str:
    m = re.match(r"(?:https?://)?([a-z0-9][a-z0-9-]*)\.", (url or "").strip(), re.IGNORECASE)
    return m.group(1) if m else ""


def _warn(report: ValidationReport, path: str, message: str) -> None:
    report.warnings.append(FieldIssue(field_path=path, message=message))


def _drop_clarification(report: ValidationReport, path: str) -> None:
    """A backfill just filled this field — its 'document didn't give this'
    question (added by the offline validate pass) is now stale."""
    report.clarifications = [c for c in report.clarifications if c.field_path != path]


def _backfill_cml_assets(payload: OnboardingDraftPayload, report: ValidationReport,
                         assets: dict) -> None:
    """The CML binding is confirmed — fill the draft from what the project
    actually contains, and turn mismatches into option-backed questions."""
    binding = payload.project.cml_project_name.strip()
    cml_jobs = [j for j in assets.get("jobs") or [] if j.get("name")]
    cml_apps = [a for a in assets.get("apps") or [] if a.get("name") or a.get("subdomain")]
    jobs_by_norm = {_norm(j["name"]): j for j in cml_jobs}
    job_names = [j["name"] for j in cml_jobs]
    matched_cml_jobs: set = set()

    def _job_options(query: str) -> List[ClarificationOption]:
        by_name = {j["name"]: j for j in cml_jobs}
        ranked = _top_matches(query, job_names) if query.strip() else []
        names = [n for _, n in ranked] or job_names[:_MAX_OPTIONS]
        return [ClarificationOption(
            value=n,
            detail=f"cron: {by_name[n]['schedule']}" if by_name[n]["schedule"] else "no schedule",
        ) for n in names]

    for pi, prod in enumerate(payload.products):
        for ji, job in enumerate(prod.jobs):
            override = job.cml_project_name.strip()
            if override and _norm(override) != _norm(binding):
                continue  # bound to a different CML project — not our assets
            jpath = f"products[{pi}].jobs[{ji}]"
            label = job.cml_job_name or job.control_m_job_name or f"Job #{ji + 1}"
            name = job.cml_job_name.strip()
            if name:
                hit = jobs_by_norm.get(_norm(name))
                if hit is None:
                    _add_clarification(
                        report, path=f"{jpath}.cml_job_name",
                        question=(f"Job 「{name}」 not found in CML project 「{binding}」; "
                                  "please confirm from that project's actual jobs"),
                        reason="cml-job-not-found", kind="text",
                        options=_job_options(name),
                    )
                    continue
                matched_cml_jobs.add(hit["name"])
                if hit["name"] != name:
                    job.cml_job_name = hit["name"]
                    _warn(report, f"{jpath}.cml_job_name",
                          f"Corrected to the actual CML spelling: 「{name}」 → 「{hit['name']}」")
                if hit["schedule"] and not job.schedule_cron.strip():
                    job.schedule_cron = hit["schedule"]
                    _drop_clarification(report, f"{jpath}.schedule_cron")
                    _warn(report, f"{jpath}.schedule_cron",
                          f"Backfilled cron from CML job 「{hit['name']}」's schedule: {hit['schedule']}")
            elif cml_jobs and not job.control_m_job_name.strip():
                # No schedulable identity (no CML job name, no Control-M name).
                # An MMP-only job in particular can't be monitored for
                # not-triggered/failed without one — so offer the CML
                # project's real jobs to pick from, even when an MMP binding
                # is present (model = drift target, CML job = schedule target).
                is_mmp = bool(job.mmp_model_id.strip() or job.mmp_project_id.strip())
                query = job.mmp_model_id or job.description or job.source_quote
                question = (
                    f"MMP job 「{label}」 has no Control-M / CML job name, so "
                    "missed-run / failure monitoring isn't possible; the jobs below "
                    f"were pulled from CML project 「{binding}」 — please pick the matching scoring job"
                ) if is_mmp else (
                    f"Job 「{label}」 has no CML job name; CML project 「{binding}」 has these jobs to choose from"
                )
                _add_clarification(
                    report, path=f"{jpath}.cml_job_name",
                    question=question,
                    reason="cml-job-for-mmp" if is_mmp else "cml-job-unbound",
                    kind="text",
                    options=_job_options(query),
                )

    leftover = [j for j in cml_jobs
                if j["schedule"] and j["name"] not in matched_cml_jobs]
    if leftover:
        _warn(report, "products",
              f"CML project 「{binding}」 has {len(leftover)} more scheduled job(s) not yet included: "
              + "、".join(j["name"] for j in leftover))

    apps_by_sub = {_norm(a["subdomain"]): a for a in cml_apps if a["subdomain"]}
    apps_by_name = {_norm(a["name"]): a for a in cml_apps if a["name"]}
    app_names = [a["name"] for a in cml_apps if a["name"]]
    for pi, prod in enumerate(payload.products):
        for ai, app in enumerate(prod.apps):
            override = app.cml_project_name.strip()
            if override and _norm(override) != _norm(binding):
                continue
            apath = f"products[{pi}].apps[{ai}]"
            label = app.cml_application_name or f"App #{ai + 1}"
            sub = app.cml_subdomain.strip() or _subdomain_from_url(app.application_url)
            hit = apps_by_sub.get(_norm(sub)) if sub else None
            if hit is None and app.cml_application_name.strip():
                hit = apps_by_name.get(_norm(app.cml_application_name))
            if hit is not None:
                if not app.cml_application_name.strip() and hit["name"]:
                    app.cml_application_name = hit["name"]
                    _drop_clarification(report, f"{apath}.cml_application_name")
                    _warn(report, f"{apath}.cml_application_name",
                          f"Backfilled the application name 「{hit['name']}」 from CML by subdomain 「{hit['subdomain']}」")
                if not app.cml_subdomain.strip() and hit["subdomain"]:
                    app.cml_subdomain = hit["subdomain"]
                    _warn(report, f"{apath}.cml_subdomain",
                          f"Backfilled subdomain 「{hit['subdomain']}」 from CML")
            elif cml_apps:
                _add_clarification(
                    report, path=f"{apath}.cml_application_name",
                    question=(f"App 「{label}」 has no matching application in CML "
                              f"project 「{binding}」 (neither name nor subdomain "
                              "matched); please confirm"),
                    reason="cml-app-not-found", kind="text",
                    options=[ClarificationOption(
                        value=n,
                        detail=f"subdomain: {apps_by_name[_norm(n)]['subdomain']}",
                    ) for _, n in _top_matches(
                        app.cml_application_name or sub or label, app_names)]
                    or [ClarificationOption(
                        value=a["name"], detail=f"subdomain: {a['subdomain']}")
                        for a in cml_apps[:_MAX_OPTIONS] if a["name"]],
                )


def _backfill_mmp_models(payload: OnboardingDraftPayload, report: ValidationReport,
                         entry: dict) -> None:
    """The MMP binding is confirmed — verify/canonicalize each job's model
    name against the project's model list, and flag uncovered prod models."""
    repo = str(entry.get("repo_name") or "")
    models = [m for m in (entry.get("models") or []) if m.get("name")]
    if not repo or not models:
        return
    by_norm = {_norm(m["name"]): m["name"] for m in models}
    by_model_id = {int(m["id"]): m["name"]
                   for m in models if m.get("id") is not None}
    names = [m["name"] for m in models]
    matched: set = set()

    for pi, prod in enumerate(payload.products):
        for ji, job in enumerate(prod.jobs):
            if not (job.mmp_model_id.strip() or job.mmp_project_id.strip()):
                continue  # job never claimed an MMP binding
            if job.mmp_project_id.strip() and job.mmp_project_id.strip() != repo:
                continue  # bound to a different MMP project
            jpath = f"products[{pi}].jobs[{ji}]"
            label = (job.cml_job_name or job.control_m_job_name
                     or job.mmp_model_id or f"Job #{ji + 1}")
            if not job.mmp_project_id.strip():
                job.mmp_project_id = repo
                _warn(report, f"{jpath}.mmp_project_id",
                      f"Backfilled the project-level MMP binding 「{repo}」")
            model = job.mmp_model_id.strip()
            if not model:
                query = job.cml_job_name or job.description or job.source_quote
                ranked = _top_matches(query, names) if query.strip() else []
                _add_clarification(
                    report, path=f"{jpath}.mmp_model_id",
                    question=(f"Job 「{label}」 enabled an MMP binding but gave no "
                              f"model name; please pick from MMP project 「{repo}」's models"),
                    reason="mmp-model-missing", kind="text",
                    options=[ClarificationOption(value=n, detail=_match_detail(s))
                             for s, n in ranked]
                    or [ClarificationOption(value=n) for n in names[:_MAX_OPTIONS]],
                )
                continue
            # Model track 1 — a /model/<id> link or a bare numeric model id →
            # the model's name (the URL the document actually carries). The
            # name tracks below can't use a number, and this disambiguates
            # without any fuzzy guessing.
            mm = _MMP_URL_MODEL_RE.search(model)
            mnum = mm.group(1) if mm else (model if model.isdigit() else None)
            if mnum is not None and int(mnum) in by_model_id:
                canon = by_model_id[int(mnum)]
                matched.add(canon)
                if canon != model:
                    job.mmp_model_id = canon
                    _warn(report, f"{jpath}.mmp_model_id",
                          f"Resolved MMP model id 「{model}」 to model 「{canon}」 "
                          f"（{_MMP_TRACK_LABELS['url' if mm else 'id']}）")
                continue
            # Model tracks 2/3 — exact, then fuzzy, on the model name.
            hit = by_norm.get(_norm(model))
            if hit is not None:
                matched.add(hit)
                if hit != model:
                    job.mmp_model_id = hit
                    _warn(report, f"{jpath}.mmp_model_id",
                          f"Corrected to the actual MMP model name: 「{model}」 → 「{hit}」")
            else:
                _add_clarification(
                    report, path=f"{jpath}.mmp_model_id",
                    question=(f"Job 「{label}」's MMP model 「{model}」 is not found "
                              f"under project 「{repo}」; please confirm"),
                    reason="mmp-model-not-found", kind="text",
                    options=[ClarificationOption(value=n, detail=_match_detail(s))
                             for s, n in _top_matches(model, names)],
                )

    uncovered = [m["name"] for m in models
                 if m.get("is_production") and m["name"] not in matched]
    if uncovered:
        _warn(report, "products",
              f"MMP project 「{repo}」 has production models not bound to any job: "
              + ", ".join(uncovered) + " (add them if drift monitoring is needed)")


def _cml_query_tokens(payload: OnboardingDraftPayload) -> List[str]:
    """Document terms worth a targeted CML search — the project's own name
    rarely matches the CML repo name (Inventory Scoring ↔ material-classifier),
    but the bitbucket repo / app subdomain usually do."""
    project = payload.project
    tokens = [project.cml_project_name, project.name]
    for prod in payload.products:
        for job in prod.jobs:
            tokens.append(job.cml_project_name)
        for app in prod.apps:
            tokens.append(app.cml_project_name)
            tokens.append(app.cml_subdomain)
            tokens.append(_subdomain_from_url(app.application_url))
    return [t for t in tokens if (t or "").strip()]


def _llm_pick_cml(text: str, project_name: str, candidates: List[str]) -> Optional[str]:
    """LLM picks the matching CML project from a bounded candidate list (or
    None). Output is constrained to the list — a pick that isn't a candidate
    is discarded, so the model can never invent a binding."""
    if not candidates:
        return None
    try:
        from pydantic import BaseModel

        from core.agent.llm import get_chat_model
        from core.agent.prompts import CML_MATCH_SYSTEM
        from core.agent.structured import invoke_structured

        class _Pick(BaseModel):
            cml_project_name: str = ""

        listing = "\n".join(f"- {c}" for c in candidates)
        chat = get_chat_model(temperature=0)
        result = invoke_structured(chat, _Pick, [
            ("system", CML_MATCH_SYSTEM),
            ("human", f"Project display name: {project_name}\n\nDocument snippet:\n{text[:4000]}\n\n"
                      f"Candidate CML projects (pick only from these):\n{listing}"),
        ])
        pick = result.cml_project_name.strip()
        if not pick:
            return None
        by_norm = {_norm(c): c for c in candidates}
        return by_norm.get(_norm(pick))  # constrain to the offered list
    except Exception:  # noqa: BLE001 — matching must never fail the pipeline
        logger.opt(exception=True).warning("onboarding: LLM CML match failed")
        return None


def resolve_cml_binding(payload: OnboardingDraftPayload, text: str) -> None:
    """LangGraph step: settle ``project.cml_project_name`` against the live
    CML project list, using the LLM to bridge display-name ⇄ repo-name gaps
    that string similarity can't (Inventory Scoring ↔ material-classifier).

    Order of trust: a deterministic binding already present (e.g. parsed from
    the prod-stat URL) wins and is only spelling-normalized — never overridden
    by a guess. Only when there is no resolvable binding do we ask the LLM to
    pick from the fetched candidates; an empty pick leaves the field for the
    reviewer's clarification (with the same candidates as options). Mutates
    ``payload`` in place; never raises.
    """
    project = payload.project
    given = project.cml_project_name.strip()
    pool = fetch_cml_project_names(extra_queries=_cml_query_tokens(payload))
    if not pool:
        return  # CML unreachable → leave to the offline / clarification path

    # 1) Deterministic binding is ground truth — keep it (fix spelling only).
    canon = _canonical_in_pool(given, pool) if given else None
    if canon:
        if canon != given:
            project.cml_project_name = canon
            payload.warnings.append(
                f"CML project name corrected to the platform's actual spelling: 「{given}」 → 「{canon}」")
        return

    # 2) No resolvable binding → let the LLM match within the fetched pool.
    query = project.name or given
    shortlist = pool if len(pool) <= 60 else [
        n for _, n in _top_matches(query, pool, floor=0.0, limit=60)]
    pick = _llm_pick_cml(text, query, shortlist)
    if pick:
        project.cml_project_name = pick
        payload.warnings.append(
            f"CML project matched by AI to 「{pick}」 among {len(shortlist)} real "
            "candidates; auto-bound — please verify in the CML validation area")


def _llm_match_jobs(text: str, jobs_ctx: List[dict], cml_jobs: List[dict]) -> List[str]:
    """LLM maps each context job to a real CML job name (or ""), constrained to
    the fetched list. Returns one pick per ``jobs_ctx`` entry (same order)."""
    if not jobs_ctx or not cml_jobs:
        return ["" for _ in jobs_ctx]
    try:
        from pydantic import BaseModel, Field

        from core.agent.llm import get_chat_model
        from core.agent.prompts import CML_JOB_MATCH_SYSTEM
        from core.agent.structured import invoke_structured

        class _Matches(BaseModel):
            matches: List[str] = Field(default_factory=list)

        wanted = "\n".join(
            f"{i}. MMP model={c.get('mmp_model_id') or '-'} | description={c.get('desc', '')[:160]!r} "
            f"| source={c.get('quote', '')[:120]!r}"
            for i, c in enumerate(jobs_ctx)
        )
        real = "\n".join(
            f"- {j['name']}" + (f" (schedule {j['schedule']})" if j.get("schedule") else "")
            for j in cml_jobs
        )
        chat = get_chat_model(temperature=0)
        result = invoke_structured(chat, _Matches, [
            ("system", CML_JOB_MATCH_SYSTEM),
            ("human", f"Document snippet:\n{text[:3000]}\n\nJobs to match:\n{wanted}\n\n"
                      f"Real job list (pick only from these):\n{real}"),
        ])
        by_norm = {_norm(j["name"]): j["name"] for j in cml_jobs}
        picks = list(result.matches) + [""] * len(jobs_ctx)
        return [by_norm.get(_norm((p or "").strip()), "") for p in picks[:len(jobs_ctx)]]
    except Exception:  # noqa: BLE001 — matching must never fail the pipeline
        logger.opt(exception=True).warning("onboarding: LLM CML job match failed")
        return ["" for _ in jobs_ctx]


def resolve_cml_assets(payload: OnboardingDraftPayload, text: str) -> None:
    """LangGraph step (runs right after the project binding is settled): for
    jobs with no schedulable identity — the MMP stub, or a job whose invented
    name was cleared — let the LLM pick the matching real CML job from the
    bound project's job list. Only fills empties (never overrides a name the
    document gave), so there is nothing to clobber. Mutates ``payload``; never
    raises."""
    binding = payload.project.cml_project_name.strip()
    if not binding:
        return
    assets = fetch_cml_project_assets(binding)
    if not assets:
        return
    cml_jobs = [j for j in assets.get("jobs") or [] if j.get("name")]
    if not cml_jobs:
        return
    jobs_by_norm = {_norm(j["name"]): j for j in cml_jobs}

    # Cleanup: extraction sometimes stuffs the CML *project* name into a job's
    # cml_job_name (an MMP-only job has no real job name in the document, so the
    # project name gets borrowed). That bogus value is non-empty, so it both
    # blocks the LLM auto-match below (which only fills empties) and makes every
    # such job a spurious "not found in project" question. When a job's name is
    # exactly its CML project's name AND that name is not a real job in the
    # project, treat it as unprovided so the matcher can bind it properly.
    for prod in payload.products:
        for job in prod.jobs:
            eff = _effective_cml_binding(job.cml_project_name, binding)
            name = job.cml_job_name.strip()
            if (name and _norm(eff) == _norm(binding)
                    and _norm(name) == _norm(binding)
                    and _norm(name) not in jobs_by_norm):
                job.cml_job_name = ""

    targets: List = []      # (job, context) for jobs needing a name
    for prod in payload.products:
        for job in prod.jobs:
            override = job.cml_project_name.strip()
            if override and _norm(override) != _norm(binding):
                continue
            if not job.cml_job_name.strip() and not job.control_m_job_name.strip():
                targets.append(job)
    if not targets:
        return

    ctx = [{
        "mmp_model_id": j.mmp_model_id, "desc": j.description, "quote": j.source_quote,
    } for j in targets]
    picks = _llm_match_jobs(text, ctx, cml_jobs)
    schedule_by_norm = {_norm(j["name"]): j.get("schedule", "") for j in cml_jobs}
    for job, pick in zip(targets, picks):
        if not pick:
            continue
        job.cml_job_name = pick
        sched = schedule_by_norm.get(_norm(pick), "")
        if sched and not job.schedule_cron.strip():
            job.schedule_cron = sched
        payload.warnings.append(
            f"Job matched by AI to the real job 「{pick}」 under CML project 「{binding}」"
            + (f" (schedule {sched})" if sched else "") + "; auto-bound — please verify"
        )


def _effective_cml_binding(override: str, project_binding: str) -> str:
    """The CML project an asset actually binds to: its own override if set,
    else the project-level binding."""
    return (override or "").strip() or (project_binding or "").strip()


def partition_payload_by_cml_project(
    payload: OnboardingDraftPayload,
) -> "List[Tuple[str, OnboardingDraftPayload]]":
    """Split one draft payload into one payload per CML project the assets
    actually bind to (each asset's override, else the project binding). Returns
    ``[(cml_project, payload), …]`` ordered with the original project binding
    first; a single-project draft yields a one-element list (caller treats <2
    as "nothing to split"). Pure — never mutates the input."""
    project_binding = payload.project.cml_project_name.strip()

    buckets: "List[str]" = []      # insertion-ordered distinct CML projects
    def _bucket(name: str) -> str:
        for b in buckets:
            if _norm(b) == _norm(name):
                return b
        buckets.append(name)
        return name

    # Origin binding leads, even if it ends up owning no asset (rare).
    if project_binding:
        _bucket(project_binding)

    placement: dict = {}   # cml project -> (jobs[], apps[]) per product index
    for pi, prod in enumerate(payload.products):
        for job in prod.jobs:
            b = _bucket(_effective_cml_binding(job.cml_project_name, project_binding))
            placement.setdefault(b, {}).setdefault(pi, ([], []))[0].append(job)
        for app in prod.apps:
            b = _bucket(_effective_cml_binding(app.cml_project_name, project_binding))
            placement.setdefault(b, {}).setdefault(pi, ([], []))[1].append(app)

    out: "List[Tuple[str, OnboardingDraftPayload]]" = []
    for b in buckets:
        per_product = placement.get(b)
        if not per_product:
            continue   # the origin binding with no asset of its own — skip
        is_origin = bool(project_binding) and _norm(b) == _norm(project_binding)
        proj = payload.project.model_copy(deep=True)
        proj.cml_project_name = b
        if not is_origin:
            # CML/MMP project-level specifics belonged to the original binding.
            proj.prod_stat_url = ""
            proj.mmp_project_id = ""
            proj.name = f"{payload.project.name} ({b})".strip()
        products: "List[ProductDraft]" = []
        for pi, prod in enumerate(payload.products):
            jobs, apps = per_product.get(pi, ([], []))
            if not jobs and not apps:
                continue
            np = ProductDraft(name=prod.name)
            for job in jobs:
                j = job.model_copy(deep=True)
                j.cml_project_name = ""   # now redundant with the bucket binding
                np.jobs.append(j)
            for app in apps:
                a = app.model_copy(deep=True)
                a.cml_project_name = ""
                np.apps.append(a)
            products.append(np)
        out.append((b, OnboardingDraftPayload(
            project=proj, products=products, warnings=list(payload.warnings))))
    return out


def reconcile_cross_project_assets(
    payload: OnboardingDraftPayload,
    report: ValidationReport,
    documented_projects: Iterable[str],
) -> None:
    """Cross-project asset reconciliation — the layer above the single-project
    backfill.

    A handover doc can name several CML projects (two prod-stat ``/view/``
    links — e.g. ``material-classifier`` for the train job, ``inventory-scoring-
    model`` for the deploy job), and a documented job/app may actually live in
    one of the *others*, not the primary binding. The single-project backfill
    only ever checks the bound project, so it reports each such asset as a flat
    'not found here' question and never realises the asset belongs elsewhere.

    For every CML project the document points at we fetch its live jobs/apps
    once, then decide which project truly contains each draft asset:

      * exactly one other documented project contains it (and the bound one
        does not) → **re-home** it: write the per-asset ``cml_project_name``
        override (submit honours it — see ``submit_payload``), pull the real
        schedule/spelling from that project, and drop the now-stale 'not found
        in the bound project' question;
      * more than one candidate contains it → a clarification asking which;
      * the draft ends up partitioned across ≥2 projects → one advisory warning
        so the reviewer can split into separate Ops projects (the manual flow).

    Only runs when the document references ≥2 CML projects and ≥2 are live —
    otherwise the per-asset backfill already says everything there is to say.
    Best-effort: any CML fetch failure just drops that project as evidence;
    never raises."""
    project_binding = payload.project.cml_project_name.strip()
    candidates: List[str] = []
    for nm in (*documented_projects, project_binding,
               *(j.cml_project_name for p in payload.products for j in p.jobs),
               *(a.cml_project_name for p in payload.products for a in p.apps)):
        nm = (nm or "").strip()
        if nm and not any(_norm(nm) == _norm(c) for c in candidates):
            candidates.append(nm)
    if len(candidates) < 2:
        return  # single project in play — per-asset backfill already covers it

    assets: dict = {}
    for nm in candidates:
        fetched = fetch_cml_project_assets(nm)
        if fetched is not None:
            assets[nm] = fetched
    if len(assets) < 2:
        return  # can't compare across projects — nothing reliable to add

    def _job_homes(name: str) -> List[str]:
        n = _norm(name)
        return [p for p, a in assets.items()
                if any(_norm(j.get("name", "")) == n for j in a.get("jobs") or [])]

    def _job_in(project: str, name: str) -> Optional[dict]:
        n = _norm(name)
        for j in assets.get(project, {}).get("jobs") or []:
            if _norm(j.get("name", "")) == n:
                return j
        return None

    def _app_homes(sub: str) -> List[str]:
        n = _norm(sub)
        return [p for p, a in assets.items()
                if any(_norm(x.get("subdomain", "")) == n for x in a.get("apps") or [])]

    placement: dict = {}   # project -> [asset labels actually living there]

    for pi, prod in enumerate(payload.products):
        for ji, job in enumerate(prod.jobs):
            name = job.cml_job_name.strip()
            if not name:
                continue   # nothing to match on — LLM/Control-M paths own this
            homes = _job_homes(name)
            if not homes:
                continue   # genuinely missing — the not-found question stands
            bound = _effective_cml_binding(job.cml_project_name, project_binding)
            if any(_norm(bound) == _norm(h) for h in homes):
                placement.setdefault(bound, []).append(f"job「{name}」")
                continue
            jpath = f"products[{pi}].jobs[{ji}]"
            if len(homes) == 1:
                home = homes[0]
                hit = _job_in(home, name)
                job.cml_project_name = home
                if hit and hit.get("name") and hit["name"] != name:
                    job.cml_job_name = hit["name"]
                if hit and hit.get("schedule") and not job.schedule_cron.strip():
                    job.schedule_cron = hit["schedule"]
                _drop_clarification(report, f"{jpath}.cml_job_name")
                _drop_clarification(report, f"{jpath}.cml_project_name")
                _warn(report, f"{jpath}.cml_project_name",
                      f"Job 「{name}」 is not in project 「{bound}」, but a job with "
                      f"the same name was found under another documented CML project "
                      f"「{home}」; re-homed to 「{home}」 — please verify")
                placement.setdefault(home, []).append(f"job「{name}」")
            else:
                _add_clarification(
                    report, path=f"{jpath}.cml_project_name",
                    question=(f"Job 「{name}」 is not found in project 「{bound}」, but "
                              "these documented CML projects all have a job with the "
                              "same name; please confirm which one it belongs to"),
                    reason="cml-job-cross-project", kind="cml_project",
                    options=[ClarificationOption(
                        value=h, detail=f"this project has a job named 「{name}」") for h in homes],
                )

        for ai, app in enumerate(prod.apps):
            sub = app.cml_subdomain.strip() or _subdomain_from_url(app.application_url)
            if not sub:
                continue
            homes = _app_homes(sub)
            if not homes:
                continue
            bound = _effective_cml_binding(app.cml_project_name, project_binding)
            if any(_norm(bound) == _norm(h) for h in homes):
                placement.setdefault(bound, []).append(f"app「{sub}」")
                continue
            apath = f"products[{pi}].apps[{ai}]"
            label = app.cml_application_name or sub
            if len(homes) == 1:
                home = homes[0]
                app.cml_project_name = home
                _drop_clarification(report, f"{apath}.cml_application_name")
                _drop_clarification(report, f"{apath}.cml_project_name")
                _warn(report, f"{apath}.cml_project_name",
                      f"App 「{label}」 is not in project 「{bound}」, but it was found "
                      f"by subdomain under another documented CML project 「{home}」; "
                      f"re-homed to 「{home}」 — please verify")
                placement.setdefault(home, []).append(f"app「{label}」")
            else:
                _add_clarification(
                    report, path=f"{apath}.cml_project_name",
                    question=(f"App 「{label}」 is not found in project 「{bound}」, but "
                              "these documented CML projects all have it; please "
                              "confirm which one it belongs to"),
                    reason="cml-app-cross-project", kind="cml_project",
                    options=[ClarificationOption(
                        value=h, detail=f"this project has this app (subdomain {sub})") for h in homes],
                )

    real = {p: items for p, items in placement.items() if items}
    if len(real) >= 2:
        summary = "; ".join(f"「{p}」有 {', '.join(items)}" for p, items in real.items())
        _warn(report, "project.cml_project_name",
              f"The documented assets belong to {len(real)} CML projects: {summary}. "
              "Only one Ops project was created, with assets re-homed to their "
              "respective CML projects; confirm the split card above to register "
              "them as separate Ops projects.")
        # Actionable counterpart to the advisory above: a project-level card the
        # reviewer confirms to split this draft into one sibling draft per CML
        # project (handled by the /split endpoint, not apply_answers — kind
        # 'project_split' is an action, never written back into a field).
        _add_clarification(
            report, path="__split__",
            question=(f"这篇文档的资产分布在 {len(real)} 个 CML project：{summary}。"
                      "是否拆分为多个独立登记（每个 CML project 一个）？"),
            reason="cross-project-split", kind="project_split",
            options=[ClarificationOption(value=p, detail="、".join(items))
                     for p, items in real.items()],
        )


def enrich_validation_report(payload: OnboardingDraftPayload,
                             report: ValidationReport) -> ValidationReport:
    """Mutates ``report`` — and, for platform-confirmed values, ``payload`` —
    in place (and returns the report). Never raises."""
    # The combobox kind is set even when the platforms are unreachable — the
    # preview UI's type-to-search hits the live picker endpoint on its own.
    for c in report.clarifications:
        if c.field_path.endswith("cml_project_name"):
            c.kind = "cml_project"
        elif c.field_path.endswith("mmp_project_id"):
            c.kind = "mmp_project"

    project = payload.project
    wanted = [project.cml_project_name, project.name]
    wanted += [j.cml_project_name for prod in payload.products for j in prod.jobs]
    wanted += [a.cml_project_name for prod in payload.products for a in prod.apps]
    cml_pool = fetch_cml_project_names(extra_queries=wanted)

    if cml_pool is not None:
        # Project-level binding. Match is tolerant of case/separator drift
        # (northstar-forecast ⇄ northstar_forecast) and rewrites to the CML spelling.
        given = project.cml_project_name.strip()
        canon = _canonical_in_pool(given, cml_pool) if given else None
        if not given:
            clar = _clar_by_path(report).get("project.cml_project_name")
            if clar is not None:
                clar.options = _cml_options(project.name, cml_pool)
        elif canon:
            if canon != given:
                project.cml_project_name = canon
                _drop_clarification(report, "project.cml_project_name")
                _warn(report, "project.cml_project_name",
                      f"Binding corrected to the actual CML name: 「{given}」 → 「{canon}」 (case/separator difference only)")
            else:
                report.warnings.append(FieldIssue(
                    field_path="project.cml_project_name",
                    message=f"Verified in CML: project 「{given}」 exists and is visible",
                ))
        else:
            _add_clarification(
                report, path="project.cml_project_name",
                question=(f"The CML project 「{given}」 from the document has no match "
                          "among the CML projects visible to you; please confirm the "
                          "correct project from the candidates (or search manually)"),
                reason="cml-project-not-found", kind="cml_project",
                options=_cml_options(given, cml_pool) or _cml_options(project.name, cml_pool),
            )

        # Per-job/app overrides: only checked when the document set one.
        for pi, prod in enumerate(payload.products):
            for ji, job in enumerate(prod.jobs):
                name = job.cml_project_name.strip()
                if not name:
                    continue
                canon = _canonical_in_pool(name, cml_pool)
                if canon and canon != name:
                    job.cml_project_name = canon
                    _warn(report, f"products[{pi}].jobs[{ji}].cml_project_name",
                          f"Corrected to the actual CML name: 「{name}」 → 「{canon}」 (case/separator difference only)")
                elif not canon:
                    label = job.cml_job_name or job.control_m_job_name or f"Job #{ji + 1}"
                    _add_clarification(
                        report, path=f"products[{pi}].jobs[{ji}].cml_project_name",
                        question=f"Job 「{label}」's CML project 「{name}」 is not found in CML; please confirm",
                        reason="cml-project-not-found", kind="cml_project",
                        options=_cml_options(name, cml_pool),
                    )
            for ai, app in enumerate(prod.apps):
                name = app.cml_project_name.strip()
                if not name:
                    continue
                canon = _canonical_in_pool(name, cml_pool)
                if canon and canon != name:
                    app.cml_project_name = canon
                    _warn(report, f"products[{pi}].apps[{ai}].cml_project_name",
                          f"Corrected to the actual CML name: 「{name}」 → 「{canon}」 (case/separator difference only)")
                elif not canon:
                    label = app.cml_application_name or f"App #{ai + 1}"
                    _add_clarification(
                        report, path=f"products[{pi}].apps[{ai}].cml_project_name",
                        question=f"App 「{label}」's CML project 「{name}」 is not found in CML; please confirm",
                        reason="cml-project-not-found", kind="cml_project",
                        options=_cml_options(name, cml_pool),
                    )

    # CML binding confirmed (or pool unavailable but a name was given) →
    # backfill the draft from the project's actual jobs/applications.
    bound = project.cml_project_name.strip()
    if bound and (cml_pool is None or bound in cml_pool):
        assets = fetch_cml_project_assets(bound)
        if assets is not None:
            _backfill_cml_assets(payload, report, assets)

    mmp_projects = fetch_mmp_projects()
    if mmp_projects is not None:
        repo_names = {p["repo_name"] for p in mmp_projects}
        by_id = {int(p["id"]): p["repo_name"]
                 for p in mmp_projects if p.get("id") is not None}

        by_repo = {p["repo_name"]: p for p in mmp_projects}

        def _mmp_option(repo: str, *, detail_prefix: str = "") -> ClarificationOption:
            p = by_repo.get(repo, {})
            return ClarificationOption(value=repo, detail=" · ".join(x for x in (
                detail_prefix, p.get("business_name") or "",
                f"{p.get('model_count', len(p.get('models') or []))} models") if x))

        raw = project.mmp_project_id.strip()
        if raw:
            match = resolve_mmp_project(
                raw, (project.cml_project_name, project.name),
                projects=mmp_projects, by_id=by_id, repo_names=repo_names)
            if match.ambiguous:
                _add_clarification(
                    report, path="project.mmp_project_id",
                    question=(f"The project-level MMP binding 「{raw}」 matched several "
                              "same-named MMP projects with different entities by name, "
                              "so which one is meant can't be determined; an MMP link "
                              "with /project/<id> can pinpoint it — please confirm"),
                    reason="mmp-project-ambiguous", kind="mmp_project",
                    options=[_mmp_option(r) for r in match.candidates])
            elif match.repo_name and match.repo_name != raw:
                project.mmp_project_id = match.repo_name
                _warn(report, "project.mmp_project_id",
                      f"Resolved MMP binding 「{raw}」 to MMP project 「{match.repo_name}」 "
                      f"（{_MMP_TRACK_LABELS.get(match.track, match.track)}）")
        for pi, prod in enumerate(payload.products):
            for ji, job in enumerate(prod.jobs):
                jraw = job.mmp_project_id.strip()
                if not jraw:
                    continue
                jpath = f"products[{pi}].jobs[{ji}].mmp_project_id"
                jmatch = resolve_mmp_project(
                    jraw, (job.cml_project_name, project.cml_project_name, project.name),
                    projects=mmp_projects, by_id=by_id, repo_names=repo_names)
                if jmatch.ambiguous:
                    label = job.cml_job_name or job.mmp_model_id or f"Job #{ji + 1}"
                    _add_clarification(
                        report, path=jpath,
                        question=(f"Job 「{label}」's MMP binding 「{jraw}」 matched "
                                  "several same-named MMP projects with different "
                                  "entities by name; please confirm which one (a link "
                                  "with /project/<id> can pinpoint it)"),
                        reason="mmp-project-ambiguous", kind="mmp_project",
                        options=[_mmp_option(r) for r in jmatch.candidates])
                elif jmatch.repo_name and jmatch.repo_name != jraw:
                    job.mmp_project_id = jmatch.repo_name
                    _warn(report, jpath,
                          f"Resolved MMP binding 「{jraw}」 to MMP project 「{jmatch.repo_name}」 "
                          f"（{_MMP_TRACK_LABELS.get(jmatch.track, jmatch.track)}）")

        given = project.mmp_project_id.strip()

        def _mmp_opts(query: str, *, floor: float) -> List[ClarificationOption]:
            # Match against both repo and business names; keep the best score
            # per repo so one project never shows up twice.
            best: dict = {}
            for p in mmp_projects:
                score = max(_score(query, p["repo_name"]),
                            _score(query, p["business_name"]))
                if score >= floor and score > best.get(p["repo_name"], 0.0):
                    best[p["repo_name"]] = score
            ranked = sorted(best.items(), key=lambda kv: -kv[1])[:_MAX_OPTIONS]
            return [ClarificationOption(
                value=repo,
                detail=" · ".join(x for x in (
                    _match_detail(score),
                    by_repo[repo]["business_name"],
                    f"{by_repo[repo]['model_count']} models",
                ) if x),
            ) for repo, score in ranked]

        if given and given not in repo_names and _is_numeric_mmp_ref(given):
            # A bare number / MMP web-link id the API can't resolve. The doc's
            # …/project/<id>/projectDetails link points at the MMP *platform*
            # (web UI), whose id space differs from the API's — so these (often
            # hidden / no-model / permission-scoped projects, e.g. 160/174)
            # can't be auto-translated. Don't pretend with fuzzy candidates;
            # ask the reviewer to pick the real project or leave it blank.
            _add_clarification(
                report, path="project.mmp_project_id",
                question=(f"The document gave the project id 「{given}」 from an MMP "
                          "platform link — the MMP platform (web) and the MMP API use "
                          "different id spaces, so this id can't be auto-resolved to a "
                          "binding (common for hidden / no-model / permission-scoped "
                          "projects). Please search and pick the real MMP project "
                          "manually, or leave it blank (this project may simply have "
                          "no MMP model monitoring)"),
                reason="mmp-platform-id-manual", kind="mmp_project",
                options=[],
            )
        elif given and given not in repo_names:
            _add_clarification(
                report, path="project.mmp_project_id",
                question=(f"The MMP project 「{given}」 from the document is not found "
                          "in the MMP directory; please confirm from the candidates "
                          "(may be left blank if there is no MMP binding)"),
                reason="mmp-project-not-found", kind="mmp_project",
                options=_mmp_opts(given, floor=_MATCH_FLOOR),
            )
        elif given:
            report.warnings.append(FieldIssue(
                field_path="project.mmp_project_id",
                message=f"Verified in MMP: project 「{given}」 exists",
            ))
            entry = next(p for p in mmp_projects if p["repo_name"] == given)
            _backfill_mmp_models(payload, report, entry)
        else:
            # Unprompted suggestion only when the directory has a plausible
            # name match — most projects have no MMP binding at all.
            options = _mmp_opts(project.name, floor=_UNPROMPTED_FLOOR)
            if options:
                _add_clarification(
                    report, path="project.mmp_project_id",
                    question=("The document gave no MMP project, but the MMP directory "
                              "has a similarly-named project; if this project has an "
                              "MMP binding please confirm the selection (otherwise ignore)"),
                    reason="mmp-project-suggested", kind="mmp_project",
                    options=options,
                )

    # The backfills above mutated the payload — cml_application_name from the
    # CML subdomain match, cml_job_name / schedule_cron / mmp_model_id from the
    # live platform, the MMP project binding, etc. But the blocking errors were
    # computed by validate_payload BEFORE any of that, so a field that is now
    # filled (e.g. a required cml_application_name) would still carry its stale
    # "required" error in the report — the form shows the value filled yet still
    # red. Recompute the blocking errors against the final payload; the
    # clarifications and warnings the enrichment added are kept (enrichment only
    # ever adds advisory items, never errors).
    from core.agent.validate import validate_payload

    report.errors = validate_payload(payload).errors
    return report
