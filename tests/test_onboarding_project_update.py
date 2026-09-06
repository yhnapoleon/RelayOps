"""Onboarding "add assets to an existing project" — merge + diff logic.

Pure-logic, same convention as test_onboarding_agent.py: the snapshot is built
by hand rather than read from a database, so everything the reviewer sees
(what is new, what changes, what stays) is pinned down without a live project.
"""

from core.agent.project_update import ProjectSnapshot, annotate, merge_into_snapshot
from core.agent.scenario_enrich import inject_email_actions
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


def _snapshot() -> ProjectSnapshot:
    """A live project: one product, one job (with one scenario), one app."""
    payload = OnboardingDraftPayload(
        project=ProjectDraft(
            name="Inventory Scoring",
            description="Existing description",
            cml_project_name="northstar-forecast",
        ),
        products=[ProductDraft(
            name="Screening",
            jobs=[JobDraft(
                cml_job_name="ns_batch_sg",
                control_m_job_name="NS_BATCH_SG",
                schedule_cron="0 2 * * 6",
                control_m_cron="0 2 * * 6",
                owner_contact="old@example.com",
                mmp_model_id="M-1",
                scenarios=[JobScenarioDraft(
                    scenario_type="not_triggered",
                    scenario_name="Missed run",
                    action_steps=["Old step"],
                    email_template=EmailTemplateDraft(subject="Existing subject"),
                )],
            )],
            apps=[AppDraft(
                cml_application_name="forecast-ui",
                application_url="https://forecast.example.com/",
                health_check_url="https://forecast.example.com/",
            )],
        )],
    )
    snap = ProjectSnapshot(project_id=7, name="Inventory Scoring", payload=payload)
    snap.ids.update({
        "project": 7,
        "products[0]": 11,
        "products[0].jobs[0]": 21,
        "products[0].jobs[0].scenarios[0]": 31,
        "products[0].apps[0]": 41,
    })
    return snap


def _nodes(payload, snap):
    return annotate(payload, snap).nodes


# ── matching ─────────────────────────────────────────────────────────


def test_job_matches_on_either_name_case_and_separator_insensitively():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening", jobs=[JobDraft(cml_job_name="NS-Batch-SG", owner_contact="new@example.com")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert len(merged.products) == 1
    assert len(merged.products[0].jobs) == 1
    assert merged.products[0].jobs[0].owner_contact == "new@example.com"
    # The platform spelling of the identity field survives the doc's variant.
    assert merged.products[0].jobs[0].cml_job_name == "ns_batch_sg"


def test_job_matches_on_mmp_model_when_names_differ():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(mmp_model_id="M-1", schedule_cron="30 3 * * 1-5")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert len(merged.products[0].jobs) == 1
    assert merged.products[0].jobs[0].schedule_cron == "30 3 * * 1-5"


def test_app_matches_on_url_ignoring_scheme_and_trailing_slash():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        apps=[AppDraft(application_url="http://forecast.example.com", owner_contact="a@example.com")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert len(merged.products[0].apps) == 1
    assert merged.products[0].apps[0].owner_contact == "a@example.com"
    assert merged.products[0].apps[0].cml_application_name == "forecast-ui"


def test_product_is_matched_by_where_its_assets_live_not_by_name():
    """A document that files a known job under its own product name must not
    re-create that job under a second product."""
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Totally Different Product Name",
        jobs=[JobDraft(cml_job_name="ns_batch_sg", description="doc description")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert len(merged.products) == 1
    assert merged.products[0].name == "Screening"
    assert merged.products[0].jobs[0].description == "doc description"
    assert any("Totally Different Product Name" in w for w in merged.warnings)


def test_unknown_product_and_assets_are_appended():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Reporting",
        jobs=[JobDraft(cml_job_name="rpt_daily", owner_contact="r@example.com")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert [p.name for p in merged.products] == ["Screening", "Reporting"]
    nodes = _nodes(merged, snap)
    assert nodes["products[1]"].change == "new"
    assert nodes["products[1].jobs[0]"].change == "new"
    assert nodes["products[1].jobs[0]"].existing_id is None
    # The pre-existing assets are untouched.
    assert nodes["products[0].jobs[0]"].change == "unchanged"
    assert nodes["products[0].apps[0]"].change == "unchanged"


def test_new_job_lands_in_the_matched_product():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[
            JobDraft(cml_job_name="ns_batch_sg"),
            JobDraft(cml_job_name="ns_batch_my", owner_contact="my@example.com"),
        ],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert len(merged.products) == 1
    assert [j.cml_job_name for j in merged.products[0].jobs] == ["ns_batch_sg", "ns_batch_my"]
    assert _nodes(merged, snap)["products[0].jobs[1]"].change == "new"


# ── identity fields are fill-blank-only ──────────────────────────────


def test_identity_conflict_keeps_the_platform_name_and_warns():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        # Matched on the Control-M name; the doc's CML name disagrees.
        jobs=[JobDraft(control_m_job_name="NS_BATCH_SG", cml_job_name="ns_batch_sg_v2")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert merged.products[0].jobs[0].cml_job_name == "ns_batch_sg"
    assert any("ns_batch_sg_v2" in w for w in merged.warnings)
    assert _nodes(merged, snap)["products[0].jobs[0]"].change == "unchanged"


def test_blank_identity_field_is_filled_from_the_document():
    snap = _snapshot()
    snap.payload.products[0].jobs[0].control_m_job_name = ""
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(cml_job_name="ns_batch_sg", control_m_job_name="NS_BATCH_SG_PROD")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert merged.products[0].jobs[0].control_m_job_name == "NS_BATCH_SG_PROD"
    node = _nodes(merged, snap)["products[0].jobs[0]"]
    assert node.change == "update"
    assert node.previous == {"control_m_job_name": ""}


# ── the project row ──────────────────────────────────────────────────


def test_project_binding_is_never_silently_repointed():
    snap = _snapshot()
    doc = OnboardingDraftPayload(project=ProjectDraft(
        name="Inventory Scoring", cml_project_name="some-other-project",
        prod_stat_url="https://prod-stat/view/forecast",
    ))
    merged = merge_into_snapshot(doc, snap)

    assert merged.project.cml_project_name == "northstar-forecast"
    assert any("some-other-project" in w for w in merged.warnings)
    # A blank field, on the other hand, is filled — and reported as a change.
    assert merged.project.prod_stat_url == "https://prod-stat/view/forecast"
    assert _nodes(merged, snap)["project"].previous == {"prod_stat_url": ""}


def test_project_name_is_taken_from_the_platform_not_the_document():
    snap = _snapshot()
    doc = OnboardingDraftPayload(project=ProjectDraft(name="NS Handover v3"))
    merged = merge_into_snapshot(doc, snap)

    assert merged.project.name == "Inventory Scoring"
    assert _nodes(merged, snap)["project"].change == "unchanged"


# ── data fields + the diff the UI renders ────────────────────────────


def test_changed_data_fields_are_reported_with_their_current_value():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(
            cml_job_name="ns_batch_sg",
            schedule_cron="30 3 * * 1-5",
            owner_contact="new@example.com",
            description="Runs the SG screening batch",
        )],
    )])
    merged = merge_into_snapshot(doc, snap)
    node = _nodes(merged, snap)["products[0].jobs[0]"]

    assert node.existing_id == 21
    assert node.change == "update"
    assert node.previous == {
        "schedule_cron": "0 2 * * 6",
        "owner_contact": "old@example.com",
        "description": "",
    }
    # Fields the document is silent about keep the platform's value.
    assert merged.products[0].jobs[0].control_m_cron == "0 2 * * 6"


def test_a_document_that_says_nothing_new_produces_no_changes():
    snap = _snapshot()
    merged = merge_into_snapshot(OnboardingDraftPayload(), snap)
    nodes = _nodes(merged, snap)

    assert all(n.change == "unchanged" for n in nodes.values())
    assert all(n.previous == {} for n in nodes.values())


def test_product_is_flagged_when_only_a_child_changed():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening", jobs=[JobDraft(cml_job_name="ns_batch_sg", owner_contact="new@example.com")],
    )])
    nodes = _nodes(merge_into_snapshot(doc, snap), snap)

    assert nodes["products[0]"].change == "update"
    assert nodes["products[0]"].existing_id == 11


# ── scenarios ────────────────────────────────────────────────────────


def test_scenario_matches_by_name_then_type():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(cml_job_name="ns_batch_sg", scenarios=[
            JobScenarioDraft(
                scenario_type="not_triggered", scenario_name="Missed run",
                action_steps=["Check Control-M", "Rerun"],
            ),
            JobScenarioDraft(scenario_type="triggered_but_failed", scenario_name="Crashed"),
        ])],
    )])
    merged = merge_into_snapshot(doc, snap)
    scenarios = merged.products[0].jobs[0].scenarios
    nodes = _nodes(merged, snap)

    assert len(scenarios) == 2
    assert scenarios[0].action_steps == ["Check Control-M", "Rerun"]
    assert nodes["products[0].jobs[0].scenarios[0]"].existing_id == 31
    assert nodes["products[0].jobs[0].scenarios[0]"].previous == {"action_steps": "Old step"}
    assert nodes["products[0].jobs[0].scenarios[1]"].change == "new"
    # A changed scenario makes its owning job (and product) count as changed.
    assert nodes["products[0].jobs[0]"].change == "update"


def test_an_authored_email_template_is_never_replaced_by_the_document():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(cml_job_name="ns_batch_sg", scenarios=[JobScenarioDraft(
            scenario_type="not_triggered", scenario_name="Missed run",
            email_template=EmailTemplateDraft(subject="Generated subject"),
        )])],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert merged.products[0].jobs[0].scenarios[0].email_template.subject == "Existing subject"


def test_an_empty_email_template_is_filled_and_flagged():
    snap = _snapshot()
    snap.payload.products[0].jobs[0].scenarios[0].email_template = EmailTemplateDraft()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(cml_job_name="ns_batch_sg", scenarios=[JobScenarioDraft(
            scenario_type="not_triggered", scenario_name="Missed run",
            email_template=EmailTemplateDraft(subject="Generated subject"),
        )])],
    )])
    merged = merge_into_snapshot(doc, snap)
    node = _nodes(merged, snap)["products[0].jobs[0].scenarios[0]"]

    assert merged.products[0].jobs[0].scenarios[0].email_template.subject == "Generated subject"
    assert node.previous == {"email_template": "(no template)"}


def test_app_scenarios_are_added_to_a_matched_app():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        apps=[AppDraft(cml_application_name="forecast-ui", scenarios=[AppScenarioDraft(
            scenario_type="offline", scenario_name="Down", action_steps=["Restart"],
        )])],
    )])
    merged = merge_into_snapshot(doc, snap)
    nodes = _nodes(merged, snap)

    assert len(merged.products[0].apps[0].scenarios) == 1
    assert nodes["products[0].apps[0].scenarios[0]"].change == "new"
    assert nodes["products[0].apps[0]"].change == "update"


# ── the asset-level CML override sentinel ────────────────────────────


def test_new_asset_inherits_the_project_binding_instead_of_overriding_it():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(cml_job_name="ns_batch_my", cml_project_name="northstar-forecast")],
    )])
    merged = merge_into_snapshot(doc, snap)

    # Empty = inherit from the parent project (the platform's sentinel).
    assert merged.products[0].jobs[1].cml_project_name == ""


def test_a_genuine_override_on_a_new_asset_is_kept():
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(cml_job_name="train_job", cml_project_name="material-classifier")],
    )])
    merged = merge_into_snapshot(doc, snap)

    assert merged.products[0].jobs[1].cml_project_name == "material-classifier"


# ── re-running the diff must be stable ───────────────────────────────


def test_annotating_the_snapshot_itself_reports_nothing_to_do():
    """The reviewer can save repeatedly without the diff drifting."""
    snap = _snapshot()
    nodes = _nodes(snap.payload.model_copy(deep=True), snap)

    assert {n.change for n in nodes.values()} == {"unchanged"}
    assert nodes["products[0].jobs[0]"].existing_id == 21
    assert nodes["products[0].apps[0]"].existing_id == 41


def test_controlm_request_fields_survive_the_merge_into_a_new_asset():
    """A doc that adds a new job with Control-M rerun-request identifiers must
    carry them onto the merged payload so the (later) email composition can fold
    them into that job's scenario bodies."""
    snap = _snapshot()
    doc = OnboardingDraftPayload(products=[ProductDraft(
        name="Screening",
        jobs=[JobDraft(
            cml_job_name="ns_batch_my",
            control_m_application="APPL_CML_NS",
            control_m_table="TBL_01_DEMO_DS_NS",
            change_number="CHG00000000001",
            scenarios=[JobScenarioDraft(scenario_type="triggered_but_failed", scenario_name="F")],
        )],
    )])
    merged = merge_into_snapshot(doc, snap)
    new_job = merged.products[0].jobs[1]

    assert new_job.cml_job_name == "ns_batch_my"
    assert new_job.control_m_application == "APPL_CML_NS"
    assert new_job.control_m_table == "TBL_01_DEMO_DS_NS"
    assert new_job.change_number == "CHG00000000001"

    inject_email_actions(merged, only_paths={"products[0].jobs[1]"})
    body = new_job.scenarios[0].email_template.body.replace("\xa0", " ")
    assert "APPL_CML_NS" in body and "TBL_01_DEMO_DS_NS" in body
    assert "CHG00000000001" in body
    assert new_job.control_m_application == ""  # cleared after composing


def test_scoped_email_injection_leaves_untouched_runbooks_alone():
    """The email fill must not turn every untouched scenario into a change —
    that is what draft_service scopes it with (see _touched_paths)."""
    snap = _snapshot()
    snap.payload.products[0].jobs[0].scenarios[0].email_template = EmailTemplateDraft()
    doc = OnboardingDraftPayload(
        project=ProjectDraft(owner_name="Alex Morgan"),
        products=[ProductDraft(name="Screening", jobs=[JobDraft(
            cml_job_name="ns_batch_my",
            scenarios=[JobScenarioDraft(scenario_type="not_triggered", scenario_name="Missed")],
        )])],
    )
    merged = merge_into_snapshot(doc, snap)
    touched = {
        path for path, node in annotate(merged, snap).nodes.items()
        if node.change != "unchanged" and ".scenarios[" not in path and path != "project"
    }
    inject_email_actions(merged, only_paths=touched)

    # The new job got its template and send step…
    assert not merged.products[0].jobs[1].scenarios[0].email_template.is_empty
    assert merged.products[0].jobs[1].owner_contact == "AlexMorgan@example.com"
    # …and the existing one the document never mentioned is untouched, so it
    # stays "unchanged" after the fill.
    assert merged.products[0].jobs[0].scenarios[0].email_template.is_empty
    assert merged.products[0].jobs[0].scenarios[0].action_steps == ["Old step"]
    assert merged.products[0].jobs[0].owner_contact == "old@example.com"
    assert _nodes(merged, snap)["products[0].jobs[0]"].change == "unchanged"
