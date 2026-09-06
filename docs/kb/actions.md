---
tab: actions
title: My Actions
---

## purpose
My Actions ("My Outstanding Actions") is your personal queue of the issues
assigned to you. It is a stable page available to every user; it stays empty
until something is assigned to you. You become an assignee by being put on
duty, by an admin assigning you, or by claiming an issue yourself from the
Open Issues board (which project-level Ops members can do for their projects).
You triage each one, jump into the Issue Workbench to work it, and keep the
timeline moving until it's resolved. Handover-review items are excluded here —
those live in the Admin Panel.

## layout
- Header: the title with two count badges — "N Open" and "N Resolved".
- Open issues: one card each (red left border, yellow when in progress) showing
  the issue #id, title, status and type badges, distilled key facts, the
  project/product context line, created/owner/support-group/assignee metadata,
  and — on the right — an SLA Countdown clock plus the action buttons.
- Recently Resolved: a collapsed section under the open cards listing up to 5
  recently resolved/closed issues, each with a Details button.
- "All Caught Up!" empty state when nothing is assigned to you.

## flow
1. Scan your open issue cards; watch the SLA Countdown on time-critical ones.
2. Open Workbench on an issue to diagnose and record handling (opens in a new
   window/tab), or click Start Working to mark it in progress without opening it.
3. For MMP governance issues, use Open in MMP to act on the MMP platform, then
   the one-click resolve button to close it in Ops.
4. Use View Details for the full description; Contact Owner to email the asset
   owner.

## buttons
- **View Details** — opens the full issue detail dialog.
- **Open Workbench** — opens the Issue Workbench for this issue (new window) to diagnose and record handling.
- **Start Working** — marks an open issue in progress (start_working action) without opening the workbench.
- **Contact Owner** — opens a mailto to the asset's owner contact (disabled when there's no valid owner email).
- **Open in MMP** — deep-links to the MMP platform for MMP-backed issues.
- **Details** — opens the detail dialog for a recently resolved issue.

## faq
- Q: How do I start working on an issue?
- Q: What does the SLA Countdown show?
- Q: How do I resolve an MMP governance issue?
- Q: Why don't I see handover reviews here?

## coach:default
My Actions is your personal issue queue. Open Workbench to work an issue, or
Start Working to mark it in progress. Watch the SLA Countdown on urgent ones.
