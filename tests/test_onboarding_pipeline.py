"""Onboarding pipeline v2 — scenario normalization, owner-email injection,
the refine-mode protective merge, Confluence URL parsing, and the new ingest
formats. Pure-logic tests; LLM calls are stubbed or exercised via their
no-LLM fallbacks.
"""

import pytest

from core.agent import scenario_enrich
from core.agent.ingest import (
    IMAGE_SENTINEL,
    UnsupportedFormatError,
    decode_image_sentinel,
    encode_image_sentinel,
    extract_text,
    html_to_text,
)
from core.agent.onboarding_graph import merge_preserving, run_onboarding_pipeline
from core.agent.scenario_enrich import (
    apply_owner_contact_fallback,
    inject_email_actions,
    normalize_scenario_types,
)
from core.agent.validate import validate_payload
from core.agent.schemas import (
    AppDraft,
    AppScenarioDraft,
    EmailTemplateDraft,
    JobDraft,
    JobScenarioDraft,
    OnboardingDraftPayload,
    ProductDraft,
    ProjectDraft,
)
from core.integrations.confluence_connector import ConfluenceError, parse_page_locator


def _payload() -> OnboardingDraftPayload:
    return OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(
            name="Inventory Scoring",
            jobs=[JobDraft(cml_job_name="ns_batch_sg", schedule_cron="0 2 * * 6",
                           control_m_job_name="NS_SG_001", owner_contact="lead@example.com")],
            apps=[AppDraft(cml_application_name="ns-api", application_url="https://x",
                           owner_contact="lead@example.com")],
        )],
    )


# ── scenario type normalization ───────────────────────────────────────

def test_unknown_scenario_type_becomes_name_and_is_reclassified(monkeypatch):
    monkeypatch.setattr(scenario_enrich, "_classify_with_llm", lambda items: None)
    p = _payload()
    p.products[0].jobs[0].scenarios = [
        JobScenarioDraft(scenario_type="CML no resource",
                         condition_description="job cannot get resources to start"),
        JobScenarioDraft(scenario_type="Data pipeline failure",
                         condition_description="upstream ingest failed"),
    ]
    p.products[0].apps[0].scenarios = [
        AppScenarioDraft(scenario_type="API must be up",
                         condition_description="DNS API must be up"),
    ]
    normalize_scenario_types(p)
    js = p.products[0].jobs[0].scenarios
    # original table label preserved as the *name*, enum as the type
    assert js[0].scenario_name == "CML no resource"
    assert js[0].scenario_type == "not_triggered"
    assert js[1].scenario_name == "Data pipeline failure"
    assert js[1].scenario_type == "dependency_failed"
    asc = p.products[0].apps[0].scenarios[0]
    assert asc.scenario_name == "API must be up"
    assert asc.scenario_type == "healthcheck_failed"
    assert any("auto-classified" in w for w in p.warnings)


def test_known_scenario_types_untouched(monkeypatch):
    monkeypatch.setattr(scenario_enrich, "_classify_with_llm", lambda items: None)
    p = _payload()
    p.products[0].jobs[0].scenarios = [
        JobScenarioDraft(scenario_type="mmp_perf_drift", scenario_name="Performance drift"),
    ]
    normalize_scenario_types(p)
    assert p.products[0].jobs[0].scenarios[0].scenario_type == "mmp_perf_drift"
    assert p.warnings == []


def test_llm_classification_wins_when_valid(monkeypatch):
    monkeypatch.setattr(scenario_enrich, "_classify_with_llm",
                        lambda items: ["mmp_perf_drift"])
    p = _payload()
    p.products[0].jobs[0].scenarios = [
        JobScenarioDraft(scenario_type="Perf drift > 5%", condition_description="…")]
    normalize_scenario_types(p)
    assert p.products[0].jobs[0].scenarios[0].scenario_type == "mmp_perf_drift"


# ── owner-email injection ─────────────────────────────────────────────

def test_email_injected_with_scenario_contact_to_and_owner_cc():
    p = _payload()
    sc = JobScenarioDraft(
        scenario_type="dependency_failed", scenario_name="Data ingest failure",
        condition_description="upstream feed missing",
        escalation_target="Maxime Azzi",
        action_steps=["Check upstream tables"],
    )
    p.products[0].jobs[0].scenarios = [sc]
    inject_email_actions(p)
    tpl = sc.email_template
    assert tpl.to == "Maxime Azzi"                  # 固定联系人 → To
    assert tpl.cc == "lead@example.com"                # owner rides along in Cc
    assert "Inventory Scoring" in tpl.subject and "Data ingest failure" in tpl.subject
    # Body is the canonical Control-M-request layout, NOT a free-text summary.
    assert tpl.body.startswith("Please kindly perform the following job requests.")
    assert "Type of Request:" in tpl.body
    assert "Re-Run" in tpl.body                     # dependency_failed → Re-Run
    assert "{{job_name}}" in tpl.body and "{{date}}" in tpl.body
    assert tpl.body.rstrip().endswith("{{sender_name}}")
    assert "upstream feed missing" not in tpl.body  # condition stays in the runbook
    # a send-email action step was appended
    assert any("email" in s.lower() for s in sc.action_steps)


def test_email_body_matches_canonical_template_per_scenario_type():
    from core.agent.scenario_enrich import _default_email_body

    # Job: not_triggered → Trigger; app: offline → Restart, with {{app_name}}.
    job_body = _default_email_body("job", "not_triggered")
    assert "Job:" in job_body and "{{job_name}}" in job_body
    assert "Group:" in job_body and "Table:" in job_body          # job-only rows
    assert "Type of Request:" in job_body and "Trigger" in job_body

    app_body = _default_email_body("app", "offline")
    assert "Application:" in app_body and "{{app_name}}" in app_body
    assert "Group:" not in app_body                                # dropped for apps
    assert "Restart" in app_body
    assert "No Impact" in app_body


def test_email_falls_back_to_owner_and_is_idempotent():
    p = _payload()
    sc = AppScenarioDraft(scenario_type="offline", scenario_name="API down")
    p.products[0].apps[0].scenarios = [sc]
    inject_email_actions(p)
    assert sc.email_template.to == "lead@example.com"
    assert sc.email_template.cc == ""
    steps_after_first = list(sc.action_steps)
    inject_email_actions(p)                          # second pass: no dupes
    assert sc.action_steps == steps_after_first


def test_owner_contact_fallback_from_project_owner():
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NORTHSTAR FORECAST", owner_name="Alex Morgan"),
        products=[ProductDraft(name="X", jobs=[
            JobDraft(cml_job_name="j1"),                       # empty owner → filled
            JobDraft(cml_job_name="j2", owner_contact="real@example.com"),  # kept
        ], apps=[AppDraft(cml_application_name="a1")])],       # empty owner → filled
    )
    apply_owner_contact_fallback(p)
    assert p.products[0].jobs[0].owner_contact == "AlexMorgan@example.com"
    assert p.products[0].jobs[1].owner_contact == "real@example.com"
    assert p.products[0].apps[0].owner_contact == "AlexMorgan@example.com"
    assert any("project owner" in w and "AlexMorgan@example.com" in w for w in p.warnings)


def test_owner_contact_fallback_noop_without_owner_name():
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="X"),   # no owner_name
        products=[ProductDraft(name="X", jobs=[JobDraft(cml_job_name="j1")])],
    )
    apply_owner_contact_fallback(p)
    assert p.products[0].jobs[0].owner_contact == ""   # nothing to derive from


def test_owner_fallback_suppresses_missing_owner_clarification():
    # inject_email_actions runs the fallback first, so validate sees a filled
    # owner and doesn't ask for it.
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="X", owner_name="Alex Morgan"),
        products=[ProductDraft(name="X", jobs=[
            JobDraft(cml_job_name="j1", control_m_job_name="J1", schedule_cron="0 2 * * 1")])],
    )
    inject_email_actions(p)
    report = validate_payload(p)
    assert not any(c.reason == "missing-owner-contact" for c in report.clarifications)


def test_email_never_clobbers_authored_template():
    p = _payload()
    sc = JobScenarioDraft(
        scenario_type="not_triggered",
        email_template=EmailTemplateDraft(to="custom@example.com", subject="S"),
    )
    p.products[0].jobs[0].scenarios = [sc]
    inject_email_actions(p)
    assert sc.email_template.to == "custom@example.com"
    assert sc.email_template.subject == "S"


# ── Control-M rerun-request fields from the doc's email template ──────

def test_controlm_request_fields_fold_into_the_email_body():
    p = _payload()
    job = p.products[0].jobs[0]
    job.control_m_application = "APPL_CML_EDSP"
    job.control_m_group = "GRP_01_DEMO_DS_MONETIZATION_EDSP"
    job.control_m_table = "TBL_01_DEMO_DS_MONETIZATION_EDSP"
    job.change_number = "CHG00000344706"
    job.scenarios = [
        JobScenarioDraft(scenario_type="triggered_but_failed", scenario_name="Failed"),
        JobScenarioDraft(scenario_type="dependency_failed", scenario_name="Pipeline"),
    ]
    inject_email_actions(p)

    for sc in job.scenarios:
        body = sc.email_template.body.replace("\xa0", " ")
        # The real identifiers replace the blank placeholders on every scenario
        # of the job (the doc gives them once; the fill is job-wide).
        assert "Application:       APPL_CML_EDSP" in body
        assert "Group:             GRP_01_DEMO_DS_MONETIZATION_EDSP" in body
        assert "Table:             TBL_01_DEMO_DS_MONETIZATION_EDSP" in body
        # CHG/TSK Number row is appended with the exact platform label.
        assert "CHG/TSK Number:    CHG00000344706" in body
        # Job token is left for send-time substitution, not overwritten.
        assert "{{job_name}}" in body

    # The conduit is cleared once composed — the email body is the sole store.
    assert job.control_m_application == ""
    assert job.control_m_group == ""
    assert job.control_m_table == ""
    assert job.change_number == ""


def test_controlm_request_fields_are_idempotent():
    p = _payload()
    job = p.products[0].jobs[0]
    job.control_m_application = "APPL_CML_EDSP"
    job.scenarios = [JobScenarioDraft(scenario_type="triggered_but_failed", scenario_name="F")]
    inject_email_actions(p)
    body_after_first = job.scenarios[0].email_template.body
    inject_email_actions(p)  # conduit now empty; body must not change or dup the CHG row
    assert job.scenarios[0].email_template.body == body_after_first


def test_default_body_without_controlm_keeps_blank_rows():
    from core.agent.scenario_enrich import _default_email_body

    body = _default_email_body("job", "triggered_but_failed")
    # No doc values → the Application/Group/Table rows stay blank and there is
    # no CHG/TSK Number row (the reviewer can add one from the editor).
    assert "Application:" in body
    assert "CHG/TSK Number" not in body


# ── refine-mode protective merge ──────────────────────────────────────

def test_merge_fills_blanks_but_never_overwrites():
    old = _payload()
    old.products[0].jobs[0].description = ""          # blank → fillable
    new = _payload()
    new.project.name = "REWRITTEN"                    # non-empty old → must lose
    new.project.description = "from the LLM"          # old blank → fills
    new.products[0].jobs[0].description = "weekly scoring job"
    new.products[0].jobs[0].schedule_cron = "1 1 * * *"  # old non-empty → must lose
    merged = merge_preserving(old, new)
    assert merged.project.name == "Inventory Scoring"
    assert merged.project.description == "from the LLM"
    assert merged.products[0].jobs[0].description == "weekly scoring job"
    assert merged.products[0].jobs[0].schedule_cron == "0 2 * * 6"


def test_merge_appends_new_entities_and_keeps_old_extras():
    old = _payload()
    new = _payload()
    new.products[0].jobs.append(JobDraft(cml_job_name="ns_batch_my"))   # LLM found one more
    merged = merge_preserving(old, new)
    names = [j.cml_job_name for j in merged.products[0].jobs]
    assert names == ["ns_batch_sg", "ns_batch_my"]

    # reverse: refine output dropping a job must not lose it
    old2 = _payload()
    old2.products[0].jobs.append(JobDraft(cml_job_name="reviewer_added"))
    merged2 = merge_preserving(old2, _payload())
    assert [j.cml_job_name for j in merged2.products[0].jobs] == ["ns_batch_sg", "reviewer_added"]


def test_merge_keeps_reviewer_step_lists():
    old = _payload()
    old.products[0].jobs[0].scenarios = [JobScenarioDraft(
        scenario_type="not_triggered", action_steps=["reviewer trimmed this list"])]
    new = _payload()
    new.products[0].jobs[0].scenarios = [JobScenarioDraft(
        scenario_type="not_triggered", action_steps=["a", "b", "c"])]
    merged = merge_preserving(old, new)
    assert merged.products[0].jobs[0].scenarios[0].action_steps == ["reviewer trimmed this list"]


def test_refine_pipeline_requires_payload():
    with pytest.raises(ValueError):
        run_onboarding_pipeline(text="x", mode="refine")


# ── ingest: image sentinel / pdf / html helper ───────────────────────

def test_image_roundtrips_through_sentinel():
    sentinel = extract_text("page1.png", b"\x89PNG fakebytes")
    assert sentinel.startswith(IMAGE_SENTINEL)
    mime, payload = decode_image_sentinel(sentinel)
    assert mime == "image/png"
    import base64
    assert base64.b64decode(payload) == b"\x89PNG fakebytes"


def test_image_size_cap():
    from core.agent.ingest import MAX_IMAGE_BYTES

    with pytest.raises(UnsupportedFormatError):
        encode_image_sentinel("big.png", b"x" * (MAX_IMAGE_BYTES + 1))


def test_scanned_pdf_rejected_with_hint():
    pytest.importorskip("pypdf")
    # minimal empty-page PDF (no text layer)
    import io

    from pypdf import PdfWriter

    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.write(buf)
    with pytest.raises(UnsupportedFormatError, match="no text layer"):
        extract_text("scan.pdf", buf.getvalue())


def test_html_to_text_public_helper():
    text = html_to_text('<table><tr><td>Job</td><td><a href="https://u">Prod Stat</a></td></tr></table>')
    assert "Job | Prod Stat (https://u)" in text


def test_confluence_full_page_export_strips_chrome():
    # A whole-page Confluence export: global nav + sidebar before the body,
    # comments + footer after it. Only #main-content should survive.
    html = (
        '<html><body>'
        '<div id="navigation"><a href="/spacedirectory/view.action">空间</a>'
        '<a href="/browsepeople.action">人员</a></div>'
        '<ul class="sidebar"><li><a href="/collector/pages.action">页面树</a></li></ul>'
        '<div id="main-content" class="wiki-content">'
        '<p>handing over GM Capture project to the Ops Team</p>'
        '<p>Prod stats url: <a href="https://prod-stat.x/view/order-capture">order-capture</a></p>'
        '</div>'
        '<div id="comments-section"><p>missing scripts folder</p></div>'
        '<div id="footer">基于 Atlassian Confluence</div>'
        '</body></html>'
    )
    text = html_to_text(html)
    assert "handing over GM Capture" in text
    assert "view/order-capture" in text          # the binding-deciding URL survives
    assert "空间" not in text and "页面树" not in text   # nav chrome gone
    assert "missing scripts folder" not in text         # comments gone
    assert "Atlassian" not in text                       # footer gone


def test_html_to_text_non_confluence_passthrough():
    # No #main-content marker → parse the whole thing (don't slice arbitrary HTML).
    text = html_to_text("<body><p>plain page</p><p>second line</p></body>")
    assert "plain page" in text and "second line" in text


# ── hallucinated-asset guard (alert rows misread as fabricated jobs) ──

from core.agent.doc_signals import drop_hallucinated_assets  # noqa: E402


def test_drop_hallucinated_job_names_and_prune_phantom_jobs():
    # NORTHSTAR-FORECAST failure mode: the LLM turned alert-table scenario rows into
    # jobs with invented names. Those names don't occur in the source.
    text = (
        "handing over NORTHSTAR FORECAST project to the Ops Team. "
        "CML no resource MMP ONLY COMPULSORY | Send control M job to control M team for rerun. "
        "Data pipeline failure MMP ONLY COMPULSORY | Send email to above contact points."
    )
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NORTHSTAR FORECAST"),
        products=[ProductDraft(name="NORTHSTAR FORECAST", jobs=[
            JobDraft(cml_job_name="CML Resource Monitoring",
                     description="CML no resource MMP ONLY COMPULSORY"),
            JobDraft(control_m_job_name="Data Pipeline Monitoring",
                     description="Data pipeline failure MMP ONLY COMPULSORY"),
            # a genuine job whose name IS in the source survives untouched
            JobDraft(mmp_model_id="forecast_scoring"),
        ])],
    )
    drop_hallucinated_assets(p, text)
    jobs = p.products[0].jobs
    assert len(jobs) == 1                              # both phantom jobs pruned
    assert jobs[0].mmp_model_id == "forecast_scoring"     # real one kept
    assert any("fabricated" in w for w in p.warnings)
    assert any("Removed" in w and "scenario" in w for w in p.warnings)


def test_guard_keeps_real_name_present_in_source():
    text = "Control-M job NS_SG_001 runs the scoring. CML job ns_batch_sg."
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NS"),
        products=[ProductDraft(name="NS", jobs=[
            JobDraft(cml_job_name="ns_batch_sg", control_m_job_name="NS_SG_001")])],
    )
    drop_hallucinated_assets(p, text)
    job = p.products[0].jobs[0]
    assert job.cml_job_name == "ns_batch_sg"           # present in source → kept
    assert job.control_m_job_name == "NS_SG_001"


# ── Control-M job-sheet import ───────────────────────────────────────

from core.agent import controlm_sheet  # noqa: E402
from core.agent.controlm_sheet import (  # noqa: E402
    ControlMJob,
    UnsupportedSheetError,
    merge_controlm_into_draft,
    parse_sheet_to_text,
)


def test_controlm_sheet_parse_tsv_and_unsupported():
    text = parse_sheet_to_text(
        "jobs.tsv",
        b"S/No\tJob Name\tPARM1 (Project_Name)\n1\tPKG_CML_X_RUN_W_EDSP\tmaterial-classifier",
    )
    assert "Job Name | PARM1" in text
    assert "PKG_CML_X_RUN_W_EDSP | material-classifier" in text
    with pytest.raises(UnsupportedSheetError):
        parse_sheet_to_text("jobs.pdf", b"%PDF-")


def test_controlm_merge_folds_into_stub_and_creates_new():
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="NS", jobs=[
            # an MMP stub job for NS SG, no Control-M name yet
            JobDraft(mmp_model_id="NS SG Batch Scoring", description="NS SG Batch Scoring Job")])],
    )
    cm = [
        ControlMJob(control_m_job_name="PKG_CML_MATERIAL_CLASSIFIER_NS_SG_RUN_W_EDSP",
                    cml_project_name="material-classifier", schedule_cron="55 14 * * 1",
                    description="NS SG Batch Scoring Job"),
        ControlMJob(control_m_job_name="PKG_CML_MATERIAL_CLASSIFIER_NS_GE_RUN_BW_EDSP",
                    cml_project_name="material-classifier", schedule_cron="30 11 * * 2,5",
                    description="NS GE Batch Scoring Job"),
    ]
    merge_controlm_into_draft(p, cm)
    jobs = p.products[0].jobs
    assert len(jobs) == 2
    sg = next(j for j in jobs if "NS_SG" in j.control_m_job_name)
    assert sg.mmp_model_id == "NS SG Batch Scoring"      # folded into the stub
    assert sg.schedule_cron == "55 14 * * 1"
    ge = next(j for j in jobs if "NS_GE" in j.control_m_job_name)   # created new
    assert ge.description == "NS GE Batch Scoring Job"
    assert ge.schedule_cron == "30 11 * * 2,5"


def test_controlm_merge_idempotent_backfills_schedule():
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NS", cml_project_name="material-classifier"),
        products=[ProductDraft(name="NS", jobs=[
            JobDraft(control_m_job_name="PKG_CML_X_RUN_W_EDSP")])],   # already present, no cron
    )
    merge_controlm_into_draft(p, [ControlMJob(
        control_m_job_name="PKG_CML_X_RUN_W_EDSP", schedule_cron="0 2 * * 1")])
    assert len(p.products[0].jobs) == 1                  # no duplicate
    assert p.products[0].jobs[0].schedule_cron == "0 2 * * 1"


def test_controlm_merge_uses_llm_when_similarity_cannot_link(monkeypatch):
    # The real test4.html case: draft jobs are identified only by MMP model
    # numbers (string similarity to the sheet's region-coded names ≈ 0), so the
    # LLM — reading the document's model↔region mapping — does the folding.
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="Inventory Scoring", cml_project_name="material-classifier"),
        products=[ProductDraft(name="NS", jobs=[
            JobDraft(cml_job_name="Inventory_Scoring_Model_Demo_API", mmp_model_id="345"),  # NS SG
            JobDraft(cml_job_name="Inventory_Scoring_Model_Demo_API", mmp_model_id="344"),  # NS GE
        ])],
    )
    cm = [
        ControlMJob(control_m_job_name="PKG_CML_MATERIAL_CLASSIFIER_NS_GE_RUN_BW_EDSP",
                    cml_project_name="material-classifier", schedule_cron="30 11 * * 2,5",
                    description="NS GE Batch Scoring Job"),
        ControlMJob(control_m_job_name="PKG_CML_MATERIAL_CLASSIFIER_NS_SG_RUN_W_EDSP",
                    cml_project_name="material-classifier", schedule_cron="55 14 * * 1",
                    description="NS SG Batch Scoring Job"),
    ]
    # NS GE → draft index 1 (model 344), NS SG → draft index 0 (model 345).
    monkeypatch.setattr(controlm_sheet, "_llm_match_controlm_to_jobs",
                        lambda src, cms, drafts: [1, 0])
    merge_controlm_into_draft(p, cm, source_text="NS SG=345 … NS GE=344 …")

    jobs = p.products[0].jobs
    assert len(jobs) == 2                                # folded, nothing appended
    assert jobs[1].control_m_job_name == "PKG_CML_MATERIAL_CLASSIFIER_NS_GE_RUN_BW_EDSP"
    assert jobs[1].schedule_cron == "30 11 * * 2,5"
    assert jobs[0].control_m_job_name == "PKG_CML_MATERIAL_CLASSIFIER_NS_SG_RUN_W_EDSP"
    assert jobs[0].schedule_cron == "55 14 * * 1"


def test_controlm_merge_llm_falls_back_to_similarity(monkeypatch):
    # LLM unavailable (all -1) → the description-similarity path still folds the
    # sheet job into the matching stub.
    monkeypatch.setattr(controlm_sheet, "_llm_match_controlm_to_jobs",
                        lambda src, cms, drafts: [-1 for _ in cms])
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="NS", cml_project_name="material-classifier"),
        products=[ProductDraft(name="NS", jobs=[
            JobDraft(description="NS SG Batch Scoring Job")])],
    )
    merge_controlm_into_draft(p, [ControlMJob(
        control_m_job_name="PKG_NS_SG_RUN", description="NS SG Batch Scoring Job",
        schedule_cron="55 14 * * 1")], source_text="doc")
    assert len(p.products[0].jobs) == 1
    assert p.products[0].jobs[0].control_m_job_name == "PKG_NS_SG_RUN"


def test_guard_clears_invented_app_name_but_keeps_app_via_subdomain():
    text = "Ray app at order-capture-prod.ml-x.apps.demo.example.com"
    p = OnboardingDraftPayload(
        project=ProjectDraft(name="GM"),
        products=[ProductDraft(name="GM", apps=[
            AppDraft(cml_application_name="GM Capture Service",   # invented
                     cml_subdomain="order-capture-prod")])],
    )
    drop_hallucinated_assets(p, text)
    apps = p.products[0].apps
    assert len(apps) == 1                              # kept (subdomain is a real signal)
    assert apps[0].cml_application_name == ""          # invented name cleared
    assert apps[0].cml_subdomain == "order-capture-prod"


# ── confluence URL parsing ────────────────────────────────────────────

def test_parse_page_locator_variants():
    base, pid, st = parse_page_locator(
        "https://confluence.example.com/pages/viewpage.action?pageId=123456")
    assert (base, pid, st) == ("https://confluence.example.com", "123456", None)

    base, pid, st = parse_page_locator(
        "https://confluence.example.com/wiki/spaces/DEMO/pages/98765/NORTHSTAR-FORECAST+Ops")
    assert base == "https://confluence.example.com/wiki"
    assert pid == "98765"

    base, pid, st = parse_page_locator(
        "https://confluence.example.com/display/DEMO/NORTHSTAR-FORECAST+Ops+%2F+Maintenance")
    assert pid is None
    assert st == ("DEMO", "NORTHSTAR-FORECAST Ops / Maintenance")

    with pytest.raises(ConfluenceError):
        parse_page_locator("not-a-url")
    with pytest.raises(ConfluenceError):
        parse_page_locator("https://confluence.example.com/x/abc")


# ── pipeline wiring (sequential path, LLM stubbed) ───────────────────

def test_extract_pipeline_runs_normalize_and_email_nodes(monkeypatch):
    from core.agent import extraction, onboarding_graph

    fake = _payload()
    fake.products[0].jobs[0].scenarios = [
        JobScenarioDraft(scenario_type="CML no resource", condition_description="no resource")]
    monkeypatch.setattr(extraction, "run_extraction", lambda text: fake)
    monkeypatch.setattr(scenario_enrich, "_classify_with_llm", lambda items: None)
    # offline finalize: no CML/MMP network
    from core.agent import onboarding_enrich
    monkeypatch.setattr(onboarding_enrich, "fetch_cml_project_names", lambda extra_queries=(): None)
    monkeypatch.setattr(onboarding_enrich, "fetch_mmp_projects", lambda: None)
    monkeypatch.setattr(onboarding_enrich, "fetch_cml_project_assets", lambda name: None)
    monkeypatch.setattr(onboarding_graph, "_LANGGRAPH_AVAILABLE", False)
    monkeypatch.setattr(onboarding_graph, "_compiled_graph", None)

    state = run_onboarding_pipeline(text="dummy doc")
    payload = state["payload"]
    sc = payload.products[0].jobs[0].scenarios[0]
    assert sc.scenario_name == "CML no resource"           # normalize node ran
    assert sc.scenario_type == "not_triggered"
    assert sc.email_template.to == "lead@example.com"         # email node ran
    assert state["report"] is not None
