---
tab: ai-assistant
title: AI Assistant
---

## purpose
The AI Assistant is the unified assistant: combined Q&A, diagnosis, onboarding,
write proposals, and guidance. Ask a question or describe what you want to do
and it routes to the right capability.

## layout
A split workspace — chat on the left; the onboarding wizard opens on the right
on demand or when the assistant proposes it. Toggles by the input box switch
Write mode and Onboarding; a Guide button explains the pages you can use.

## flow
1. Ask a question or describe what you want to do.
2. The assistant routes to query / diagnose / onboarding / write-proposal / guide.
3. For changes it only proposes; nothing is applied until you confirm.

## buttons
- **Guide** — lists the pages you can use and explains any one of them.
- **Write mode** — proposes changes (nothing applies until you confirm).
- **Onboarding** — opens the wizard to import a handover document.

## onboarding modes
The wizard's import card offers two modes, then uses the same review form for both.
- **Onboard a new project** — the document becomes a brand-new Ops project with
  its products, Jobs, Apps and scenarios. Finish with **Submit & create**.
- **Add assets to an existing project** — pick a project you can edit; the AI
  compares the document against what that project already has. Assets it does
  not have yet are marked **New**, ones whose data the document changes are
  marked **Updated** with the current value shown under each field, and the rest
  are **Unchanged** (shown for context; you can hide them). Finish with
  **Update the project**. Nothing is ever deleted — removing a row in the
  preview only drops it from the review. A Job's CML / Control-M name and an
  App's CML name are what the comparison matches on, so the import only fills
  them when they are blank; renaming one in the preview makes it a *new* asset
  rather than a rename.

## email templates
When a handover document includes a Control-M rerun/adhoc email template (the
"Example template — Sent to … for execution" block with Application / Group /
Table / Job / CHG-TSK Number rows), onboarding reads those identifiers and fills
them into each affected scenario's owner-notification email template, so the
draft email already carries the real `APPL_CML_…` / `GRP_…` / `TBL_…` values and
the change (CHG) number. Open a scenario's "Owner notification email template" to
see and edit the filled rows; a "Control-M details filled" tag flags scenarios
whose template picked these up from the document.

## faq
- Q: What can the AI Assistant do?
- Q: How do I propose a change with Write mode?
- Q: How do I import a handover document?
- Q: How do I add assets from a document to a project that already exists?
- Q: What do the New / Updated / Unchanged badges mean?
- Q: Does onboarding fill in the Control-M rerun email template (Application / Table / CHG number)?

## coach:default
The AI Assistant handles Q&A, diagnosis, onboarding, and change proposals. Just
ask, or use the Guide / Write mode / Onboarding toggles by the input box.
