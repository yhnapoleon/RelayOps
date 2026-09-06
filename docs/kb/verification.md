---
tab: verification
title: Verification
---

## purpose
Verification is the consolidated console for every saved API check across all
products. Run one product's checks or every check at once — typically during
CML downtime sign-off or operational validation — then Generate Report to save
the outcome and notify Ops + Admin.

## layout
- Top card: title "Verification", a toolbar with Reload / Run all verification /
  View Reports / Generate Report buttons, a filter box (by project, product, or
  check name), and Total / Pass / Fail / Running count badges.
- A "last report saved" banner appears after you generate a report, with a View
  Report link.
- Below: checks grouped by project → product. Each product row has a "Run
  verification" button and lists its individual checks with a per-row Run
  button and a pass/fail/running status.

## flow
1. (Optional) Filter to the project/product/check you care about; Reload to
   refresh the catalog.
2. Run a single check (per-row), a whole product (Run verification), or
   everything (Run all verification) to see live pass/fail.
3. Generate Report to run every check server-side, save a VerificationReport,
   and send a sign-off notification to Ops + Admin.
4. Use View Reports (or View Report on the banner) to review saved reports under
   Analytics → Verification Report.

## buttons
- **Reload** — reloads the check catalog (products → apps → checks).
- **Run all verification** — runs every check across all products, live in the page.
- **Run verification** — runs all checks for one product.
- **Run** — runs a single API check row.
- **Generate Report** — runs every check server-side, saves a report, and notifies Ops + Admin (opens a notes dialog first).
- **View Reports** — jumps to Analytics → Verification Report to see saved reports.
- **View Report** — opens the just-saved report's detail.

## faq
- Q: How do I run verification for one product?
- Q: How do I run every check at once?
- Q: What's the difference between Run all verification and Generate Report?
- Q: Where do saved verification reports live?

## coach:default
Verification runs your saved API checks for CML downtime sign-off. Run a single
product or Run all verification for a live check, then Generate Report to save
the outcome and notify Ops + Admin.
