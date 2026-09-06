"""Onboarding agent — pure-logic tests (ingest / validate / answer write-back).

DB- and LLM-dependent paths (draft service, submit pipeline, extraction) are
exercised in the deployed environment; here we pin down the deterministic
parts the whole flow leans on.
"""

import pytest

from core.agent.ingest import UnsupportedFormatError, extract_text
from core.agent.schemas import (
    AppDraft,
    ClarificationAnswer,
    JobDraft,
    JobScenarioDraft,
    OnboardingDraftPayload,
    ProductDraft,
    ProjectDraft,
)
from core.agent.validate import apply_answer, apply_answers, validate_payload


# ── ingest ───────────────────────────────────────────────────────────

def test_html_tables_and_links_survive():
    html = b"""
    <html><head><style>x{}</style></head><body>
      <h1>Handover</h1>
      <p>Dashboard: <a href="https://example.com/dash">Prod Stat</a></p>
      <table>
        <tr><th>Job</th><th>Cron</th></tr>
        <tr><td>ns_batch_sg</td><td>0 2 * * 6</td></tr>
      </table>
    </body></html>
    """
    text = extract_text("handover.html", html)
    assert "Prod Stat (https://example.com/dash)" in text
    assert "Job | Cron" in text
    assert "ns_batch_sg | 0 2 * * 6" in text
    assert "style" not in text


def test_plain_text_passthrough_and_unsupported():
    assert extract_text("notes.md", "# title\nbody".encode("utf-8")) == "# title\nbody"
    # GBK fallback
    assert "联系人" in extract_text("a.txt", "联系人".encode("gbk"))
    # CSV/TSV are plain text — no parser/extra dependency, passed through verbatim
    assert extract_text("jobs.csv", b"name,cron\nns_batch,0 2 * * 6") == "name,cron\nns_batch,0 2 * * 6"
    assert extract_text("jobs.tsv", b"name\tcron").startswith("name\tcron")
    with pytest.raises(UnsupportedFormatError):
        extract_text("scan.pdf", b"%PDF-")


# ── validate: errors / clarifications ────────────────────────────────

def _payload() -> OnboardingDraftPayload:
    return OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(
            name="Inventory Scoring",
            jobs=[JobDraft(cml_job_name="ns_batch_sg", schedule_cron="0 2 * * 6",
                           control_m_job_name="NS_SG_001", owner_contact="poc@example.com")],
            apps=[AppDraft(cml_application_name="ns-api", application_url="https://x",
                           owner_contact="poc@example.com")],
        )],
    )


def test_happy_payload_has_no_errors():
    report = validate_payload(_payload())
    assert report.errors == []
    assert report.ok_to_submit


def test_blocking_errors():
    p = _payload()
    p.project.name = ""
    p.products[0].jobs[0].schedule_cron = "every saturday"
    p.products[0].jobs[0].scenarios = [JobScenarioDraft(scenario_type="nonsense")]
    p.products[0].apps[0].cml_app_type = "flask"
    report = validate_payload(p)
    paths = {e.field_path for e in report.errors}
    assert "project.name" in paths
    assert "products[0].jobs[0].schedule_cron" in paths
    assert "products[0].jobs[0].scenarios[0].scenario_type" in paths
    assert "products[0].apps[0].cml_app_type" in paths
    assert not report.ok_to_submit


def test_job_requires_some_identity():
    p = _payload()
    p.products[0].jobs[0].cml_job_name = ""
    p.products[0].jobs[0].control_m_job_name = ""
    p.products[0].jobs[0].mmp_model_id = ""
    report = validate_payload(p)
    assert any(e.field_path == "products[0].jobs[0]" for e in report.errors)


def test_clarifications_for_missing_key_fields():
    p = _payload()
    p.products[0].apps[0].application_url = ""    # the "link name without URL" case
    p.products[0].jobs[0].owner_contact = ""
    p.products[0].jobs[0].control_m_job_name = ""
    report = validate_payload(p)
    asked = {c.field_path for c in report.clarifications}
    assert "products[0].apps[0].application_url" in asked
    assert "products[0].jobs[0].owner_contact" in asked
    assert "products[0].jobs[0].control_m_job_name" in asked
    # clarifications never block submit by themselves
    assert report.ok_to_submit


def test_duplicate_job_names_warn():
    p = _payload()
    p.products[0].jobs.append(JobDraft(cml_job_name="NS_BATCH_SG", owner_contact="x",
                                       control_m_job_name="NS_SG_002"))
    report = validate_payload(p)
    assert any("duplicates" in w.message for w in report.warnings)


# ── deterministic answer write-back ──────────────────────────────────

def test_apply_answer_sets_exactly_one_field():
    p = _payload()
    p.products[0].apps[0].application_url = ""
    apply_answer(p, ClarificationAnswer(
        field_path="products[0].apps[0].application_url",
        answer="  https://ns-api.apps.demo.example.com  ",
    ))
    assert p.products[0].apps[0].application_url == "https://ns-api.apps.demo.example.com"
    # nothing else moved
    assert p.products[0].apps[0].cml_application_name == "ns-api"


def test_apply_answer_rejects_bad_paths():
    p = _payload()
    with pytest.raises(ValueError):
        apply_answer(p, ClarificationAnswer(field_path="products[0].jobs", answer="x"))
    with pytest.raises((ValueError, AttributeError, IndexError)):
        apply_answer(p, ClarificationAnswer(field_path="products[9].jobs[0].owner_contact", answer="x"))


def test_apply_answers_skips_blank_and_clears_clarification_on_revalidate():
    p = _payload()
    p.products[0].jobs[0].owner_contact = ""
    before = validate_payload(p)
    assert any(c.field_path == "products[0].jobs[0].owner_contact" for c in before.clarifications)

    apply_answers(p, [
        ClarificationAnswer(field_path="products[0].jobs[0].owner_contact", answer="lead@example.com"),
        ClarificationAnswer(field_path="products[0].jobs[0].description", answer="   "),  # blank → skipped
    ])
    after = validate_payload(p)
    assert not any(c.field_path == "products[0].jobs[0].owner_contact" for c in after.clarifications)


# ── payload JSON roundtrip (storage shape) ───────────────────────────

def test_payload_roundtrips_through_json_dict():
    p = _payload()
    restored = OnboardingDraftPayload.model_validate(p.model_dump())
    assert restored == p


# ── CML/MMP enrichment (network fetchers stubbed) ────────────────────

from core.agent import onboarding_enrich as enrich  # noqa: E402


def _enriched(payload, *, cml=None, mmp=None, assets=None, monkeypatch=None):
    monkeypatch.setattr(enrich, "fetch_cml_project_names", lambda extra_queries=(): cml)
    monkeypatch.setattr(enrich, "fetch_mmp_projects", lambda: mmp)
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", lambda name: assets)
    report = validate_payload(payload)
    return enrich.enrich_validation_report(payload, report)


def test_missing_cml_name_gets_dropdown_with_candidates(monkeypatch):
    p = _payload()
    p.project.cml_project_name = ""
    report = _enriched(p, cml=["material-classifier", "inventory-scoring-model", "forecast"],
                       mmp=None, monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "project.cml_project_name")
    assert clar.kind == "cml_project"
    # Candidates matched against the document's project name ("Inventory Scoring").
    assert any(o.value == "inventory-scoring-model" for o in clar.options)


def test_unmatched_cml_name_asks_for_confirmation(monkeypatch):
    p = _payload()  # cml_project_name = "material-classifier"
    report = _enriched(p, cml=["material-classifier-job", "forecast"],
                       mmp=None, monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "project.cml_project_name")
    assert clar.reason == "cml-project-not-found"
    assert any(o.value == "material-classifier-job" for o in clar.options)


def test_cml_name_corrected_on_separator_or_case_diff(monkeypatch):
    # The doc says "northstar-forecast"; CML actually calls it "northstar_forecast" — a single
    # separator difference must NOT become a "not found" question. It should
    # auto-correct to the CML spelling so submit-time resolution works.
    p = _payload()
    p.project.cml_project_name = "northstar-forecast"
    report = _enriched(p, cml=["northstar_forecast", "other-proj"], mmp=None, monkeypatch=monkeypatch)
    assert p.project.cml_project_name == "northstar_forecast"          # rewritten to CML spelling
    assert not any(c.field_path == "project.cml_project_name" for c in report.clarifications)
    assert any("corrected" in w.message for w in report.warnings)


def test_exact_cml_match_adds_verified_warning(monkeypatch):
    p = _payload()
    report = _enriched(p, cml=["material-classifier"], mmp=None, monkeypatch=monkeypatch)
    assert not any(c.field_path == "project.cml_project_name" for c in report.clarifications)
    assert any("Verified in CML" in w.message for w in report.warnings)


def test_job_level_override_checked_against_cml(monkeypatch):
    p = _payload()
    p.products[0].jobs[0].cml_project_name = "ghost-project"
    report = _enriched(p, cml=["material-classifier"], mmp=None, monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "products[0].jobs[0].cml_project_name")
    assert clar.kind == "cml_project"


def test_mmp_suggested_only_on_plausible_match(monkeypatch):
    p = _payload()  # project name "Inventory Scoring", no mmp_project_id
    mmp = [{"repo_name": "demo-material-classifier",
            "business_name": "Inventory Scoring", "model_count": 3},
           {"repo_name": "totally-unrelated", "business_name": "Wealth", "model_count": 1}]
    report = _enriched(p, cml=None, mmp=mmp, monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications if c.field_path == "project.mmp_project_id")
    assert clar.kind == "mmp_project"
    values = [o.value for o in clar.options]
    assert "demo-material-classifier" in values
    assert "totally-unrelated" not in values


def test_resolve_cml_binding_llm_matches_within_pool(monkeypatch):
    # "Inventory Scoring" ↔ "material-classifier" share no characters — difflib
    # can't bridge it; the LLM picks from the fetched candidate pool.
    monkeypatch.setattr(enrich, "fetch_cml_project_names",
                        lambda extra_queries=(): ["material-classifier", "forecast", "gwb-rm"])
    monkeypatch.setattr(enrich, "_llm_pick_cml",
                        lambda text, name, cands: "material-classifier")
    p = OnboardingDraftPayload(project=ProjectDraft(name="Inventory Scoring"))
    enrich.resolve_cml_binding(p, "repos/material-classifier ... inventory scoring model")
    assert p.project.cml_project_name == "material-classifier"
    assert any("matched by AI" in w for w in p.warnings)


def test_resolve_cml_binding_keeps_deterministic_and_skips_llm(monkeypatch):
    # A binding already parsed from the prod-stat URL is ground truth: keep it
    # (only normalize spelling to the CML list), never consult the LLM.
    calls = {"llm": 0}

    def _spy(*a):
        calls["llm"] += 1
        return "WRONG"

    monkeypatch.setattr(enrich, "fetch_cml_project_names",
                        lambda extra_queries=(): ["northstar_forecast", "other"])
    monkeypatch.setattr(enrich, "_llm_pick_cml", _spy)
    p = OnboardingDraftPayload(project=ProjectDraft(name="NORTHSTAR FORECAST", cml_project_name="northstar-forecast"))
    enrich.resolve_cml_binding(p, "doc")
    assert p.project.cml_project_name == "northstar_forecast"   # normalized to CML spelling
    assert calls["llm"] == 0                             # LLM not consulted


def test_resolve_cml_assets_llm_binds_unnamed_job(monkeypatch):
    # An MMP stub job (no CML/Control-M name) is LLM-matched to the real
    # scoring job in the bound project — difflib couldn't, since the doc has
    # no job name to compare. Schedule is backfilled from the chosen job.
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", lambda name: {
        "jobs": [{"name": "northstar_forecast_score_batch", "schedule": "0 2 * * 1"},
                 {"name": "northstar_forecast_train", "schedule": ""}],
        "apps": [],
    })
    monkeypatch.setattr(enrich, "_llm_match_jobs",
                        lambda text, ctx, jobs: ["northstar_forecast_score_batch"])
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NORTHSTAR FORECAST", cml_project_name="northstar-forecast"),
        products=[ProductDraft(name="NORTHSTAR FORECAST", jobs=[
            JobDraft(mmp_project_id="demo-northstar-forecast", mmp_model_id="forecast_scoring")])],
    )
    enrich.resolve_cml_assets(p, "daily scoring run for northstar forecast")
    job = p.products[0].jobs[0]
    assert job.cml_job_name == "northstar_forecast_score_batch"
    assert job.schedule_cron == "0 2 * * 1"
    assert any("matched by AI" in w for w in p.warnings)


def test_resolve_cml_assets_leaves_named_jobs_untouched(monkeypatch):
    # A job that already carries a name is not a target — the LLM isn't called.
    monkeypatch.setattr(enrich, "fetch_cml_project_assets",
                        lambda name: {"jobs": [{"name": "real_job", "schedule": ""}], "apps": []})
    monkeypatch.setattr(enrich, "_llm_match_jobs",
                        lambda *a: (_ for _ in ()).throw(AssertionError("LLM should not run")))
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="X", cml_project_name="northstar-forecast"),
        products=[ProductDraft(name="X", jobs=[JobDraft(cml_job_name="given_name")])],
    )
    enrich.resolve_cml_assets(p, "doc")
    assert p.products[0].jobs[0].cml_job_name == "given_name"


def test_resolve_cml_assets_llm_pick_constrained_to_real_jobs(monkeypatch):
    # A pick that isn't a real job is dropped (no binding invented).
    monkeypatch.setattr(enrich, "fetch_cml_project_assets",
                        lambda name: {"jobs": [{"name": "real_a", "schedule": ""}], "apps": []})
    monkeypatch.setattr(enrich, "_llm_match_jobs", lambda text, ctx, jobs: [""])
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="X", cml_project_name="northstar-forecast"),
        products=[ProductDraft(name="X", jobs=[JobDraft(mmp_model_id="m1")])],
    )
    enrich.resolve_cml_assets(p, "doc")
    assert p.products[0].jobs[0].cml_job_name == ""   # left for clarification


def test_resolve_cml_binding_noop_when_cml_unreachable(monkeypatch):
    monkeypatch.setattr(enrich, "fetch_cml_project_names", lambda extra_queries=(): None)
    monkeypatch.setattr(enrich, "_llm_pick_cml",
                        lambda *a: (_ for _ in ()).throw(AssertionError("should not be called")))
    p = OnboardingDraftPayload(project=ProjectDraft(name="X"))
    enrich.resolve_cml_binding(p, "doc")
    assert p.project.cml_project_name == ""             # left for clarification


def test_resolve_cml_binding_llm_pick_must_be_in_pool(monkeypatch):
    # A pick the model invented (not in the offered list) is discarded.
    monkeypatch.setattr(enrich, "fetch_cml_project_names",
                        lambda extra_queries=(): ["alpha", "beta"])
    monkeypatch.setattr(enrich, "_llm_pick_cml",
                        lambda text, name, cands: None)  # simulates constrained-empty
    p = OnboardingDraftPayload(project=ProjectDraft(name="Gamma"))
    enrich.resolve_cml_binding(p, "doc")
    assert p.project.cml_project_name == ""


def test_platforms_unreachable_still_sets_combobox_kind(monkeypatch):
    p = _payload()
    p.project.cml_project_name = ""
    report = _enriched(p, cml=None, mmp=None, monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "project.cml_project_name")
    assert clar.kind == "cml_project"
    assert clar.options == []


def test_required_error_cleared_after_app_name_backfill(monkeypatch):
    # The reported bug: cml_application_name is required and empty, but enrich
    # backfills it from the CML subdomain match. The "required" ERROR (computed
    # before the backfill) must be recomputed away — the field is filled, so it
    # must not stay red.
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="SALES Copilot V2", cml_project_name="support-assistant-v2"),
        products=[ProductDraft(name="SALES Copilot V2", apps=[
            AppDraft(cml_application_name="", cml_subdomain="support-assistant-v2-prod",
                     application_url="https://support-assistant-v2-prod.ml-x.com/dashboard/#/overview",
                     owner_contact="x@example.com")])],
    )
    apath = "products[0].apps[0].cml_application_name"
    # Pre-enrich the offline validator flags it as a blocking error.
    assert any(e.field_path == apath for e in validate_payload(p).errors)
    report = _enriched(
        p, cml=["support-assistant-v2"], mmp=None,
        assets={"jobs": [], "apps": [
            {"name": "SALES Copilot v2", "subdomain": "support-assistant-v2-prod"}]},
        monkeypatch=monkeypatch)
    assert p.products[0].apps[0].cml_application_name == "SALES Copilot v2"   # backfilled
    assert not any(e.field_path == apath for e in report.errors)           # error cleared


# ── cross-project asset reconciliation (jobs/apps split across CML projects) ─


def _assets_by_project(mapping):
    """A fetch_cml_project_assets stub: returns each project's assets, or None
    when the project name isn't in the map (CML can't see it)."""
    return lambda name: mapping.get(name)


def test_reconcile_rehomes_job_to_other_documented_project(monkeypatch):
    # The doc names two CML projects; the deploy job actually lives in the
    # second one, not the primary binding. Reconciliation re-homes it (writes
    # the per-job cml_project_name override) and backfills its real schedule.
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", _assets_by_project({
        "material-classifier": {
            "jobs": [{"name": "ns_batch_sg", "schedule": "0 2 * * 6"}], "apps": []},
        "inventory-scoring-model": {
            "jobs": [{"name": "Inventory_Scoring_Model_Deploy", "schedule": "0 3 2 * *"}],
            "apps": []},
    }))
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(cml_job_name="ns_batch_sg"),
            JobDraft(cml_job_name="Inventory_Scoring_Model_Deploy"),
        ])],
    )
    report = validate_payload(p)
    enrich.reconcile_cross_project_assets(
        p, report, documented_projects=["material-classifier", "inventory-scoring-model"])

    deploy = p.products[0].jobs[1]
    assert deploy.cml_project_name == "inventory-scoring-model"   # re-homed
    assert deploy.schedule_cron == "0 3 2 * *"                 # schedule backfilled
    assert p.products[0].jobs[0].cml_project_name == ""        # the matched one untouched
    assert any("re-homed to 「inventory-scoring-model」" in w.message for w in report.warnings)
    # The split summary names both projects.
    assert any("belong to 2 CML projects" in w.message for w in report.warnings)


def test_reconcile_asks_when_job_name_is_ambiguous(monkeypatch):
    # The same job name exists in BOTH documented projects → don't guess, ask.
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", _assets_by_project({
        "proj-a": {"jobs": [{"name": "shared_job", "schedule": ""}], "apps": []},
        "proj-b": {"jobs": [{"name": "shared_job", "schedule": ""}], "apps": []},
    }))
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="X", cml_project_name="proj-a"),
        # bound to proj-a, but documented projects include a sibling that also
        # has it — force the not-in-bound branch by binding the job to neither.
        products=[ProductDraft(name="X", jobs=[
            JobDraft(cml_job_name="shared_job", cml_project_name="ghost")])],
    )
    report = validate_payload(p)
    enrich.reconcile_cross_project_assets(
        p, report, documented_projects=["proj-a", "proj-b"])
    clar = next(c for c in report.clarifications
                if c.field_path == "products[0].jobs[0].cml_project_name")
    assert clar.reason == "cml-job-cross-project"
    assert {o.value for o in clar.options} == {"proj-a", "proj-b"}


def test_reconcile_noop_with_single_project(monkeypatch):
    # Only one CML project in play → reconciliation adds nothing (the
    # single-project backfill already owns this case).
    called = {"n": 0}

    def _spy(name):
        called["n"] += 1
        return {"jobs": [{"name": "ns_batch_sg", "schedule": ""}], "apps": []}

    monkeypatch.setattr(enrich, "fetch_cml_project_assets", _spy)
    p = _payload()
    report = validate_payload(p)
    enrich.reconcile_cross_project_assets(
        p, report, documented_projects=["material-classifier"])
    assert called["n"] == 0                       # never even fetched
    assert not any("belong to" in w.message for w in report.warnings)


def test_reconcile_keeps_correctly_bound_job(monkeypatch):
    # Two documented projects, but the job really is in its bound project — no
    # re-home, no split warning (only one project actually hosts an asset).
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", _assets_by_project({
        "material-classifier": {
            "jobs": [{"name": "ns_batch_sg", "schedule": "0 2 * * 6"}], "apps": []},
        "inventory-scoring-model": {"jobs": [], "apps": []},
    }))
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(cml_job_name="ns_batch_sg")])],
    )
    report = validate_payload(p)
    enrich.reconcile_cross_project_assets(
        p, report, documented_projects=["material-classifier", "inventory-scoring-model"])
    assert p.products[0].jobs[0].cml_project_name == ""   # untouched
    assert not any("belong to" in w.message for w in report.warnings)


# ── cross-project split: clarification card + payload partition ──────


def test_reconcile_emits_project_split_clarification(monkeypatch):
    # Assets partition across two CML projects → an actionable split card
    # (kind=project_split at the synthetic __split__ path) lists both projects.
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", _assets_by_project({
        "material-classifier": {"jobs": [{"name": "ns_batch_sg", "schedule": ""}], "apps": []},
        "inventory-scoring-model": {
            "jobs": [{"name": "Inventory_Scoring_Model_Deploy", "schedule": ""}], "apps": []},
    }))
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(cml_job_name="ns_batch_sg"),
            JobDraft(cml_job_name="Inventory_Scoring_Model_Deploy"),
        ])],
    )
    report = validate_payload(p)
    enrich.reconcile_cross_project_assets(
        p, report, documented_projects=["material-classifier", "inventory-scoring-model"])
    split = next(c for c in report.clarifications if c.field_path == "__split__")
    assert split.kind == "project_split"
    assert split.reason == "cross-project-split"
    assert {o.value for o in split.options} == {"material-classifier", "inventory-scoring-model"}


def test_partition_payload_by_cml_project_splits_assets():
    # Project bound to A; one job stays in A, one job + one app override to B.
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier",
                             prod_stat_url="https://ps/view/material-classifier",
                             mmp_project_id="224"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(cml_job_name="ns_batch_sg"),
            JobDraft(cml_job_name="Inventory_Scoring_Model_Deploy",
                     cml_project_name="inventory-scoring-model"),
        ], apps=[
            AppDraft(cml_application_name="ns-api", cml_project_name="inventory-scoring-model"),
        ])],
    )
    parts = enrich.partition_payload_by_cml_project(p)
    assert [name for name, _ in parts] == ["material-classifier", "inventory-scoring-model"]

    origin = dict(parts)["material-classifier"]
    other = dict(parts)["inventory-scoring-model"]
    # Origin keeps its project meta and only its own job.
    assert origin.project.prod_stat_url and origin.project.mmp_project_id == "224"
    assert [j.cml_job_name for j in origin.products[0].jobs] == ["ns_batch_sg"]
    assert origin.products[0].apps == []
    # The other project: only its assets, project-level CML/MMP specifics cleared,
    # per-asset overrides dropped (now redundant with the new binding).
    assert other.project.cml_project_name == "inventory-scoring-model"
    assert other.project.prod_stat_url == "" and other.project.mmp_project_id == ""
    assert [j.cml_job_name for j in other.products[0].jobs] == ["Inventory_Scoring_Model_Deploy"]
    assert other.products[0].jobs[0].cml_project_name == ""
    assert [a.cml_application_name for a in other.products[0].apps] == ["ns-api"]


def test_partition_single_project_is_not_splittable():
    parts = enrich.partition_payload_by_cml_project(_payload())
    assert len(parts) == 1   # caller treats <2 as "nothing to split"


def test_apply_answers_skips_synthetic_split_path():
    # The split action card is not a real field — answering it is a no-op,
    # never an AttributeError on a non-existent payload attribute.
    p = _payload()
    before = p.model_dump()
    apply_answers(p, [ClarificationAnswer(
        id="q::__split__", field_path="__split__", answer="inventory-scoring-model")])
    assert p.model_dump() == before


def test_resolve_cml_assets_clears_project_name_used_as_job_name(monkeypatch):
    # The bug from test4.html: every MMP-only job had cml_job_name set to the
    # CML *project* name. The cleanup clears it so the LLM matcher can bind it.
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", lambda name: {
        "jobs": [{"name": "Inventory_Scoring_Model_Demo", "schedule": "0 0 1 * *"}],
        "apps": [],
    })
    monkeypatch.setattr(enrich, "_llm_match_jobs",
                        lambda text, ctx, cml_jobs: ["Inventory_Scoring_Model_Demo"] * len(ctx))
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(cml_job_name="material-classifier", mmp_model_id="345")])],
    )
    enrich.resolve_cml_assets(p, text="…document…")
    # The bogus project-as-job name is gone; the matcher bound the real job.
    assert p.products[0].jobs[0].cml_job_name == "Inventory_Scoring_Model_Demo"
    assert p.products[0].jobs[0].schedule_cron == "0 0 1 * *"


# ── MMP multi-track binding resolver (URL / id / name) ───────────────

_NS_MMP_DIR = [
    {"repo_name": "demo-material-classifier@batch-inventory-scoring", "id": 224,
     "business_name": "Inventory Scoring FP Reduction", "model_count": 3,
     "models": [{"id": 345, "name": "inventory-scoring-model-sg-relayops-batch", "is_production": True},
                {"id": 346, "name": "inventory-scoring-model-sg-bos-batch", "is_production": True},
                {"id": 347, "name": "inventory-scoring-model-sg-northstar-batch", "is_production": True}]},
    {"repo_name": "demo-material-classifier@batch-inventory-scoring-ge", "id": 226,
     "business_name": "Inventory Scoring FP Reduction - GE", "model_count": 1,
     "models": [{"id": 344, "name": "inventory-scoring-model-sg-ge-batch", "is_production": True}]},
    {"repo_name": "demo-material-classifier@dynamic-inventory-scoring", "id": 225,
     "business_name": "Dynamic Inventory Scoring - CFS", "model_count": 1,
     "models": [{"id": 348, "name": "inventory-scoring-model-sg-rome-api", "is_production": True}]},
    {"repo_name": "order-capture@capture", "id": 99,
     "business_name": "GM Capture", "model_count": 0, "models": []},
]


def _resolve_ns(raw, *fallbacks):
    projects = [dict(p) for p in _NS_MMP_DIR]
    by_id = {int(p["id"]): p["repo_name"] for p in projects}
    repo_names = {p["repo_name"] for p in projects}
    return enrich.resolve_mmp_project(
        raw, tuple(fallbacks), projects=projects, by_id=by_id, repo_names=repo_names)


def test_mmp_url_track_disambiguates_entity_by_id():
    # The doc's link is /project/224/model/345/runs — the id picks exactly the
    # batch-inventory-scoring entity, which a bare name never could.
    m = _resolve_ns("https://runtime-mmp-web/project/224/model/345/runs")
    assert m.track == "url"
    assert m.repo_name == "demo-material-classifier@batch-inventory-scoring"
    assert m.numeric_id == 224


def test_mmp_bare_id_track():
    m = _resolve_ns("225")
    assert m.track == "id"
    assert m.repo_name == "demo-material-classifier@dynamic-inventory-scoring"


def test_mmp_name_exact_track():
    m = _resolve_ns("order-capture@capture")
    assert m.track == "name-exact"
    assert m.repo_name == "order-capture@capture"


def test_mmp_name_fuzzy_binds_unique_stem_match():
    # "gm capture" has no id, but the repo stem before '@' is a clear unique
    # winner → fuzzy track binds it.
    m = _resolve_ns("gm capture")
    assert m.track == "name-fuzzy"
    assert m.repo_name == "order-capture@capture"


def test_mmp_name_ambiguous_when_multiple_entities_share_stem():
    # A bare CML name maps to three @entity siblings under one repo stem —
    # the resolver must NOT guess; it returns ambiguous with the candidates.
    m = _resolve_ns("material-classifier")
    assert m.ambiguous is True
    assert not m.repo_name
    # The three @entity siblings under the shared stem are all offered.
    siblings = {c for c in m.candidates if c.startswith("demo-material-classifier@")}
    assert len(siblings) == 3


def test_mmp_url_id_falls_back_to_direct_lookup(monkeypatch):
    # An id not on the shallow page is fetched directly and folded into the
    # directory (so a large workspace can't make a real binding look missing).
    monkeypatch.setattr(enrich, "fetch_mmp_project_by_id", lambda n: {
        "repo_name": "off-page@e", "id": n, "business_name": "Off Page",
        "model_count": 0, "models": []})
    m = _resolve_ns("https://x/project/9999/projectDetails")
    assert m.track == "url"
    assert m.repo_name == "off-page@e"


def test_enrich_asks_on_ambiguous_mmp_name(monkeypatch):
    # End-to-end through enrichment: a name-only MMP binding that hits several
    # entities becomes a clarification, never a silent bind.
    p = _payload()
    p.project.mmp_project_id = "material-classifier"
    report = _enriched(p, cml=None, mmp=[dict(x) for x in _NS_MMP_DIR],
                       monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "project.mmp_project_id")
    assert clar.reason == "mmp-project-ambiguous"
    assert p.project.mmp_project_id == "material-classifier"   # untouched, asked


def test_backfill_resolves_model_by_url_id(monkeypatch):
    # A job whose mmp_model_id is the /model/<id> link (or bare id) is bound to
    # the model NAME via the id index — no fuzzy matching, exact resolution.
    p = OnboardingDraftPayload(
        project=ProjectDraft(
            name="Inventory Scoring",
            cml_project_name="material-classifier",
            mmp_project_id="demo-material-classifier@batch-inventory-scoring"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(cml_job_name="ns_sg",
                     mmp_project_id="demo-material-classifier@batch-inventory-scoring",
                     mmp_model_id="https://runtime-mmp-web/project/224/model/345/runs")])],
    )
    report = _enriched(p, cml=None, mmp=[dict(x) for x in _NS_MMP_DIR],
                       monkeypatch=monkeypatch)
    assert p.products[0].jobs[0].mmp_model_id == "inventory-scoring-model-sg-relayops-batch"
    assert any("Resolved MMP model id" in w.message for w in report.warnings)


# ── direct by-id lookup: response-shape normalization ────────────────


def test_mmp_by_id_uses_precomputed_repo_name():
    # List-style shape: the combined project_repo_name is taken as-is.
    entry = enrich._mmp_entry_from_project({
        "id": 224, "project_repo_name": "demo-material-classifier@batch-inventory-scoring",
        "business_understanding_project_name": "Inventory Scoring FP Reduction",
        "models": [{"id": 345, "model_name": "m-sg-relayops", "is_production": True}],
    })
    assert entry["repo_name"] == "demo-material-classifier@batch-inventory-scoring"
    assert entry["models"][0]["id"] == 345


def test_mmp_by_id_builds_name_at_entity_when_combined_absent():
    # Single-project shape (the hidden-id path, e.g. 174): only project_name +
    # entity are present → the binding must be built as name@entity, NOT the
    # bare repo_name (which would drop the entity and bind the wrong sibling).
    entry = enrich._mmp_entry_from_project({
        "id": 174, "project_name": "order-capture", "repo_name": "order-capture",
        "entity": "capture-prod",
        "business_understanding_project_name": "GM Capture",
        "models": [],
    })
    assert entry["repo_name"] == "order-capture@capture-prod"
    assert entry["id"] == 174


def test_mmp_by_id_wrapped_under_project_key():
    entry = enrich._mmp_entry_from_project({"project": {
        "id": 160, "project_name": "northstar-forecast", "entity": "forecast",
        "models": [{"id": 1, "model_name": "scoring", "is_production": True}],
    }})
    assert entry["repo_name"] == "northstar-forecast@forecast"


def test_mmp_by_id_none_when_no_name():
    assert enrich._mmp_entry_from_project({"id": 9, "models": []}) is None


# ── MMP LLM fallback (difflib is not trusted blindly) ────────────────


def test_resolve_mmp_binding_keeps_deterministic_and_skips_llm(monkeypatch):
    # A URL-id binding is ground truth: resolved by the deterministic track,
    # the LLM is never consulted.
    monkeypatch.setattr(enrich, "fetch_mmp_projects", lambda: [dict(x) for x in _NS_MMP_DIR])
    monkeypatch.setattr(enrich, "_llm_pick_mmp",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not run")))
    p = _payload()
    p.project.mmp_project_id = "https://runtime-mmp-web/project/224/model/345/runs"
    enrich.resolve_mmp_binding(p, "inventory scoring batch")
    assert p.project.mmp_project_id == "demo-material-classifier@batch-inventory-scoring"


def test_resolve_mmp_binding_llm_disambiguates_entity_siblings(monkeypatch):
    # A bare name maps to three @entity siblings — difflib can't choose, so the
    # LLM picks from those exact candidates (here: the DNS/dynamic one).
    monkeypatch.setattr(enrich, "fetch_mmp_projects", lambda: [dict(x) for x in _NS_MMP_DIR])
    seen = {}

    def _fake_pick(text, ctx, candidates, *, projects):
        seen["candidates"] = candidates
        return "demo-material-classifier@dynamic-inventory-scoring"

    monkeypatch.setattr(enrich, "_llm_pick_mmp", _fake_pick)
    p = _payload()
    p.project.mmp_project_id = "material-classifier"
    enrich.resolve_mmp_binding(p, "This is the Dynamic Inventory Scoring (DNS) handover")
    assert p.project.mmp_project_id == "demo-material-classifier@dynamic-inventory-scoring"
    # The LLM was offered the real @entity siblings as candidates (constrained
    # to actual repos — never free text), so it could disambiguate on meaning.
    siblings = {c for c in seen["candidates"] if c.startswith("demo-material-classifier@")}
    assert len(siblings) == 3
    assert any("LLM fallback" in w for w in p.warnings)


def test_resolve_mmp_binding_llm_abstain_leaves_field_for_clarification(monkeypatch):
    # LLM returns None (not confident) → the bare name is left untouched so the
    # enrich layer raises the ambiguity clarification.
    monkeypatch.setattr(enrich, "fetch_mmp_projects", lambda: [dict(x) for x in _NS_MMP_DIR])
    monkeypatch.setattr(enrich, "_llm_pick_mmp", lambda *a, **k: None)
    p = _payload()
    p.project.mmp_project_id = "material-classifier"
    enrich.resolve_mmp_binding(p, "doc")
    assert p.project.mmp_project_id == "material-classifier"   # untouched


def test_llm_pick_mmp_constrained_to_offered_candidates(monkeypatch):
    # A pick the model invents (not in the candidate list) is discarded.
    import core.agent.llm as llm_mod

    class _Chat:
        def with_structured_output(self, _model):
            return self
        def invoke(self, _messages):
            return {"project_repo_name": "totally-made-up@x"}

    monkeypatch.setattr(llm_mod, "get_chat_model", lambda **k: _Chat())
    pick = enrich._llm_pick_mmp(
        "doc", "ctx",
        ["demo-material-classifier@batch-inventory-scoring"],
        projects=[dict(x) for x in _NS_MMP_DIR])
    assert pick is None   # invented value rejected, never bound


# ── MMP platform-link id: not API-resolvable → ask, never guess ──────


# A directory with nothing resembling the GM Capture project, so the name
# tracks can't match — the only honest outcome for its platform id is "ask".
_UNRELATED_DIR = [
    {"repo_name": "demo-inventory-forecast@demand", "id": 189,
     "business_name": "Apr Revolvers", "model_count": 1,
     "models": [{"id": 1, "name": "apr-model", "is_production": True}]},
]


def _gm_capture_payload():
    p = _payload()
    p.project.cml_project_name = "order-capture"
    p.project.name = "GM Capture"
    p.project.mmp_project_id = "174"
    return p


def test_enrich_numeric_platform_id_asks_manual_not_fuzzy(monkeypatch):
    # 174 is an MMP web-platform id, absent from the API directory. The API
    # can't translate it, so we ask the reviewer to fill it manually — NOT a
    # "not found, did you mean <fuzzy>" with junk candidates.
    monkeypatch.setattr(enrich, "fetch_mmp_project_by_id", lambda n: None)  # API can't read it
    p = _gm_capture_payload()
    report = _enriched(p, cml=None, mmp=[dict(x) for x in _UNRELATED_DIR],
                       monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "project.mmp_project_id")
    assert clar.reason == "mmp-platform-id-manual"
    assert clar.kind == "mmp_project"
    assert clar.options == []                       # no misleading fuzzy guesses
    assert p.project.mmp_project_id == "174"         # untouched


def test_resolve_mmp_binding_does_not_llm_guess_numeric_platform_id(monkeypatch):
    # A bare numeric id that the API can't resolve must NOT be bound by the LLM
    # guessing off the CML name — it carries no semantics to match on.
    monkeypatch.setattr(enrich, "fetch_mmp_projects", lambda: [dict(x) for x in _UNRELATED_DIR])
    monkeypatch.setattr(enrich, "fetch_mmp_project_by_id", lambda n: None)
    monkeypatch.setattr(enrich, "_llm_pick_mmp",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM must not guess an id")))
    p = _gm_capture_payload()
    enrich.resolve_mmp_binding(p, "GM Capture handover, ray app")
    assert p.project.mmp_project_id == "174"          # left for manual entry


# ── doc signals: deterministic anchors from the raw Confluence export ─

from core.agent.doc_signals import (  # noqa: E402
    apply_doc_signals,
    derive_doc_signals,
    hints_block,
)

# Condensed from the real six-project consolidation (handover_docs_raw.md):
# page chrome up front, scheme-less app host, broken tables.
_MESSY_DOC = """\
northstar-forecast
 查看内联评论(&V)
 收藏(F) 观看(W) 分享(S)
页面… Consolidated information of hand-over-ed projects
创建者： Akshay Sachdeva，上次更新者： Alex Morgan，更新时间：三月 06, 2026  需要 2 分钟阅读时间
Introduction
This document serves as an official checklist before handing over NORTHSTAR FORECAST project to the Ops Team.
MetaData
Project Bitbucket Repo\thttps://bitbucket.example.com/projects/DEMO/repos/northstar-forecast
Ray Server App: dynamic-inventory-scoring-prod.ml-demo-workspace.apps.apps.demo.example.com
Prod stats url: https://prod-stat.ml-demo-workspace.apps.apps.demo.example.com/view/northstar-forecast
[For MMP] MMP platform link\thttps://runtime-mmp-web-prod.ml-demo-workspace.apps.apps.demo.example.com/project/160/projectDetails
Grafana: https://grafana-prod.ml-demo-workspace.apps.apps.demo.example.com/d/8-yuqASIk/dash?orgId=1
App: https://support-assistant-v2-prod.ml-demo-workspace.apps.apps.demo.example.com/dashboard/#/overview
"""


def test_doc_signals_derive_anchors_from_messy_export():
    s = derive_doc_signals(_MESSY_DOC)
    assert s.cml_project_names == ["northstar-forecast"]
    assert s.prod_stat_urls == [
        "https://prod-stat.ml-demo-workspace.apps.apps.demo.example.com/view/northstar-forecast"
    ]
    assert s.mmp_project_ids == ["160"]
    assert s.bitbucket_repos == ["northstar-forecast"]
    assert s.handover_names == ["NORTHSTAR FORECAST"]
    subs = {a.subdomain for a in s.app_links}
    # scheme-less Ray host detected; platform hosts (prod-stat/grafana/mmp) excluded
    assert "dynamic-inventory-scoring-prod" in subs
    assert "support-assistant-v2-prod" in subs
    assert not any(x.startswith(("prod-stat", "grafana", "runtime-mmp")) for x in subs)
    ray = next(a for a in s.app_links if a.subdomain == "support-assistant-v2-prod")
    assert ray.looks_ray
    block = hints_block(s)
    assert "northstar-forecast" in block and "160" in block


def test_apply_doc_signals_overrides_noisy_extraction():
    s = derive_doc_signals(_MESSY_DOC)
    p = OnboardingDraftPayload(
        project=ProjectDraft(
            name="northstar-forecast 查看内联评论(&V) 收藏(F)",   # noise-led extraction
            cml_project_name="consolidated-information",  # wrong: followed the prefix
        ),
    )
    apply_doc_signals(p, s)
    assert p.project.cml_project_name == "northstar-forecast"          # URL wins
    assert p.project.name == "NORTHSTAR FORECAST"                      # handover sentence wins
    assert p.project.prod_stat_url.endswith("/view/northstar-forecast")
    assert p.project.mmp_project_id == "160"
    assert any("disagrees" in w for w in p.warnings)
    # uncaptured app links are materialized as App drafts (not just warned)
    assert p.products, "a product was created to hold the materialized assets"
    subs = {a.cml_subdomain for prod in p.products for a in prod.apps}
    assert "dynamic-inventory-scoring-prod" in subs
    assert "support-assistant-v2-prod" in subs
    ray_app = next(a for prod in p.products for a in prod.apps
                   if a.cml_subdomain == "support-assistant-v2-prod")
    assert ray_app.cml_app_type == "ray"
    assert ray_app.application_url.startswith("https://")
    # MMP id with no MMP job → one stub MMP job to hold the binding
    mmp_jobs = [j for prod in p.products for j in prod.jobs if j.mmp_project_id]
    assert len(mmp_jobs) == 1


def test_apply_doc_signals_no_stub_job_when_mmp_job_exists():
    s = derive_doc_signals(_MESSY_DOC)
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NORTHSTAR FORECAST"),
        products=[ProductDraft(name="NORTHSTAR FORECAST",
                               jobs=[JobDraft(mmp_model_id="forecast_scoring")])],
    )
    apply_doc_signals(p, s)
    # extraction already made an MMP-bound job → no duplicate stub
    assert sum(1 for j in p.products[0].jobs if j.mmp_project_id or j.mmp_model_id) == 1


def test_no_stub_mmp_job_when_doc_disclaims_ml_models():
    doc = (_MESSY_DOC
           + "\nThe project does not include traditional ML models hence there "
             "are no MMP model tracking criteria.\n")
    s = derive_doc_signals(doc)
    assert s.no_mmp_models
    p = OnboardingDraftPayload(project=ProjectDraft(name="SALES Copilot V2"))
    apply_doc_signals(p, s)
    # MMP link present but doc tracks no models → no conjured MMP job
    assert not any(j.mmp_project_id for prod in p.products for j in prod.jobs)


def test_apply_doc_signals_app_added_to_existing_product():
    s = derive_doc_signals(_MESSY_DOC)
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NORTHSTAR FORECAST"),
        products=[ProductDraft(name="P1")],
    )
    apply_doc_signals(p, s)
    # apps land in the existing product, not a brand-new one
    assert len(p.products) == 1
    assert any(a.cml_subdomain == "dynamic-inventory-scoring-prod" for a in p.products[0].apps)


def test_apply_doc_signals_fills_but_never_overrides_good_values():
    s = derive_doc_signals(_MESSY_DOC)
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NORTHSTAR FORECAST", cml_project_name="NORTHSTAR_FORECAST",
                             mmp_project_id="demo-northstar-forecast"),
    )
    apply_doc_signals(p, s)
    # norm-equal CML name and an already-named MMP binding stay put
    assert p.project.cml_project_name == "NORTHSTAR_FORECAST"
    assert p.project.mmp_project_id == "demo-northstar-forecast"
    assert p.project.name == "NORTHSTAR FORECAST"


def test_doc_signals_ambiguous_anchors_do_not_autofill():
    two_docs = _MESSY_DOC + "\nProd stats url: https://prod-stat.x.example.com/view/another-proj\n"
    s = derive_doc_signals(two_docs)
    p = OnboardingDraftPayload(project=ProjectDraft(name="X"))
    apply_doc_signals(p, s)
    assert p.project.cml_project_name == ""   # two distinct /view/ names → ask, don't guess
    assert p.project.prod_stat_url == ""


# ── enrichment: MMP numeric id → repo name, model backfill ───────────

_MMP_DIR = [
    {"repo_name": "demo-northstar-forecast", "id": 160, "business_name": "NORTHSTAR FORECAST",
     "model_count": 2,
     "models": [{"name": "forecast_scoring", "is_production": True},
                {"name": "forecast_exp", "is_production": False}]},
    {"repo_name": "other", "id": 7, "business_name": "Other", "model_count": 0,
     "models": []},
]


def test_mmp_numeric_id_resolved_to_repo_name(monkeypatch):
    p = _payload()
    p.project.mmp_project_id = "160"
    report = _enriched(p, cml=None, mmp=_MMP_DIR, monkeypatch=monkeypatch)
    assert p.project.mmp_project_id == "demo-northstar-forecast"
    assert any("Verified in MMP" in w.message for w in report.warnings)
    assert not any(c.field_path == "project.mmp_project_id" for c in report.clarifications)


def test_mmp_numeric_id_resolved_by_direct_fetch_when_not_in_directory(monkeypatch):
    # The doc gives the numeric id (174); the shallow directory's first page
    # doesn't include it (large workspace), so the by-id lookup resolves it.
    # Reproduces the "MMP project [174] 找不到 but candidates are names" report.
    monkeypatch.setattr(enrich, "fetch_cml_project_names", lambda extra_queries=(): None)
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", lambda n: None)
    monkeypatch.setattr(enrich, "fetch_mmp_projects",
                        lambda: [{"repo_name": "other", "id": 999, "business_name": "",
                                  "model_count": 0, "models": []}])
    monkeypatch.setattr(enrich, "fetch_mmp_project_by_id",
                        lambda n: {"repo_name": "order-capture", "id": 174,
                                   "business_name": "GM Capture", "model_count": 0,
                                   "models": []} if n == 174 else None)
    p = OnboardingDraftPayload(project=ProjectDraft(name="GM Capture", mmp_project_id="174"))
    report = validate_payload(p)
    enrich.enrich_validation_report(p, report)
    assert p.project.mmp_project_id == "order-capture"
    assert not any(c.field_path == "project.mmp_project_id"
                   and c.reason == "mmp-project-not-found" for c in report.clarifications)


def test_mmp_resolved_by_name_when_numeric_id_unresolvable(monkeypatch):
    # The GM Capture case: the web-link id (174) doesn't match the directory's
    # id space and the direct by-id lookup fails, but the MMP repo "order-capture"
    # mirrors the CML project name → resolve by NAME (the dual-match).
    monkeypatch.setattr(enrich, "fetch_cml_project_names", lambda extra_queries=(): None)
    monkeypatch.setattr(enrich, "fetch_cml_project_assets", lambda n: None)
    monkeypatch.setattr(enrich, "fetch_mmp_project_by_id", lambda n: None)  # id path fails
    monkeypatch.setattr(enrich, "fetch_mmp_projects",
                        lambda: [{"repo_name": "order-capture", "id": 9001, "business_name": "GM Capture",
                                  "model_count": 0, "models": []},
                                 {"repo_name": "other", "id": 9002, "business_name": "",
                                  "model_count": 0, "models": []}])
    p = OnboardingDraftPayload(project=ProjectDraft(
        name="GM Capture", cml_project_name="order-capture", mmp_project_id="174"))
    report = validate_payload(p)
    enrich.enrich_validation_report(p, report)
    assert p.project.mmp_project_id == "order-capture"        # matched by name
    assert not any(c.field_path == "project.mmp_project_id"
                   and c.reason == "mmp-project-not-found" for c in report.clarifications)


def test_mmp_model_backfill_and_uncovered_production_warning(monkeypatch):
    p = _payload()
    p.project.mmp_project_id = "demo-northstar-forecast"
    job = p.products[0].jobs[0]
    job.mmp_model_id = "FORECAST SCORING"   # sloppy spelling from the document
    report = _enriched(p, cml=None, mmp=_MMP_DIR, monkeypatch=monkeypatch)
    assert job.mmp_model_id == "forecast_scoring"        # canonicalized
    assert job.mmp_project_id == "demo-northstar-forecast"   # project binding propagated
    assert not any("production models not bound" in w.message for w in report.warnings)

    # a second project where no job covers the production model
    p2 = _payload()
    p2.project.mmp_project_id = "demo-northstar-forecast"
    report2 = _enriched(p2, cml=None, mmp=_MMP_DIR, monkeypatch=monkeypatch)
    assert any("forecast_scoring" in w.message and "production models not bound" in w.message
               for w in report2.warnings)


def test_mmp_unknown_model_asks_with_options(monkeypatch):
    p = _payload()
    p.project.mmp_project_id = "demo-northstar-forecast"
    p.products[0].jobs[0].mmp_model_id = "ghost_model"
    report = _enriched(p, cml=None, mmp=_MMP_DIR, monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "products[0].jobs[0].mmp_model_id")
    assert clar.reason == "mmp-model-not-found"


# ── enrichment: CML asset backfill after the binding is confirmed ────

_CML_ASSETS = {
    "jobs": [{"name": "ns_batch_sg", "schedule": "0 2 * * 6"},
             {"name": "ns_batch_my", "schedule": "0 3 * * 6"}],
    "apps": [{"name": "ns-api", "subdomain": "dynamic-inventory-scoring-prod"}],
}


def test_cml_backfill_fills_cron_and_app_fields(monkeypatch):
    p = _payload()
    job = p.products[0].jobs[0]
    job.schedule_cron = ""                       # document didn't give it
    app = p.products[0].apps[0]
    app.cml_application_name = ""
    app.application_url = "https://dynamic-inventory-scoring-prod.ml-x.apps.demo.example.com"
    report = _enriched(p, cml=["material-classifier"], assets=_CML_ASSETS,
                       monkeypatch=monkeypatch)
    assert job.schedule_cron == "0 2 * * 6"      # from the CML job's schedule
    assert app.cml_application_name == "ns-api"  # matched by subdomain
    assert app.cml_subdomain == "dynamic-inventory-scoring-prod"
    # the backfilled fields' "document didn't give this" questions are gone
    asked = {c.field_path for c in report.clarifications}
    assert "products[0].jobs[0].schedule_cron" not in asked
    assert "products[0].apps[0].cml_application_name" not in asked
    # the project's other scheduled CML job is flagged as not onboarded
    assert any("ns_batch_my" in w.message for w in report.warnings)


def test_cml_backfill_unknown_job_gets_options(monkeypatch):
    p = _payload()
    p.products[0].jobs[0].cml_job_name = "ns_batch_sgp"   # close but wrong
    report = _enriched(p, cml=["material-classifier"], assets=_CML_ASSETS,
                       monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "products[0].jobs[0].cml_job_name")
    assert clar.reason == "cml-job-not-found"
    assert any(o.value == "ns_batch_sg" for o in clar.options)
    assert any("cron" in o.detail for o in clar.options)


def test_mmp_only_job_offered_cml_jobs_to_pick(monkeypatch):
    # The user's case: an MMP job with no Control-M / CML job name can't be
    # monitored — the enrichment fetches the CML project's jobs as options.
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="Inventory Scoring", jobs=[
            JobDraft(mmp_project_id="material-classifier", mmp_model_id="ns_model")])],
    )
    report = _enriched(p, cml=["material-classifier"], assets=_CML_ASSETS,
                       monkeypatch=monkeypatch)
    clar = next(c for c in report.clarifications
                if c.field_path == "products[0].jobs[0].cml_job_name")
    assert clar.reason == "cml-job-for-mmp"
    values = {o.value for o in clar.options}
    assert "ns_batch_sg" in values and "ns_batch_my" in values


def test_job_with_controlm_not_offered_cml_jobs(monkeypatch):
    # A job that already has a Control-M name is schedulable on its own —
    # don't pester the reviewer with a CML-job picker.
    p = _payload()
    p.products[0].jobs[0].cml_job_name = ""
    p.products[0].jobs[0].control_m_job_name = "NS_SG_001"
    report = _enriched(p, cml=["material-classifier"], assets=_CML_ASSETS,
                       monkeypatch=monkeypatch)
    assert not any(c.field_path == "products[0].jobs[0].cml_job_name"
                   for c in report.clarifications)
