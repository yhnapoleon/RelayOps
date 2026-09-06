---
tab: relayops-admin-users
title: Admin Panel · User Management
---

## purpose
User Management (admin only) changes a user's global login role — Admin / Ops
Member / Regular User. The new role is pinned so the next LDAP sync won't undo
it. This is separate from per-project roles, which live in each project's
Members dialog.

## layout
- Header: "User Management" with count badges ("N users", "N admins").
- A search box (by username or display name; debounced).
- A table with columns User / Username / Current Role / Projects / Change Role /
  Action. Change Role is a dropdown (Admin / Ops Member / Regular User); Action
  is an Apply button that commits the staged role. Platform owners (pinned via
  config) and the last remaining admin are protected and can't be changed.

## flow
1. Search for the user.
2. Pick a new global role in that row's Change Role dropdown.
3. Click Apply and confirm. (Changing your own account to a non-admin role logs
   you out immediately.)

## buttons
- **Apply** — commits the selected global role for that user (confirmation required).

## faq
- Q: How do I change a user's global role?
- Q: What's the difference between a global role and a project role?
- Q: Why is the Apply button disabled for some users?
- Q: Does demoting a user remove them from their projects?

## coach:default
User Management (admin only) sets platform-level global roles. Search a user,
pick a role in Change Role, and Apply. This is separate from per-project roles.
