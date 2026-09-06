---
tab: relayops-admin-issues
title: Admin Panel · Issue Management
---

## purpose
Issue Management (Ops Admin Panel, issues mode) is the admin view of issues
across all projects, with lifecycle audit trails: manage outstanding issues,
reassign the handler, and review the full history.

## layout
- Header "Ops Admin Panel".
- An "Issue Management" card with a view toggle: Outstanding Issues / Resolved /
  All Issue History.
- Outstanding view is a table: ID / Issue / Type / Assigned To / Status /
  Timeline / SLA Deadline / Management. Each row has a View Timeline button, a
  Details button, and a reassign control. Resolved / History views show the
  lifecycle audit records.

## flow
1. Use the Outstanding / Resolved / All Issue History toggle to pick a view.
2. On an outstanding row, open View Timeline for the audit trail or Details for
   the full issue.
3. Reassign the handler when an issue needs a different owner.

## buttons
- **Outstanding Issues** — shows currently open/in-progress issues.
- **Resolved** — shows resolved issues only.
- **All Issue History** — shows the full issue history with audit details.
- **View Timeline** — opens the issue's lifecycle audit timeline.
- **Details** — opens the full issue detail dialog.

## faq
- Q: How do I see all issues across projects?
- Q: How do I reassign an issue to another handler?
- Q: Where do I see an issue's audit timeline?
- Q: What's the difference between the Outstanding, Resolved, and History views?

## coach:default
Issue Management shows issues across all projects. Use the Outstanding / Resolved
/ All Issue History toggle, then View Timeline or Details, and reassign handlers
as needed.
