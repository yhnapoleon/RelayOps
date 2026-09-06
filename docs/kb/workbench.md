---
tab: workbench
title: Issue Workbench
sub_views: [page1, page2]
---

## purpose
The Issue Workbench is where a Ops member investigates and resolves a single
issue end to end: read the alert, pick the matching runbook scenario, run AI
diagnosis, execute and record the handling steps, escalate or return to the
owner if needed, and finally resolve or mark it a false positive — all inside
one two-page flow.

## layout
- Header: the Product / Project name, an "Open in MMP" button for MMP-backed
  issues, and a "Back to Actions" button.
- Issue summary card: #id, title, status and type badges, endpoint link, and
  Entity / Owner / Support Group / Owner Group / Assigned Via / Owner Contact,
  plus "Open Project" and "Open Product" buttons.
- Page 1 (Runbook Scenarios / Triage): the scenario picker.
- Page 2 (Execution): AI diagnosis panel, Record Step, Verification, Escalate /
  Return, Resolve, False Positive, and an Execution Timeline on the right.

## flow: page1
1. Read the issue summary and the Runbook Scenarios list. The closest match is
   highlighted "✓ Matched", but any configured scenario (even inactive ones)
   can be picked based on your needs.
2. Click a scenario to load its Diagnostic / Action / Verification steps; use
   "Change scenario" to pick a different one.
3. Click "Start Working" (or "Continue Working" if already in progress) to
   record work-started and advance to Page 2.
   (If no scenario is configured, return the issue to the product owner.)

## flow: page2
1. Review the AI diagnosis panel for a suggested root cause.
2. In Record Step, the selected scenario's steps appear as one-click
   "Use D1: …" (diagnostic) and "Use A1: …" (action) buttons — click one to
   fill it into the Record Step title/notes, edit if needed, then click
   "Record Step" to log it to the timeline. A "Sends email" action step also
   shows a mail button that opens Outlook prefilled from the scenario template.
3. Use Escalate (with a target) or Return to Product Owner when needed; Contact
   Owner emails the asset owner directly.
4. When you've recorded an action step (or escalated/returned), "Resolve Issue"
   unlocks — resolve with the auto-built conclusion, or use "Mark False
   Positive" (with a reason) instead.

## buttons
- **Back to Actions** — closes the workbench and returns to My Actions.
- **Open in MMP** — deep-links to the MMP platform for MMP-backed issues.
- **Open Project** — navigates to the issue's project.
- **Open Product** — navigates to the issue's product.
- **Change scenario** — clears the selected scenario to pick another.
- **Start Working** — records work-started and advances to Page 2 (Execution).
- **Continue Working** — same as Start Working for an already in-progress issue.
- **Back to runbook** — returns from Page 2 to the Page 1 scenario picker.
- **Record Step** — logs the current step title/notes to the action timeline.
- **Escalate** — records an escalation to the entered target/contact.
- **Return to Product Owner** — returns the issue to the product owner (status back to open).
- **Contact Owner** — opens a mailto to the asset owner (disabled without a valid email).
- **Resolve Issue** — closes the issue with a final conclusion (unlocks after a recorded step or escalation/return).
- **Mark False Positive** — closes the issue as a false positive with a reason.

## faq
- Q: How do I pick a runbook scenario?
- Q: How do I quickly fill a scenario step into Record Step?
- Q: Why is the Resolve Issue button disabled?
- Q: What's the difference between Resolve and Mark False Positive?
- Q: How do I escalate or return an issue to the owner?

## coach:default
Welcome to the Issue Workbench. Page 1 is triage — read the issue and pick the
runbook scenario whose steps match your needs, then Start Working to execute
them on Page 2.

## coach:page1
Tip: In Runbook Scenarios you can pick any preset scenario (the "✓ Matched" one
is recommended) to load its Diagnostic / Action / Verification steps — no need
to write steps from scratch. Then click Start Working.

## coach:page2
Tip: Click "AI Diagnose" to get a concise, grounded brief here — similar past
cases and recommended action steps for this issue. The scenario's steps also
appear as one-click "Use D1: …" / "Use A1: …" buttons that fill Record Step;
Resolve Issue unlocks once you've recorded a step or escalated/returned.
