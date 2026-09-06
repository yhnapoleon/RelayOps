---
tab: schedules
title: Admin Panel · Schedules
---

## purpose
Schedules (the "Duty Schedule") manages Ops on-duty coverage — who is on call
for a time range — so the platform knows who newly created issues are assigned
to. All times are Singapore (SGT) wall-clock. Ops members reach this as a
standalone "Schedules" page in the sidebar (not inside the Admin Panel) and can
put themselves or any other Ops member on duty; admins reach the same view from
the Ops Admin Panel. Only admins and Ops members can be assigned on duty.

## layout
- Header "Duty Schedule" and a banner showing who is currently on duty (green)
  or that no one is (amber).
- Left card "Assign Duty": Start Time / End Time (datetime), Assignee, Duty Role
  (Primary / Secondary / Shadow), an optional Note, and an "Assign Duty" button.
- Right card "Schedule (N)": a Calendar / List toggle. In calendar view, color =
  duty role and name = assignee; click a day to pre-fill the assign form, click
  a bar to edit that entry. The list view shows all duty periods newest first,
  each editable and deletable.

## flow
1. In Assign Duty, pick Start/End time, an Assignee, a Duty Role, and an
   optional note, then click Assign Duty. (Back-dating the start hands that
   member every still-open issue raised since then.)
2. Use the Calendar/List toggle to view coverage; click a calendar day to
   pre-fill, or a bar to edit.
3. Edit or Delete an existing entry from the list.

## buttons
- **Assign Duty** — creates a duty schedule entry for the selected member and time range.
- **Calendar** — switches the schedule to the calendar view.
- **List** — switches the schedule to the list view.
- **Edit** — opens an existing entry for editing (assignee / duty role / note).
- **Delete** — removes a duty schedule entry.

## faq
- Q: How do I assign someone to on-call duty?
- Q: Can a Ops member (not just an admin) manage the duty schedule?
- Q: Who can be put on duty?
- Q: What do the duty roles (Primary / Secondary / Shadow) mean?
- Q: What happens if I back-date the start time?
- Q: How do I edit or delete an existing duty entry?

## coach:default
Duty Schedule manages Ops on-call coverage. Use Assign Duty to add a member for
a time range, and the Calendar/List toggle to view and edit coverage.
