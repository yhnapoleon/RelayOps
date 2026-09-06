---
tab: relayops-admin-handover
title: Admin Panel · Handover Approval
---

## purpose
Handover Approval (Ops Admin Panel, handover mode) is where admins review
versioned handover submissions and approve or reject them. Approving makes that
version the active Ops baseline.

## layout
- Header "Ops Admin Panel" with the handover subtitle.
- A "Pending Handover Reviews" card with a table: Product / Version / Submitted
  By / … and per-row Approve and Reject actions. Reject opens a "Reject
  Handover" dialog for a reason. Empty state: "No pending handover reviews".

## flow
1. Review a pending submission's product, version, and submitter.
2. Approve it to make that version the active Ops baseline, or
3. Reject it and enter a reason to send it back for changes.

## buttons
- **Approve** — accepts the submission and makes that version the active baseline.
- **Reject** — opens the Reject Handover dialog to send it back with a reason.

## faq
- Q: How do I approve a handover submission?
- Q: How do I reject a handover and give a reason?
- Q: What does approving a handover actually do?

## coach:default
Handover Approval lists pending submissions. Review one, then Approve it to make
it the active baseline, or Reject it with a reason.
