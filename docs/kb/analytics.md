---
tab: analytics
title: Analytics
---

## purpose
Analytics is the operational reporting and trends view: issue volume by
type/status, SLA performance, false-positive rate, product-health scoring with
tunable anomaly rules, period-over-period comparison, and saved Verification
Reports. Everyone can see it; the anomaly-rule tuning is aimed at admins.

## layout
- A period selector: Month or Week granularity, with year/month or week-start
  pickers — every section is scoped to the chosen period.
- Issue analytics: volume and breakdowns by type/status, with drilldowns; an
  issue list with search and status/type filters (rows can open the Workbench).
- Product Health: a sortable table (product, project, severity, runs, failures,
  failure rate, open issues, repeat-failure streak, anomaly score) plus an
  Anomaly Queue; an "Anomaly Rule Tuning" card lets you adjust the thresholds
  (min runs, failure-rate %, repeat-failure streak, open issues, recent-failure
  hours) and apply them.
- CSV export controls for the project / product / all views.
- A "Verification Report" section listing saved verification reports (the
  Verification tab's View Reports jumps here).

## flow
1. Pick Month or Week and the specific period.
2. Read issue volume, SLA, and false-positive trends; drill into a type,
   project, or product.
3. Review Product Health; sort by anomaly score, and optionally tune the anomaly
   rules and apply them.
4. Export a view to CSV, or open a saved Verification Report.

## buttons
- **Month** — sets monthly granularity for the period.
- **Week** — sets weekly granularity for the period.
- **Export** — downloads the current view (project / product / all) as CSV.
- **Apply** — applies the edited anomaly-rule thresholds to the health view.

## faq
- Q: How do I switch between monthly and weekly analytics?
- Q: How is a product's health severity / anomaly score calculated?
- Q: How do I tune the anomaly rules?
- Q: How do I export analytics to CSV?
- Q: Where do I find saved verification reports?

## coach:default
Analytics shows issue, SLA, and product-health trends. Pick Month or Week, drill
into any breakdown, tune the anomaly rules for health scoring, or export to CSV.
