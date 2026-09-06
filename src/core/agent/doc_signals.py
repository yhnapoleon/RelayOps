"""Deterministic anchors regex-derived from the raw handover text.

Confluence exports are noisy: the first lines are page chrome ("查看内联评论 /
收藏 / 分享", creator metadata), tables collapse into stacked lines, and the
project's real identifiers live inside URLs. The LLM is bad at exactly this
part — so the URLs are parsed here, by fixed conventions, before and after
the model runs:

  * prod-stat ``…/view/<name>``           → the CML project name (canonical)
  * MMP ``…/project/<id>/projectDetails`` → the MMP numeric project id
    (enrichment later swaps it for the MMP repo name the platform stores)
  * ``<subdomain>.ml-<hash>.…``           → a served application (Ray/API)
  * bitbucket ``…/repos/<repo>``          → code repo (hint only — the CML
    project usually shares the name, but prod-stat wins)
  * "handing over <X> project to the Ops Team" → the project display name

The signals feed the extraction prompt as a hint block AND override/fill the
extracted payload afterwards (:func:`apply_doc_signals`), so a noisy document
prefix can never decide the CML binding again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from core.agent.schemas import (
    AppDraft,
    JobDraft,
    OnboardingDraftPayload,
    ProductDraft,
)

# Subdomains on the CML workspace wildcard domain that are platform services,
# never user applications.
_PLATFORM_SUBDOMAIN_PREFIXES = ("prod-stat", "grafana", "runtime-mmp")

_PROD_STAT_RE = re.compile(
    r"https?://prod-stat[\w.-]*/view/([A-Za-z0-9][\w-]*)", re.IGNORECASE
)
_PROD_STAT_URL_RE = re.compile(
    r"https?://prod-stat[\w.-]*/view/[A-Za-z0-9][\w-]*", re.IGNORECASE
)
_MMP_ID_RE = re.compile(r"/project/(\d+)/projectDetails", re.IGNORECASE)
_BITBUCKET_RE = re.compile(
    r"https?://bitbucket[\w.-]*/projects/[\w-]+/repos/([A-Za-z0-9][\w-]*)",
    re.IGNORECASE,
)
# Workspace-served app: <subdomain>.ml-<hash>.<rest> — scheme optional because
# Confluence copies often drop it ("dynamic-inventory-scoring-prod.ml-….example.com").
_WORKSPACE_APP_RE = re.compile(
    r"(?:https?://)?([a-z0-9][a-z0-9-]*)\.(ml-[a-z0-9-]+\.[\w.-]+?)(/[^\s|，。）)]*)?(?=[\s|，。）)]|$)",
    re.IGNORECASE,
)
_HANDOVER_NAME_RE = re.compile(
    r"handing\s+over\s+(?:the\s+)?(.+?)\s+project\s+to\s+the\s+Ops\s+Team",
    re.IGNORECASE,
)

# Confluence page chrome that must never become a project name.
_NOISE_TOKENS = (
    "查看内联评论", "收藏(F)", "观看(W)", "分享(S)", "分析功能",
    "上次更新", "创建者", "分钟阅读", "Consolidated information",
)

# Projects that register an MMP project but explicitly track no models — the
# MMP link is registration only, so no scoring job should be conjured from it.
_NO_MMP_MODELS_RE = re.compile(
    r"does\s+not\s+include\s+traditional\s+ML\s+models"
    r"|no\s+MMP\s+model\s+tracking",
    re.IGNORECASE,
)


@dataclass
class AppLink:
    subdomain: str
    url: str
    looks_ray: bool = False


@dataclass
class DocSignals:
    cml_project_names: List[str] = field(default_factory=list)
    prod_stat_urls: List[str] = field(default_factory=list)
    mmp_project_ids: List[str] = field(default_factory=list)   # numeric, as strings
    bitbucket_repos: List[str] = field(default_factory=list)
    app_links: List[AppLink] = field(default_factory=list)
    handover_names: List[str] = field(default_factory=list)
    no_mmp_models: bool = False   # doc says it tracks no ML models

    @property
    def has_any(self) -> bool:
        return bool(
            self.cml_project_names or self.prod_stat_urls or self.mmp_project_ids
            or self.bitbucket_repos or self.app_links or self.handover_names
        )


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-./]+", "", (s or "").lower())


def _dedupe(values: List[str]) -> List[str]:
    return list(dict.fromkeys(v for v in values if v))


def _unique(values: List[str]) -> str:
    """The value, but only when the document is unambiguous about it."""
    distinct = _dedupe(values)
    return distinct[0] if len(distinct) == 1 else ""


def _first_label(url: str) -> str:
    m = re.match(r"(?:https?://)?([a-z0-9][a-z0-9-]*)\.", (url or "").strip(), re.IGNORECASE)
    return m.group(1) if m else ""


def looks_like_export_noise(name: str) -> bool:
    return any(token in name for token in _NOISE_TOKENS)


def derive_doc_signals(text: str) -> DocSignals:
    signals = DocSignals()
    signals.cml_project_names = _dedupe(_PROD_STAT_RE.findall(text))
    signals.prod_stat_urls = _dedupe(_PROD_STAT_URL_RE.findall(text))
    signals.mmp_project_ids = _dedupe(_MMP_ID_RE.findall(text))
    signals.bitbucket_repos = _dedupe(_BITBUCKET_RE.findall(text))
    signals.handover_names = _dedupe(
        " ".join(m.split()) for m in _HANDOVER_NAME_RE.findall(text)
    )
    signals.no_mmp_models = bool(_NO_MMP_MODELS_RE.search(text))

    seen_subs: set = set()
    for m in _WORKSPACE_APP_RE.finditer(text):
        sub, domain, path = m.group(1), m.group(2), m.group(3) or ""
        low = sub.lower()
        if any(low.startswith(p) for p in _PLATFORM_SUBDOMAIN_PREFIXES):
            continue
        if low in seen_subs:
            continue
        seen_subs.add(low)
        signals.app_links.append(AppLink(
            subdomain=sub,
            url=f"https://{sub}.{domain}{path}",
            looks_ray="dashboard" in path.lower(),
        ))
    return signals


def hints_block(signals: DocSignals) -> str:
    """The deterministic-anchors block appended to the extraction prompt."""
    if not signals.has_any:
        return ""
    lines: List[str] = []
    for n in signals.cml_project_names:
        lines.append(f"- CML project name (parsed from prod-stat /view/ link): {n}")
    for u in signals.prod_stat_urls:
        lines.append(f"- prod-stat URL: {u}")
    for i in signals.mmp_project_ids:
        lines.append(f"- MMP project numeric id (parsed from MMP link): {i}")
    for r in signals.bitbucket_repos:
        lines.append(f"- Bitbucket repo: {r}")
    for a in signals.app_links:
        suffix = " (URL has dashboard — likely a Ray app)" if a.looks_ray else ""
        lines.append(f"- Likely App: subdomain={a.subdomain} url={a.url}{suffix}")
    for h in signals.handover_names:
        lines.append(f"- Project name from the handover statement: {h}")
    return (
        "\nThe anchors below were parsed directly from the document by fixed URL "
        "rules — more reliable than free extraction, so prefer them when filling "
        "the matching fields:\n" + "\n".join(lines)
    )


def apply_doc_signals(payload: OnboardingDraftPayload, signals: DocSignals) -> OnboardingDraftPayload:
    """Post-extraction reconciliation: URL-derived values win over the LLM.

    Only unambiguous signals (a single distinct value in the document) are
    written; anything surprising is recorded in ``payload.warnings`` so the
    reviewer sees what was overridden and why.
    """
    project = payload.project
    notes: List[str] = []

    cml = _unique(signals.cml_project_names)
    if cml:
        given = project.cml_project_name.strip()
        if not given:
            project.cml_project_name = cml
            notes.append(f"CML project name backfilled from the prod-stat link: 「{cml}」")
        elif _norm(given) != _norm(cml):
            project.cml_project_name = cml
            notes.append(
                f"Extracted CML project 「{given}」 disagrees with the prod-stat "
                f"link's 「{cml}」; used the link-parsed value (the link is more reliable)"
            )

    prod_stat = _unique(signals.prod_stat_urls)
    if prod_stat and not project.prod_stat_url.strip():
        project.prod_stat_url = prod_stat
        notes.append("prod_stat_url backfilled from the document link")

    mmp_id = _unique(signals.mmp_project_ids)
    if mmp_id and not project.mmp_project_id.strip():
        project.mmp_project_id = mmp_id
        notes.append(
            f"MMP binding parsed from the MMP link as numeric id {mmp_id} "
            "(resolved to the MMP project name during review)"
        )

    display = signals.handover_names[0] if signals.handover_names else ""
    name = project.name.strip()
    if not name:
        fallback = display or cml or _unique(signals.bitbucket_repos)
        if fallback:
            project.name = fallback
            notes.append(
                f"Project name not stated explicitly in the document; backfilled "
                f"from a parsed anchor: 「{fallback}」")
    elif looks_like_export_noise(name) and (display or cml):
        project.name = display or cml
        notes.append(
            f"Extracted project name 「{name}」 looks like Confluence page chrome; "
            f"replaced with 「{project.name}」"
        )

    # A product with no assets is the most common failure mode — the LLM
    # narrates the project but drops the jobs/apps. The deterministic signals
    # (a served-app URL, an MMP project id) are strong evidence an asset
    # exists, so materialize them here rather than only warning. Each carries
    # the minimum the document gave; the CML/MMP enrichment step then offers
    # the reviewer the real jobs/apps to confirm the binding.

    # 1) Served-app links → App drafts (skip ones extraction already captured).
    known_subs = set()
    for prod in payload.products:
        for app in prod.apps:
            for cand in (app.cml_subdomain, _first_label(app.application_url),
                         _first_label(app.health_check_url)):
                if cand:
                    known_subs.add(_norm(cand))
    new_apps: List[AppDraft] = []
    for link in signals.app_links:
        if _norm(link.subdomain) in known_subs:
            continue
        new_apps.append(AppDraft(
            cml_project_name="",
            cml_application_name="",   # filled by CML enrichment (match by subdomain)
            cml_subdomain=link.subdomain,
            cml_app_type="ray" if link.looks_ray else "generic",
            application_url=link.url,
            source_quote=link.url,
        ))
        notes.append(
            f"Auto-created an App from the document's application link "
            f"(subdomain={link.subdomain}); the application name is backfilled "
            "during the CML check — please review"
        )

    # 2) MMP project id with no MMP-bound job anywhere → one stub MMP job, so
    #    the binding has a home (model + CML job get picked during enrichment).
    have_mmp_job = any(
        (j.mmp_project_id.strip() or j.mmp_model_id.strip())
        for prod in payload.products for j in prod.jobs
    )
    new_jobs: List[JobDraft] = []
    if mmp_id and not have_mmp_job and not signals.no_mmp_models:
        new_jobs.append(JobDraft(
            mmp_project_id=project.mmp_project_id or mmp_id,
            description="(auto placeholder) The document gave an MMP project but "
                        "did not list a specific scoring job — please pick the "
                        "matching MMP model and CML job during review",
            source_quote=f"MMP project id {mmp_id}",
        ))
        notes.append(
            "The document gave an MMP binding but no explicit scoring job; "
            "auto-created a placeholder MMP job — please complete its model and "
            "CML job during review"
        )

    if new_apps or new_jobs:
        target = _ensure_product(payload)
        target.apps.extend(new_apps)
        target.jobs.extend(new_jobs)

    payload.warnings.extend(notes)
    return payload


def _ensure_product(payload: OnboardingDraftPayload) -> ProductDraft:
    """The product that holds materialized assets: the first existing one, or
    a new product named after the project (the convention extraction uses)."""
    if payload.products:
        return payload.products[0]
    name = payload.project.name.strip() or payload.project.cml_project_name.strip() or "Default Product"
    product = ProductDraft(name=name)
    payload.products.append(product)
    return product


# Alert-table rows are SCENARIOS of the scoring job, not jobs — the LLM keeps
# turning them into fabricated jobs ("CML Resource Monitoring" from a "CML no
# resource" alert row). These phrases flag that misread when pruning.
_SCENARIO_ROW_PHRASES = (
    "no resource", "data pipeline failure", "data ingest failure",
    "must be up", "health check", "healthcheck", "drift",
)


def _appears_in_source(text_norm: str, value: str) -> bool:
    """True when ``value`` occurs in the document (ignoring case/separators).
    Short values are treated as absent — too collision-prone to trust."""
    nv = _norm(value)
    return len(nv) >= 5 and nv in text_norm


def drop_hallucinated_assets(payload: OnboardingDraftPayload, text: str) -> OnboardingDraftPayload:
    """Remove identifiers the model invented and assets that are pure
    hallucination, checked against the source text.

    Job/app *names* must be copied verbatim from the document — a name that
    doesn't occur in the source (e.g. an alert-row description rewritten into a
    job title) is cleared. A job/app left with no identifying signal at all is
    then dropped, with its description preserved in a warning so nothing is
    silently lost. The MMP/app stubs added by :func:`apply_doc_signals` carry a
    real signal (mmp_project_id / subdomain) and are never touched.
    """
    text_norm = _norm(text)
    notes: List[str] = []

    for prod in payload.products:
        kept_jobs: List[JobDraft] = []
        for job in prod.jobs:
            for attr, label in (("cml_job_name", "CML job"),
                                ("control_m_job_name", "Control-M")):
                val = getattr(job, attr).strip()
                if val and not _appears_in_source(text_norm, val):
                    setattr(job, attr, "")
                    notes.append(
                        f"{label} name 「{val}」 not found in the source — likely "
                        "AI-fabricated, cleared (please check the source and fill "
                        "manually or pick from the CML candidates)"
                    )
            has_signal = any((
                job.cml_job_name.strip(), job.control_m_job_name.strip(),
                job.mmp_model_id.strip(), job.mmp_project_id.strip(),
            ))
            if has_signal:
                kept_jobs.append(job)
            else:
                desc = (job.description or job.source_quote or "").strip()
                looks_scenario = any(p in desc.lower() for p in _SCENARIO_ROW_PHRASES)
                notes.append(
                    f"Removed a job with no identifier (description: {desc[:80] or 'empty'}) — "
                    + ("it is actually an alert/scenario row; please add it as a "
                       "scenario under the matching MMP/CML job"
                       if looks_scenario else "no CML/Control-M/MMP binding, cannot monitor")
                )
        prod.jobs = kept_jobs

        kept_apps: List[AppDraft] = []
        for app in prod.apps:
            val = app.cml_application_name.strip()
            if val and not _appears_in_source(text_norm, val):
                app.cml_application_name = ""
                notes.append(
                    f"App name 「{val}」 not found in the source — likely "
                    "AI-fabricated, cleared (backfilled by subdomain during the "
                    "CML check, or fill manually)"
                )
            if any((app.cml_application_name.strip(), app.cml_subdomain.strip(),
                    app.application_url.strip())):
                kept_apps.append(app)
            else:
                notes.append(
                    f"Removed an App with no identifier (description: {(app.description or '').strip()[:80] or 'empty'})"
                )
        prod.apps = kept_apps

    payload.warnings.extend(notes)
    return payload
