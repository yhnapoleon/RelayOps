---
tab: projects
title: My Projects
sub_views: [projects, products, assets]
---

## purpose
My Projects is where you create and manage the whole asset hierarchy: projects
→ products → jobs / applications / recovery scenarios / API checks, plus project
members, CML/MMP bindings, and handover submissions. It has three levels: the
project list, a project's products, and a product's assets.

## layout
- Projects list: a filter/search bar and project cards. Each card shows CML/MMP
  binding badges and opens the project. A "New Project" button (top right) opens
  the Create New Project dialog.
- Project view (products): the selected project's products, with a Members
  control, project Edit/Copy/Delete, and a handover submission entry. "Create
  Product" adds a product.
- Product view (assets): the product's Jobs and Applications, each with Add Job
  / Add App, per-asset Scenarios and (for apps) API Checks, and Edit/Delete.
  This is where the full Scope Editor and per-scenario runbook pages open.

## flow: projects
1. Click New Project, fill in Project Name, Description, and optionally bind a
   CML Project and/or MMP Project, then Create Project.
2. Use the filter/search bar to find a project; open a card to manage it.

## flow: products
1. Open a project to see its products.
2. Click Create Product to add one; open a product to manage its assets.
3. Use Members to manage access and Transfer Business Owner to hand over
   ownership; when ready, submit the project version for handover review
   (Submit for Review).

## flow: assets
1. In a product, use Add Job / Add App to add assets and fill in their CML/MMP
   bindings and schedules.
2. Configure each asset's runbook via Scenarios (diagnostic/action/verification
   steps) using Add / Manage; for apps, configure API Checks.
3. Save Changes on each asset. Complete assets improve handover readiness.

## buttons
- **New Project** — opens the Create New Project dialog.
- **Create Project** — creates the project (name, description, optional CML/MMP binding).
- **Create Product** — adds a product under the open project.
- **Add Job** — adds a job (scheduled/CML-monitored asset) to the product.
- **Add App** — adds an application (health-monitored asset) to the product.
- **Scenarios** — opens the asset's failure/recovery scenario runbook.
- **Add / Manage** — creates/edits scenarios (or API checks) for the asset.
- **API Checks** — opens the app's Postman-style API check suite.
- **Transfer Business Owner** — hands project ownership to another user (owner only).
- **Submit for Review** — submits the project version for handover approval.
- **Save Changes** — saves edits to a product/asset.

## faq
- Q: How do I create a new project?
- Q: How do I bind a project to CML or MMP?
- Q: How do I add a job or application to a product?
- Q: How do I configure a runbook scenario for an asset?
- Q: How do I add a member or transfer the Business Owner?
- Q: How do I submit a project for handover review?

## coach:default
My Projects manages projects → products → jobs/apps/scenarios. Use New Project to
start, open a project to add products, and open a product to configure assets,
scenarios, and API checks — then Submit for Review to hand over.

## coach:products
Tip: Open a product to manage its jobs and apps. Use Members to manage access and
Transfer Business Owner for ownership; Submit for Review when the project is ready.

## coach:assets
Tip: Add Job / Add App to add assets, then use Scenarios (Add / Manage) to build
each asset's runbook and API Checks for apps. Save Changes as you go.
