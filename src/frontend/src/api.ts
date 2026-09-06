/**
 * Unified API client for RelayOps.
 *
 * Handles JWT token management, request/response intercepting, and error handling.
 * All business API calls go through this module.
 */

// ── Token management ─────────────────────────────────────────────────

const TOKEN_KEY = 'relayops_token';
const USER_KEY = 'relayops_user';

export interface AuthUser {
  user_id: number;
  username: string;
  role: string;
  display_name: string | null;
  email: string | null;
  groups: string[] | null;
  /** True when the user holds the relayops_member role on at least one project
   *  (per-project role, decoupled from the global `role`). Grants operational
   *  reach (My Actions / Open Issues) over the projects they're appointed to. */
  project_relayops_member?: boolean;
  /** Project ids where the user holds the per-project relayops_member role. The
   *  Open Issues board is scoped to these for a project-level Ops member. */
  project_relayops_member_project_ids?: number[];
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function getSavedUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function saveUser(user: AuthUser): void {
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

// ── Base fetch wrapper ───────────────────────────────────────────────

export class ApiError extends Error {
  status: number;
  detail: string;
  payload: unknown;

  constructor(status: number, detail: string, payload?: unknown) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.payload = payload;
  }
}

function summarizeApiDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>;
    const blockingItems = Array.isArray(record.blocking_items) ? record.blocking_items : [];
    if (blockingItems.length > 0) {
      const first = blockingItems[0] as Record<string, unknown>;
      const firstMessage = typeof first?.message === 'string' ? first.message : 'handover is incomplete';
      return `Handover blocked: ${firstMessage}`;
    }
    if (typeof record.message === 'string' && record.message.trim()) {
      return record.message;
    }
  }
  return fallback;
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> || {}),
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const res = await fetch(path, {
    ...options,
    headers,
  });

  // 204 No Content
  if (res.status === 204) {
    return undefined as unknown as T;
  }

  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`;
    let payload: unknown = null;
    try {
      const body = await res.json();
      payload = body;
      detail = summarizeApiDetail(body.detail, detail);
    } catch {
      // ignore parse errors
    }
    // Global 401 handling: any authenticated request that comes back
    // unauthorized — expired token, stale token from before a DB
    // rebuild, or a server-side credential check that rejected us —
    // means the local token is no good. Clear it and reload so the
    // user lands on the login screen instead of seeing a sequence of
    // confusing errors as more requests fire against the same dead
    // token. Skipped if there was no token in the first place (the
    // /login call itself returns 401 on bad creds — let that one
    // surface as a normal error to the login form).
    if (res.status === 401 && token) {
      clearToken();
      if (typeof window !== 'undefined') {
        window.location.reload();
      }
    }
    throw new ApiError(res.status, detail, payload);
  }

  return res.json();
}

// ── Auth API ─────────────────────────────────────────────────────────

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user_id: number;
  username: string;
  display_name: string | null;
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  const data = await apiFetch<LoginResponse>('/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  setToken(data.access_token);
  return data;
}

export async function logout(): Promise<void> {
  try {
    await apiFetch('/logout', { method: 'POST' });
  } catch {
    // ignore errors on logout
  }
  clearToken();
}

export async function getMe(): Promise<AuthUser> {
  return apiFetch<AuthUser>('/me');
}

export async function changePassword(current_password: string, new_password: string): Promise<void> {
  await apiFetch('/auth/password', { method: 'POST', body: JSON.stringify({ current_password, new_password }) });
  clearToken();
}

// ── Project API ──────────────────────────────────────────────────────

export interface ProjectData {
  id: number;
  name: string;
  description: string;
  owner_id: number;
  owner_group_id: number | null;
  owner_group_name: string | null;
  is_system: boolean;
  owner_username: string | null;
  owner_display_name: string | null;
  /** CML v2 binding — every Job/App under this Project inherits it. */
  cml_project_name: string;
  cml_project_id: string | null;
  cml_binding_error: string | null;
  cml_binding_status: 'resolved' | 'pending' | 'error' | 'unconfigured';
  /** Optional MMP project binding — stores project_repo_name like
   *  "demo-inventory-forecast@regional-demand". Independent of
   *  the CML binding above. When set, the Job MMP picker under this
   *  Project defaults to filtering on this binding. */
  mmp_project_id: string;
  /** Single prod-stat dashboard URL for the whole project (one repo). */
  prod_stat_url: string;
  /** Version-control / handover lifecycle (moved up from product level). */
  status: string;
  lifecycle_status: string;
  current_draft_version_id: number | null;
  current_draft_version_status: string | null;
  current_approved_version_id: number | null;
  current_approved_version_status: string | null;
  latest_version_number: string | null;
  created_at: string;
  updated_at: string | null;
}

export async function listProjects(): Promise<ProjectData[]> {
  return apiFetch<ProjectData[]>('/api/projects');
}

export async function createProject(
  name: string,
  description: string,
  cml_project_name?: string,
  cml_project_id?: string,
  prod_stat_url?: string,
  mmp_project_id?: string,
): Promise<ProjectData> {
  return apiFetch<ProjectData>('/api/projects', {
    method: 'POST',
    body: JSON.stringify({ name, description, cml_project_name, cml_project_id, prod_stat_url, mmp_project_id }),
  });
}

export async function updateProject(
  id: number,
  data: { name?: string; description?: string; cml_project_name?: string; cml_project_id?: string; prod_stat_url?: string; mmp_project_id?: string },
): Promise<ProjectData> {
  return apiFetch<ProjectData>(`/api/projects/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteProject(id: number): Promise<void> {
  await apiFetch(`/api/projects/${id}`, { method: 'DELETE' });
}

export async function copyProject(id: number): Promise<ProjectData> {
  return apiFetch<ProjectData>(`/api/projects/${id}/copy`, { method: 'POST' });
}

export async function resolveProjectBinding(id: number): Promise<ProjectData> {
  return apiFetch<ProjectData>(`/api/projects/${id}/resolve-binding`, { method: 'POST' });
}

export interface CmlResourceOption {
  id: string;
  name: string;
}

/** List CML jobs under a Ops project's bound CML project. Returns [] when
 * the project is unbound. Throws on 502 (CML unreachable) so the caller
 * can degrade the combobox to free-text input.
 *
 * Pass ``cmlProjectNameOverride`` to query a different CML project than
 * the Ops project's default binding (per-asset override).
 */
export async function listProjectCmlJobs(
  projectId: number,
  cmlProjectNameOverride?: string,
): Promise<CmlResourceOption[]> {
  const override = (cmlProjectNameOverride || '').trim();
  const qs = override ? `?cml_project_name=${encodeURIComponent(override)}` : '';
  return apiFetch<CmlResourceOption[]>(`/api/projects/${projectId}/cml-jobs${qs}`);
}

/** Richer per-app row used by the Application picker — carries subdomain
 *  and a server-composed serving URL so the form can auto-fill both. */
export interface CmlAppOption {
  id: string;
  name: string;
  subdomain: string | null;
  status: string | null;
  serving_url: string | null;
}

export async function listProjectCmlApps(
  projectId: number,
  cmlProjectNameOverride?: string,
): Promise<CmlAppOption[]> {
  const override = (cmlProjectNameOverride || '').trim();
  const qs = override ? `?cml_project_name=${encodeURIComponent(override)}` : '';
  return apiFetch<CmlAppOption[]>(`/api/projects/${projectId}/cml-apps${qs}`);
}

/** One row of the MMP model picker. The Job form keys on model_name (the
 *  primary visible label) and uses business_name / project_repo_name as
 *  secondary disambiguators. Picking a row writes BOTH Job.mmp_model_id
 *  (= model_name) and Job.mmp_project_id (= project_repo_name) at once. */
export interface MmpModelOption {
  model_name: string;
  project_repo_name: string;
  business_name: string | null;
  is_production: boolean;
}

/** Fetch the MMP model directory for the Job form's picker.
 *
 *  Default behaviour: when the parent Ops Project has an MMP binding
 *  (Project.mmp_project_id), the list is filtered server-side to just
 *  models under that MMP project. Pass ``includeAll = true`` to bypass
 *  the filter and get the full workspace directory (used when the user
 *  wants to pick a model outside the parent Project's MMP binding).
 *
 *  Returns [] when MMP isn't configured or the directory load fails so
 *  the form can degrade to free-text input. */
export async function listProjectMmpModels(
  projectId: number,
  includeAll: boolean = false,
): Promise<MmpModelOption[]> {
  const qs = includeAll ? '?include_all=true' : '';
  return apiFetch<MmpModelOption[]>(`/api/projects/${projectId}/mmp-models${qs}`);
}

/** One row of the MMP project picker (coarser than MmpModelOption — one
 *  row per MMP project). Used by the Project create/edit form so a Ops
 *  Project can be bound to an MMP project independently of its CML
 *  workspace binding. ``model_count`` lets the UI show "(3 models)". */
export interface MmpProjectOption {
  project_repo_name: string;
  business_name: string | null;
  model_count: number;
}

/** Workspace-wide MMP project picker — for the Project create form (no
 *  project_id needed because the Ops Project doesn't exist yet). The
 *  full MMP directory is ~30 projects so we fetch once and filter
 *  client-side. */
export async function listMmpProjects(): Promise<MmpProjectOption[]> {
  return apiFetch<MmpProjectOption[]>(`/api/projects/mmp-projects`);
}

/** Result of resolving a pasted MMP web link (…/project/<id>/…) into a
 *  binding. Auto-fills mmp_project_id (= project_repo_name) and, when there's
 *  a single production model, mmp_model_id (= suggested_model_name). */
export interface MmpUrlResolveResponse {
  project_id: number | null;
  project_repo_name: string | null;
  business_name: string | null;
  models: MmpModelOption[];
  suggested_model_name: string | null;
  error: string | null;
}

export async function resolveMmpUrl(url: string): Promise<MmpUrlResolveResponse> {
  return apiFetch<MmpUrlResolveResponse>(`/api/projects/mmp/resolve-url?url=${encodeURIComponent(url)}`);
}

export interface CmlProjectOption {
  id: string;
  name: string;
  owner_username: string | null;
  owner_email: string | null;
}

export interface CmlProjectSearchResponse {
  items: CmlProjectOption[];
  has_more: boolean;
}

/** Search CML projects visible to the Ops service identity. ``q`` is
 * forwarded to CML as a substring filter on project name. Returns the
 * first page plus a ``has_more`` flag the picker uses to decide whether
 * to require typing before showing results. Throws on 502 so the caller
 * can degrade to free-text input. */
export async function searchCmlProjects(q?: string): Promise<CmlProjectSearchResponse> {
  const qs = q && q.trim() ? `?q=${encodeURIComponent(q.trim())}` : '';
  return apiFetch<CmlProjectSearchResponse>(`/api/projects/cml/search${qs}`);
}

// ── Product API ──────────────────────────────────────────────────────

export interface ProductData {
  id: number;
  project_id: number;
  name: string;
  /** Lightweight status — gates monitoring (archived assets aren't checked).
   *  Version control / submit-draft lifecycle now lives on the project. */
  status: string;
  is_system: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface ProductVersionSummaryData {
  id: number;
  version_number: string;
  version_status: string;
  submitted_at: string | null;
  reviewed_at: string | null;
  rejection_reason: string | null;
}

export interface ProductVersionData {
  id: number;
  product_id: number;
  version_number: string;
  version_status: string;
  change_summary: string;
  snapshot_json: Record<string, unknown> | null;
  completeness_summary_json: Record<string, unknown> | null;
  submitted_by: number | null;
  submitted_at: string | null;
  reviewed_by: number | null;
  reviewed_at: string | null;
  rejection_reason: string | null;
  derived_from_version_id: number | null;
  created_at: string;
  updated_at: string | null;
}

export interface HandoverCompletenessItemData {
  code: string;
  severity: string;
  entity_type: string;
  entity_id: number | null;
  entity_label: string | null;
  // Populated for scenario-level items so the UI can show "in Job X" /
  // "in App Y" and jump straight into the parent's edit form. Null for
  // asset- and product-level items.
  parent_entity_type: string | null;
  parent_entity_id: number | null;
  parent_entity_label: string | null;
  // Project-level completeness adds these so the UI can show "in Product X"
  // context and jump to the right product. Null for per-product completeness.
  product_id?: number | null;
  product_label?: string | null;
  // Form field id the editor should scroll into view and outline in red
  // (e.g. 'action_steps', 'verification_steps', 'dependency_notes',
  // 'restart_summary'). Null when the missing item isn't tied to a single
  // field (e.g. "Job has no scenarios").
  field_code: string | null;
  message: string;
}

export interface HandoverCompletenessData {
  is_complete: boolean;
  blocking_items: HandoverCompletenessItemData[];
  summary: Record<string, unknown>;
}

export async function listProducts(projectId: number): Promise<ProductData[]> {
  return apiFetch<ProductData[]>(`/api/projects/${projectId}/products`);
}

export async function createProduct(projectId: number, name: string): Promise<ProductData> {
  return apiFetch<ProductData>(`/api/projects/${projectId}/products`, {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
}

export async function updateProduct(id: number, data: { name?: string }): Promise<ProductData> {
  return apiFetch<ProductData>(`/api/products/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteProduct(id: number): Promise<void> {
  await apiFetch(`/api/products/${id}`, { method: 'DELETE' });
}

export async function copyProduct(id: number): Promise<ProductData> {
  return apiFetch<ProductData>(`/api/products/${id}/copy`, { method: 'POST' });
}

/** Copy one send-email step's owner-email template onto many scenarios.
 *  `mode: 'fill_empty'` only sets it where no template exists yet ("set as
 *  default"); `mode: 'overwrite'` replaces every target ("replace all").
 *  `scope` is the whole product, or a single job/app (pass the matching id). */
export interface EmailTemplateApplyResult {
  updated: number;
  scope: 'product' | 'job' | 'app';
  mode: 'fill_empty' | 'overwrite';
}

export async function applyEmailTemplate(
  productId: number,
  body: {
    template: EmailTemplate;
    mode: 'fill_empty' | 'overwrite';
    scope: 'product' | 'job' | 'app';
    job_id?: number | null;
    application_id?: number | null;
  },
): Promise<EmailTemplateApplyResult> {
  return apiFetch<EmailTemplateApplyResult>(`/api/products/${productId}/email-template/apply`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Job API ──────────────────────────────────────────────────────────

export interface JobData {
  id: number;
  product_id: number;
  mmp_project_id: string;
  mmp_model_id: string;
  control_m_job_name: string;
  control_m_cron: string;
  /** CML v2 binding (user input + server-resolved ids). */
  cml_project_name: string;
  cml_job_name: string;
  cml_project_id: string | null;
  cml_job_id: string | null;
  cml_binding_error: string | null;
  /** Derived: 'resolved' (ids cached) | 'pending' (names set, ids not yet
   *  resolved) | 'error' (CML reachable but resolve failed) | 'unconfigured'. */
  cml_binding_status: 'resolved' | 'pending' | 'error' | 'unconfigured';
  schedule_cron: string;
  description: string;
  dependencies: Record<string, unknown> | null;
  failure_strategy_summary: string;
  dependency_notes: string;
  owner_contact: string;
  support_group_id: number | null;
  support_group_name: string | null;
  support_group: string;
  runbook_required: boolean;
  has_mmp_dependency: boolean | null;
  /** Per-job staleness strictness. Backend treats NULL as "normal" (default).
   *  When sla_custom_minutes is set, it wins over the preset-derived window. */
  sla_preset: 'strict' | 'normal' | 'loose' | null;
  sla_custom_minutes: number | null;
  is_system: boolean;
  /** Last time we polled CML for this job (live, periodic checker, or Check Now). */
  last_checked_at: string | null;
  created_at: string;
  updated_at: string | null;
}

/** Owner-email template prefilled into Outlook by the "send email to owner"
 *  action. All fields optional/free-text; `to` defaults to the job's
 *  owner_contact when blank. */
export interface EmailTemplate {
  to: string;
  cc: string;
  subject: string;
  body: string;
}

export interface JobFailureScenarioData {
  id: number;
  job_id: number;
  scenario_type: string;
  scenario_name: string;
  condition_description: string;
  detection_source: string;
  diagnostic_steps: unknown[] | null;
  action_steps: unknown[] | null;
  verification_steps: unknown[] | null;
  escalation_target: string;
  fallback_owner_type: string | null;
  threshold_operator: string;
  threshold_value: number | null;
  threshold_feature_list: string[] | null;
  email_template: EmailTemplate | null;
  is_not_applicable: boolean;
  not_applicable_signoff_by: number | null;
  not_applicable_signoff_at: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export async function listJobs(productId: number): Promise<JobData[]> {
  return apiFetch<JobData[]>(`/api/products/${productId}/jobs`);
}

export async function createJob(
  productId: number,
  data: {
    mmp_project_id?: string;
    mmp_model_id?: string;
    control_m_job_name?: string;
    control_m_cron?: string;
    cml_project_name?: string;
    cml_job_name?: string;
    schedule_cron?: string;
    description?: string;
    dependencies?: Record<string, unknown>;
    failure_strategy_summary?: string;
    dependency_notes?: string;
    owner_contact?: string;
    support_group_id?: number | null;
    support_group?: string;
    runbook_required?: boolean;
    has_mmp_dependency?: boolean | null;
    sla_preset?: 'strict' | 'normal' | 'loose' | null;
    sla_custom_minutes?: number | null;
  },
): Promise<JobData> {
  return apiFetch<JobData>(`/api/products/${productId}/jobs`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateJob(id: number, data: Partial<JobData>): Promise<JobData> {
  return apiFetch<JobData>(`/api/jobs/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteJob(id: number): Promise<void> {
  await apiFetch(`/api/jobs/${id}`, { method: 'DELETE' });
}

export interface JobDuplicateRequest {
  target_product_id: number;
  control_m_job_name: string;
  /** Null = copy all source scenarios; explicit array filters by source scenario id. */
  scenario_ids?: number[] | null;
}

export async function duplicateJob(jobId: number, data: JobDuplicateRequest): Promise<JobData> {
  return apiFetch<JobData>(`/api/jobs/${jobId}/duplicate`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function resolveJobBinding(id: number): Promise<JobData> {
  return apiFetch<JobData>(`/api/jobs/${id}/resolve-binding`, { method: 'POST' });
}

export async function listJobFailureScenarios(jobId: number): Promise<JobFailureScenarioData[]> {
  return apiFetch<JobFailureScenarioData[]>(`/api/jobs/${jobId}/failure-scenarios`);
}

export async function createJobFailureScenario(jobId: number, data: {
  scenario_type: string;
  scenario_name?: string;
  condition_description?: string;
  detection_source?: string;
  diagnostic_steps?: string[];
  action_steps?: string[];
  verification_steps?: string[];
  escalation_target?: string;
  fallback_owner_type?: string | null;
  threshold_operator?: string;
  threshold_value?: number | null;
  threshold_feature_list?: string[];
  email_template?: EmailTemplate | null;
  is_not_applicable?: boolean;
  is_active?: boolean;
}): Promise<JobFailureScenarioData> {
  return apiFetch<JobFailureScenarioData>(`/api/jobs/${jobId}/failure-scenarios`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateJobFailureScenario(id: number, data: Partial<JobFailureScenarioData>): Promise<JobFailureScenarioData> {
  return apiFetch<JobFailureScenarioData>(`/api/job-failure-scenarios/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteJobFailureScenario(id: number): Promise<void> {
  await apiFetch(`/api/job-failure-scenarios/${id}`, { method: 'DELETE' });
}

// ── Application API ──────────────────────────────────────────────────

/** CML probe contract — drives which serving-URL endpoints the AppInterface
 *  hits. Mirrors the backend `cml_app_type` regex. */
export type CmlAppType = 'fastapi' | 'runtime' | 'ray' | 'generic';

export interface AppData {
  id: number;
  product_id: number;
  application_url: string;
  health_check_url: string;
  /** CML v2 binding (user input + server-resolved ids + serving URL). */
  cml_project_name: string;
  cml_application_name: string;
  cml_subdomain: string;
  cml_app_type: CmlAppType;
  cml_project_id: string | null;
  cml_application_id: string | null;
  cml_serving_url: string;
  cml_binding_error: string | null;
  cml_binding_status: 'resolved' | 'pending' | 'error' | 'unconfigured';
  /** Latest AppChecker outcome — null until the first check has run. */
  last_cml_status: string | null;
  last_relayops_health: import('./types').RelayOpsHealth | null;
  last_checked_at: string | null;
  last_check_error: string | null;
  description: string;
  restart_supported: boolean;
  restart_summary: string;
  owner_contact: string;
  support_group_id: number | null;
  support_group_name: string | null;
  support_group: string;
  is_system: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface ApplicationRecoveryScenarioData {
  id: number;
  application_id: number;
  scenario_type: string;
  scenario_name: string;
  condition_description: string;
  action_steps: unknown[] | null;
  verification_steps: unknown[] | null;
  escalation_target: string;
  fallback_owner_type: string | null;
  email_template: EmailTemplate | null;
  is_not_applicable: boolean;
  not_applicable_signoff_by: number | null;
  not_applicable_signoff_at: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export type ApplicationVerificationMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD';

export interface ApplicationVerificationRequest {
  url: string;
  method?: ApplicationVerificationMethod;
  headers?: Record<string, string>;
  body?: unknown;
  timeout_seconds?: number;
  /** When set, the backend also resolves the CML application binding and
   *  returns the result alongside the URL probe. */
  project_id?: number;
  cml_application_name?: string;
  cml_subdomain?: string;
  /** Per-asset CML project override (matches the save semantics in
   *  app_service). Non-empty value targets this CML project instead of
   *  the owning Ops project's binding. */
  cml_project_name?: string;
}

export interface ApplicationVerificationResponse {
  ok: boolean;
  status_code: number | null;
  duration_ms: number;
  response_headers: Record<string, string>;
  response_body: string;
  error: string | null;
  /** Optional CML binding result (null when not requested by the client). */
  cml_binding_ok?: boolean | null;
  cml_application_id?: string | null;
  cml_binding_error?: string | null;
  /** Real CML app name when the binding resolved (by name OR subdomain) —
   *  used to reverse-fill the App Name field from a subdomain-only entry. */
  cml_resolved_application_name?: string | null;
  /** Cross-project rescue: when the subdomain wasn't under the bound CML
   *  project but exists elsewhere in the workspace, the project / app it
   *  actually belongs to. All null when no other-project match was found. */
  cml_other_project_id?: string | null;
  cml_other_project_name?: string | null;
  cml_other_application_id?: string | null;
  cml_other_application_name?: string | null;
  /** Summary of the workspace scan when a subdomain matched nowhere. */
  cml_scan_note?: string | null;
}

export interface JobVerificationRequest {
  /** Owning Ops Project id. When set without cml_project_name, the backend
   *  uses the project's bound CML project (matches the save path). */
  project_id?: number;
  /** Per-asset CML project override. Non-empty value targets this CML
   *  project instead of the owning Ops project's binding — matches the
   *  save semantics in job_service. Also serves as the legacy entry point
   *  when project_id is omitted. */
  cml_project_name?: string;
  cml_job_name?: string;
  /** Deprecated alias for cml_job_name. */
  control_m_job_name?: string;
  timeout_seconds?: number;
}

export interface JobVerificationResponse {
  ok: boolean;
  job_found: boolean;
  job_status: string | null;
  last_run: string | null;
  cml_project_id: string | null;
  cml_job_id: string | null;
  cml_run_id: string | null;
  cml_schedule: string | null;
  cml_timezone: string | null;
  duration_ms: number;
  error: string | null;
}

export async function runApplicationVerification(
  data: ApplicationVerificationRequest,
): Promise<ApplicationVerificationResponse> {
  return apiFetch<ApplicationVerificationResponse>('/api/apps/verification/run', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/** Binding-only probe (no HTTP URL needed) — backs the Subdomain "Fetch"
 *  button. Reverse-fills the real CML app name and reports the owning project
 *  when the subdomain lives under a different CML project. */
export interface ApplicationBindingProbeRequest {
  project_id?: number;
  cml_project_name?: string;
  cml_application_name?: string;
  cml_subdomain?: string;
}

export interface ApplicationBindingProbeResponse {
  cml_binding_ok?: boolean | null;
  cml_application_id?: string | null;
  cml_binding_error?: string | null;
  cml_resolved_application_name?: string | null;
  cml_other_project_id?: string | null;
  cml_other_project_name?: string | null;
  cml_other_application_id?: string | null;
  cml_other_application_name?: string | null;
  /** Summary of the workspace scan when a subdomain matched nowhere. */
  cml_scan_note?: string | null;
}

export async function probeApplicationBinding(
  data: ApplicationBindingProbeRequest,
): Promise<ApplicationBindingProbeResponse> {
  return apiFetch<ApplicationBindingProbeResponse>('/api/apps/binding/probe', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function runJobVerification(
  data: JobVerificationRequest,
): Promise<JobVerificationResponse> {
  return apiFetch<JobVerificationResponse>('/api/jobs/verification/run', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/** MMP connectivity / binding verification — Phase 8 Validate button.
 *
 *  Pass nothing → only tests that the workspace directory loads.
 *  Pass mmp_project_id → also checks the project exists.
 *  Pass both → also checks the model exists + returns current drifted state. */
export interface MmpVerificationRequest {
  mmp_project_id?: string;
  mmp_model_id?: string;
}

export interface MmpVerificationResponse {
  ok: boolean;
  duration_ms: number;
  directory_loaded: boolean;
  total_projects: number;
  project_found: boolean | null;
  business_name: string | null;
  model_count: number | null;
  model_found: boolean | null;
  cml_model_id: number | null;
  drifted: boolean | null;
  drift_details: string | null;
  pending_approval: boolean | null;
  pending_approval_details: string | null;
  pending_review: boolean | null;
  pending_review_details: string | null;
  error: string | null;
}

export async function runMmpVerification(
  data: MmpVerificationRequest,
): Promise<MmpVerificationResponse> {
  return apiFetch<MmpVerificationResponse>('/api/mmp/verification/run', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function listApps(productId: number): Promise<AppData[]> {
  return apiFetch<AppData[]>(`/api/products/${productId}/apps`);
}

export async function createApp(
  productId: number,
  data: {
    application_url?: string;
    health_check_url?: string;
    cml_project_name?: string;
    cml_application_name?: string;
    cml_subdomain?: string;
    cml_app_type?: CmlAppType;
    cml_serving_url?: string;
    description?: string;
    restart_supported?: boolean;
    restart_summary?: string;
    owner_contact?: string;
    support_group_id?: number | null;
    support_group?: string;
  },
): Promise<AppData> {
  return apiFetch<AppData>(`/api/products/${productId}/apps`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateApp(id: number, data: Partial<AppData>): Promise<AppData> {
  return apiFetch<AppData>(`/api/apps/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteApp(id: number): Promise<void> {
  await apiFetch(`/api/apps/${id}`, { method: 'DELETE' });
}

export interface AppDuplicateRequest {
  target_product_id: number;
  cml_application_name: string;
  /** Null = copy all source recovery scenarios; explicit array filters by source scenario id. */
  recovery_scenario_ids?: number[] | null;
}

export async function duplicateApp(appId: number, data: AppDuplicateRequest): Promise<AppData> {
  return apiFetch<AppData>(`/api/apps/${appId}/duplicate`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function resolveAppBinding(id: number): Promise<AppData> {
  return apiFetch<AppData>(`/api/apps/${id}/resolve-binding`, { method: 'POST' });
}

export async function listApplicationRecoveryScenarios(appId: number): Promise<ApplicationRecoveryScenarioData[]> {
  return apiFetch<ApplicationRecoveryScenarioData[]>(`/api/apps/${appId}/recovery-scenarios`);
}

export async function createApplicationRecoveryScenario(appId: number, data: {
  scenario_type: string;
  scenario_name?: string;
  condition_description?: string;
  action_steps?: string[];
  verification_steps?: string[];
  escalation_target?: string;
  fallback_owner_type?: string | null;
  email_template?: EmailTemplate | null;
  is_not_applicable?: boolean;
  is_active?: boolean;
}): Promise<ApplicationRecoveryScenarioData> {
  return apiFetch<ApplicationRecoveryScenarioData>(`/api/apps/${appId}/recovery-scenarios`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateApplicationRecoveryScenario(id: number, data: Partial<ApplicationRecoveryScenarioData>): Promise<ApplicationRecoveryScenarioData> {
  return apiFetch<ApplicationRecoveryScenarioData>(`/api/application-recovery-scenarios/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteApplicationRecoveryScenario(id: number): Promise<void> {
  await apiFetch(`/api/application-recovery-scenarios/${id}`, { method: 'DELETE' });
}

// ── Application API Checks (persisted Postman-style validation) ──────

export interface ApplicationApiCheckData {
  id: number;
  application_id: number;
  collection_name: string;
  name: string;
  method: string;
  url: string;
  headers: Record<string, string>;
  body: string;
  body_is_json: boolean;
  timeout_seconds: number;
  expected_status: number | null;
  expected_body_contains: string | null;
  source: string;
  source_ref: string | null;
  is_active: boolean;
  sort_order: number;
  last_run_at: string | null;
  last_run_ok: boolean | null;
  last_run_status_code: number | null;
  last_run_duration_ms: number | null;
  created_at: string;
  updated_at: string | null;
  created_by: number | null;
  updated_by: number | null;
}

export interface ApplicationApiCheckCreateInput {
  name: string;
  collection_name?: string;
  method?: string;
  url: string;
  headers?: Record<string, string>;
  body?: string;
  body_is_json?: boolean;
  timeout_seconds?: number;
  expected_status?: number | null;
  expected_body_contains?: string | null;
  source?: 'manual' | 'swagger' | 'imported';
  source_ref?: string | null;
  is_active?: boolean;
  sort_order?: number;
}

export interface ApplicationApiCheckRunRecord {
  id: number;
  api_check_id: number;
  ran_at: string;
  ran_by: number | null;
  ok: boolean;
  status_code: number | null;
  duration_ms: number;
  response_headers: Record<string, string>;
  response_body: string;
  error: string | null;
  request_url: string | null;
  request_method: string | null;
}

export interface ApplicationApiCheckRunResult {
  run: ApplicationApiCheckRunRecord;
  assertion_passed: boolean | null;
  assertion_reason: string | null;
}

export async function listApplicationApiChecks(appId: number): Promise<ApplicationApiCheckData[]> {
  return apiFetch<ApplicationApiCheckData[]>(`/api/apps/${appId}/api-checks`);
}

export async function createApplicationApiCheck(
  appId: number,
  data: ApplicationApiCheckCreateInput,
): Promise<ApplicationApiCheckData> {
  return apiFetch<ApplicationApiCheckData>(`/api/apps/${appId}/api-checks`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateApplicationApiCheck(
  id: number,
  data: Partial<ApplicationApiCheckCreateInput>,
): Promise<ApplicationApiCheckData> {
  return apiFetch<ApplicationApiCheckData>(`/api/api-checks/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteApplicationApiCheck(id: number): Promise<void> {
  await apiFetch(`/api/api-checks/${id}`, { method: 'DELETE' });
}

export async function runApplicationApiCheck(id: number): Promise<ApplicationApiCheckRunResult> {
  return apiFetch<ApplicationApiCheckRunResult>(`/api/api-checks/${id}/run`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function listApplicationApiCheckRuns(
  id: number,
  limit = 20,
): Promise<ApplicationApiCheckRunRecord[]> {
  return apiFetch<ApplicationApiCheckRunRecord[]>(`/api/api-checks/${id}/runs?limit=${limit}`);
}

// ── Verification reports (Phase-2 of the Verification tab) ──────────
//
// Generate triggers a server-side 'Run All' across every active API
// check and persists the consolidated outcome as a VerificationReport
// row. The Analytics 'Verification Report' section reads the list
// endpoint; the Detail dialog reads the per-id endpoint on demand.

export interface VerificationReportEntryData {
  project_id: number | null;
  project_name: string;
  product_id: number | null;
  product_name: string;
  application_id: number | null;
  check_id: number;
  check_name: string;
  method: string;
  url: string;
  status: 'pass' | 'fail';
  status_code: number | null;
  duration_ms: number;
  error: string | null;
  assertion_reason: string | null;
  run_id: number | null;
}

export interface VerificationReportSummaryData {
  id: number;
  generated_at: string;
  generated_by: number | null;
  generated_by_name: string | null;
  total_count: number;
  pass_count: number;
  fail_count: number;
  duration_ms: number;
  notes: string;
}

export interface VerificationReportDetailData extends VerificationReportSummaryData {
  details: VerificationReportEntryData[];
}

export async function generateVerificationReport(
  notes?: string,
): Promise<VerificationReportDetailData> {
  return apiFetch<VerificationReportDetailData>(`/api/verification/reports/generate`, {
    method: 'POST',
    body: JSON.stringify(notes ? { notes } : {}),
  });
}

export async function listVerificationReports(
  limit = 50,
): Promise<VerificationReportSummaryData[]> {
  return apiFetch<VerificationReportSummaryData[]>(`/api/verification/reports?limit=${limit}`);
}

export async function getVerificationReport(
  id: number,
): Promise<VerificationReportDetailData> {
  return apiFetch<VerificationReportDetailData>(`/api/verification/reports/${id}`);
}

export async function deleteVerificationReport(id: number): Promise<void> {
  await apiFetch(`/api/verification/reports/${id}`, { method: 'DELETE' });
}

// ── Issue API ────────────────────────────────────────────────────────

export interface IssueData {
  id: number;
  type: string;
  status: string;
  title: string;
  description: string;
  project_id: number | null;
  project_name: string | null;
  project_is_system: boolean;
  product_id: number | null;
  product_version_id: number | null;
  product_version_number: string | null;
  product_version_status: string | null;
  project_version_id: number | null;
  project_version_number: string | null;
  project_version_status: string | null;
  product_name: string | null;
  product_is_system: boolean;
  job_id: number | null;
  job_name: string | null;
  app_id: number | null;
  app_name: string | null;
  /** Owner email of the underlying asset (Job/App); powers Contact Owner. */
  owner_contact: string | null;
  created_by: number;
  assignee_id: number | null;
  support_group_id: number | null;
  support_group_name: string | null;
  owner_group_id: number | null;
  owner_group_name: string | null;
  assigned_via: string | null;
  resolution_description: string | null;
  rejection_reason: string | null;
  selected_scenario_type: string | null;
  selected_scenario_name: string | null;
  external_url: string | null;
  action_summary_json: Record<string, unknown> | null;
  resolution_summary_json: Record<string, unknown> | null;
  sla_deadline: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string | null;
  // Enriched fields
  assignee_username: string | null;
  assignee_display_name: string | null;
  created_by_username: string | null;
  created_by_display_name: string | null;
  project_owner_id: number | null;
  project_owner_username: string | null;
  project_owner_display_name: string | null;
  project_prod_stat_url: string | null;
}

export interface IssueRunbookScenarioData {
  scenario_id: number;
  entity_type: string;
  scenario_type: string;
  scenario_name: string;
  condition_description: string;
  detection_source: string;
  diagnostic_steps: string[];
  action_steps: string[];
  verification_steps: string[];
  escalation_target: string;
  fallback_owner_type: string | null;
  email_template: EmailTemplate | null;
  is_active: boolean;
  is_not_applicable: boolean;
}

export interface IssueActionWorkspaceData {
  issue: IssueData;
  entity_type: string | null;
  entity_label: string | null;
  support_group_id: number | null;
  support_group_name: string | null;
  support_group: string;
  owner_group_id: number | null;
  owner_group_name: string | null;
  owner_contact: string;
  cml_app_type: string | null;
  restart_supported: boolean | null;
  restart_summary: string | null;
  recommended_scenario_id: number | null;
  recommended_scenario_name: string | null;
  recommended_scenario_type: string | null;
  scenarios: IssueRunbookScenarioData[];
}

export interface IssueAssignableUserData {
  user_id: number;
  username: string;
  display_name: string | null;
  role: string;
}

export async function listIssues(params?: {
  type?: string;
  status?: string;
  assignee_id?: number;
  product_id?: number;
}): Promise<IssueData[]> {
  const query = new URLSearchParams();
  if (params?.type) query.set('type', params.type);
  if (params?.status) query.set('status', params.status);
  if (params?.assignee_id) query.set('assignee_id', String(params.assignee_id));
  if (params?.product_id) query.set('product_id', String(params.product_id));
  const qs = query.toString();
  return apiFetch<IssueData[]>(`/api/issues${qs ? '?' + qs : ''}`);
}

export async function getIssue(id: number): Promise<IssueData> {
  return apiFetch<IssueData>(`/api/issues/${id}`);
}

// Live MMP "complete response body" for an MMP issue's bound model. Fetched
// on demand from the issue detail (not stored). `error` set when MMP is
// unreachable or the binding is stale.
export interface IssueMmpRawData {
  error?: string;
  hint?: string;
  repo_name?: string;
  model_name?: string;
  is_production?: boolean;
  attention_required?: Record<string, { status?: boolean; description?: string }>;
  // The run this issue traces to (by run id), full raw body + annotation.
  focus_run?: Record<string, unknown> | null;
  focus_run_id?: number | null;
  focus_run_found?: boolean | null;
  focus_run_is_latest?: boolean;
  latest_production_run?: Record<string, unknown> | null;
  recent_production_runs?: Array<Record<string, unknown>>;
  total_production_runs?: number;
  truncated?: boolean;
  approval_status_legend?: Record<string, string>;
  note?: string;
}

export async function getIssueMmpRaw(id: number): Promise<IssueMmpRawData> {
  return apiFetch<IssueMmpRawData>(`/api/issues/${id}/mmp-raw`);
}

export async function listPendingHandovers(): Promise<IssueData[]> {
  return apiFetch<IssueData[]>('/api/issues/pending-handovers');
}

export async function listIssueAssignableUsers(): Promise<IssueAssignableUserData[]> {
  return apiFetch<IssueAssignableUserData[]>('/api/issues/assignable-users');
}

export async function approveHandover(issueId: number): Promise<IssueData> {
  return apiFetch<IssueData>(`/api/issues/${issueId}/approve`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function rejectHandover(issueId: number, rejectionReason: string): Promise<IssueData> {
  return apiFetch<IssueData>(`/api/issues/${issueId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ rejection_reason: rejectionReason }),
  });
}

export async function updateIssue(
  id: number,
  data: { status?: string; assignee_id?: number; resolution_description?: string },
): Promise<IssueData> {
  return apiFetch<IssueData>(`/api/issues/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function createIssue(data: {
  type: string;
  title: string;
  description?: string;
  product_id?: number;
  job_id?: number;
  app_id?: number;
  support_group_id?: number;
  assignee_id?: number;
}): Promise<IssueData> {
  return apiFetch<IssueData>('/api/issues', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function listMyIssues(): Promise<IssueData[]> {
  return apiFetch<IssueData[]>('/api/issues/my');
}

export interface ClaimIssuesResult {
  claimed: IssueData[];
  skipped: { issue_id: number; reason: string }[];
}

/** Self-assign one or more open issues to the current user (Ops triage board). */
export async function claimIssues(issueIds: number[]): Promise<ClaimIssuesResult> {
  return apiFetch<ClaimIssuesResult>('/api/issues/claim', {
    method: 'POST',
    body: JSON.stringify({ issue_ids: issueIds }),
  });
}

// ── Handover API ─────────────────────────────────────────────────────

export async function initiateHandover(productId: number): Promise<IssueData> {
  return apiFetch<IssueData>(`/api/products/${productId}/handover`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function getProduct(productId: number): Promise<ProductData> {
  return apiFetch<ProductData>(`/api/products/${productId}`);
}

export async function getIssueActionWorkspace(id: number): Promise<IssueActionWorkspaceData> {
  return apiFetch<IssueActionWorkspaceData>(`/api/issues/${id}/workspace`);
}

export async function runIssueAction(
  id: number,
  data: {
    action: string;
    scenario_id?: number;
    step_title?: string;
    notes?: string;
    verification_notes?: string;
    escalation_target?: string;
    actions_taken?: string[];
    final_conclusion?: string;
  },
): Promise<IssueData> {
  return apiFetch<IssueData>(`/api/issues/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/** Per-product handover completeness — still used by the product handover guide. */
export async function getHandoverCompleteness(productId: number): Promise<HandoverCompletenessData> {
  return apiFetch<HandoverCompletenessData>(`/api/products/${productId}/handover-completeness`);
}

// ── Project version control & handover (moved up from product level) ───

export interface ProjectVersionData {
  id: number;
  project_id: number;
  version_number: string;
  version_status: string;
  change_summary: string;
  snapshot_json: Record<string, unknown> | null;
  completeness_summary_json: Record<string, unknown> | null;
  submitted_by: number | null;
  submitted_at: string | null;
  reviewed_by: number | null;
  reviewed_at: string | null;
  rejection_reason: string | null;
  derived_from_version_id: number | null;
  created_at: string;
  updated_at: string | null;
}

/** Project-wide handover completeness (aggregates every product under it). */
export async function getProjectHandoverCompleteness(projectId: number): Promise<HandoverCompletenessData> {
  return apiFetch<HandoverCompletenessData>(`/api/projects/${projectId}/handover-completeness`);
}

export async function listProjectVersions(projectId: number): Promise<ProjectVersionData[]> {
  return apiFetch<ProjectVersionData[]>(`/api/projects/${projectId}/versions`);
}

export async function getProjectVersion(versionId: number): Promise<ProjectVersionData> {
  return apiFetch<ProjectVersionData>(`/api/projects/versions/${versionId}`);
}

export async function openProjectDraft(projectId: number): Promise<ProjectVersionData> {
  return apiFetch<ProjectVersionData>(`/api/projects/${projectId}/drafts`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function submitProjectVersion(projectId: number, data?: { change_summary?: string; requested_version_number?: string }): Promise<ProjectVersionData> {
  return apiFetch<ProjectVersionData>(`/api/projects/${projectId}/versions/submit`, {
    method: 'POST',
    body: JSON.stringify(data || {}),
  });
}

export async function approveProjectVersion(versionId: number): Promise<ProjectVersionData> {
  return apiFetch<ProjectVersionData>(`/api/projects/versions/${versionId}/approve`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function rejectProjectVersion(versionId: number, rejectionReason: string): Promise<ProjectVersionData> {
  return apiFetch<ProjectVersionData>(`/api/projects/versions/${versionId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ rejection_reason: rejectionReason }),
  });
}

export async function rollbackProjectVersion(versionId: number): Promise<ProjectVersionData> {
  return apiFetch<ProjectVersionData>(`/api/projects/versions/${versionId}/rollback`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function rollbackProductVersion(versionId: number): Promise<ProductVersionData> {
  return apiFetch<ProductVersionData>(`/api/product-versions/${versionId}/rollback`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

// ── Project Members API ──────────────────────────────────────────────

export interface ProjectMemberData {
  id: number;
  project_id: number;
  user_id: number;
  username: string;
  display_name: string | null;
  /** Per-project role from ProjectMember.role
   * ('business_owner' | 'relayops_member'). NOT the user's global login role
   * — that lives on `global_role` below. */
  role: string | null;
  /** The user's global login role (admin / regular_user / relayops_member /
   * legacy 'business_owner'). Informational — independent of `role`. */
  global_role: string | null;
  support_group_id: number | null;
  support_group_name: string | null;
  active_issue_count: number;
  is_owner: boolean;
  added_by: number;
  created_at: string;
}

export interface ProjectMemberCandidateData {
  user_id: number;
  username: string;
  display_name: string | null;
  role: string;
}

export interface SupportGroupData {
  id: number;
  group_key: string;
  group_name: string;
  description: string;
  source_type: string;
  external_ref: string | null;
  sync_status: string | null;
  last_synced_at: string | null;
  is_active: boolean;
  created_by: number | null;
  created_at: string;
  updated_at: string | null;
}

export interface ProjectSupportGroupData {
  id: number;
  project_id: number;
  support_group_id: number;
  support_group_name: string;
  created_by: number | null;
  created_at: string;
}

export async function listProjectMembers(projectId: number): Promise<ProjectMemberData[]> {
  return apiFetch<ProjectMemberData[]>(`/api/projects/${projectId}/members`);
}

export async function listProjectMemberCandidates(projectId: number, params?: { search?: string }): Promise<ProjectMemberCandidateData[]> {
  const query = new URLSearchParams();
  if (params?.search) query.set('search', params.search);
  const qs = query.toString();
  return apiFetch<ProjectMemberCandidateData[]>(`/api/projects/${projectId}/member-candidates${qs ? '?' + qs : ''}`);
}

// Per-project roles assignable through the Members UI. The project's
// business_owner role is reserved for the project creator and is
// transferred via a separate endpoint, never through addProjectMember.
//  - 'relayops_member': view-only collaborator.
//  - 'product_member': project editor — can modify the project and its
//    assets like the owner (but cannot manage members / transfer ownership).
export type AssignableRole = 'relayops_member' | 'product_member';

export async function addProjectMember(
  projectId: number,
  username: string,
  role: AssignableRole = 'relayops_member',
): Promise<ProjectMemberData> {
  // Membership is keyed by username. The backend pre-provisions a stub
  // user row when the username hasn't logged in yet so membership can be
  // granted ahead of first login. The user's *global* role is no longer
  // touched here — only their project-level role is set.
  return apiFetch<ProjectMemberData>(`/api/projects/${projectId}/members`, {
    method: 'POST',
    body: JSON.stringify({ username, role }),
  });
}

export async function removeProjectMember(projectId: number, userId: number): Promise<void> {
  await apiFetch(`/api/projects/${projectId}/members/${userId}`, { method: 'DELETE' });
}

/** Change a project member's per-project role between 'relayops_member' (view-only)
 *  and 'product_member' (project editor). Backend authorization: platform
 *  admins and the project's Business Owner may change anyone's role; any
 *  member may change their *own* role. The business_owner row is invariant-
 *  bound to Project.owner_id and changes via {@link transferProjectOwner}. */
export async function updateProjectMemberRole(
  projectId: number,
  userId: number,
  role: AssignableRole,
): Promise<ProjectMemberData> {
  return apiFetch<ProjectMemberData>(`/api/projects/${projectId}/members/${userId}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });
}

/** Transfer a project's Business Owner to another user by username.
 *
 * The backend pre-provisions a stub user if the username hasn't logged
 * in yet (same flow as addProjectMember), demotes the old owner's
 * per-project role to 'relayops_member', and promotes (or creates) the
 * target's ProjectMember row to 'business_owner'. Caller should refresh
 * the project list afterwards since this changes Project.owner_id. */
export async function transferProjectOwner(projectId: number, username: string): Promise<void> {
  await apiFetch(`/api/projects/${projectId}/owner`, {
    method: 'POST',
    body: JSON.stringify({ username }),
  });
}

// ── Admin User Management API ────────────────────────────────────────

/** Global user role (login identity, NOT per-project role). */
export type GlobalUserRole = 'admin' | 'relayops_member' | 'regular_user';

export interface AdminUserData {
  id: number;
  username: string;
  display_name: string | null;
  role: GlobalUserRole;
  /** True when an administrator assigned the role. */
  role_locked: boolean;
  /** True when the user is pinned to admin via config.yaml's platform_owners;
   *  UI disables demotion for these protected accounts. */
  is_platform_owner: boolean;
  /** Number of ProjectMember rows. Lets the UI surface "this user is on
   *  N projects" so an admin understands the blast radius of a demote. */
  project_count: number;
  created_at: string | null;
}

export async function listAdminUsers(params?: { search?: string }): Promise<AdminUserData[]> {
  const query = new URLSearchParams();
  if (params?.search) query.set('search', params.search);
  const qs = query.toString();
  return apiFetch<AdminUserData[]>(`/api/admin/users${qs ? '?' + qs : ''}`);
}

export async function createLocalUser(data: {
  username: string; password: string; display_name: string; role: GlobalUserRole;
}): Promise<AdminUserData> {
  return apiFetch('/api/admin/users', { method: 'POST', body: JSON.stringify(data) });
}

export async function resetUserPassword(userId: number, password: string): Promise<void> {
  await apiFetch(`/api/admin/users/${userId}/password`, { method: 'PUT', body: JSON.stringify({ password }) });
}

export async function changeAdminUserRole(userId: number, role: GlobalUserRole): Promise<AdminUserData> {
  return apiFetch<AdminUserData>(`/api/admin/users/${userId}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });
}

export async function listSupportGroups(params?: { search?: string; active_only?: boolean }): Promise<SupportGroupData[]> {
  const query = new URLSearchParams();
  if (params?.search) query.set('search', params.search);
  if (typeof params?.active_only === 'boolean') query.set('active_only', String(params.active_only));
  const qs = query.toString();
  return apiFetch<SupportGroupData[]>(`/api/support-groups${qs ? '?' + qs : ''}`);
}

export async function createSupportGroup(data: {
  group_key?: string;
  group_name: string;
  description?: string;
  source_type?: string;
  external_ref?: string;
  is_active?: boolean;
}): Promise<SupportGroupData> {
  return apiFetch<SupportGroupData>('/api/support-groups', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateSupportGroup(id: number, data: Partial<SupportGroupData>): Promise<SupportGroupData> {
  return apiFetch<SupportGroupData>(`/api/support-groups/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function listProjectSupportGroups(projectId: number): Promise<ProjectSupportGroupData[]> {
  return apiFetch<ProjectSupportGroupData[]>(`/api/projects/${projectId}/support-groups`);
}

export async function bindProjectSupportGroup(projectId: number, support_group_id: number): Promise<ProjectSupportGroupData> {
  return apiFetch<ProjectSupportGroupData>(`/api/projects/${projectId}/support-groups`, {
    method: 'POST',
    body: JSON.stringify({ support_group_id }),
  });
}

export async function unbindProjectSupportGroup(projectId: number, supportGroupId: number): Promise<void> {
  await apiFetch(`/api/projects/${projectId}/support-groups/${supportGroupId}`, { method: 'DELETE' });
}

export async function updateProjectOwnerGroup(projectId: number, owner_group_id: number | null): Promise<ProjectData> {
  return apiFetch<ProjectData>(`/api/projects/${projectId}/owner-group`, {
    method: 'PUT',
    body: JSON.stringify({ owner_group_id }),
  });
}

// ── Schedule API ─────────────────────────────────────────────────────

export interface ScheduleData {
  id: number;
  start_time: string;
  end_time: string;
  assignee_id: number;
  assignee_username: string | null;
  assignee_display_name: string | null;
  created_by: number;
  duty_role: string;
  note: string;
  created_at: string;
  updated_at: string | null;
}

export async function listSchedules(): Promise<ScheduleData[]> {
  return apiFetch<ScheduleData[]>('/api/schedules');
}

export async function createSchedule(data: {
  start_time: string;
  end_time: string;
  assignee_id: number;
  duty_role?: string;
  note?: string;
}): Promise<ScheduleData> {
  return apiFetch<ScheduleData>('/api/schedules', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateSchedule(
  id: number,
  data: { start_time?: string; end_time?: string; assignee_id?: number; duty_role?: string; note?: string },
): Promise<ScheduleData> {
  return apiFetch<ScheduleData>(`/api/schedules/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteSchedule(id: number): Promise<void> {
  await apiFetch(`/api/schedules/${id}`, { method: 'DELETE' });
}

export async function getCurrentSchedule(): Promise<ScheduleData[]> {
  return apiFetch<ScheduleData[]>('/api/schedules/current');
}

export async function listMySchedules(): Promise<ScheduleData[]> {
  return apiFetch<ScheduleData[]>('/api/schedules/my');
}

export interface IssuePreferenceData {
  issue_type: string;
}

export async function getMyIssuePreferences(): Promise<IssuePreferenceData[]> {
  return apiFetch<IssuePreferenceData[]>('/api/me/issue-preferences');
}

export async function updateMyIssuePreferences(issueTypes: string[]): Promise<IssuePreferenceData[]> {
  return apiFetch<IssuePreferenceData[]>('/api/me/issue-preferences', {
    method: 'PUT',
    body: JSON.stringify({ issue_types: issueTypes }),
  });
}

export interface EligibleUser {
  id: number;
  username: string;
  display_name: string | null;
  role: string;
}

export async function listEligibleUsers(): Promise<EligibleUser[]> {
  return apiFetch<EligibleUser[]>('/api/schedules/eligible-users');
}

// ── Audit Log API ────────────────────────────────────────────────────

export interface AuditLogData {
  id: number;
  user_id: number;
  action: string;
  entity_type: string;
  entity_id: number;
  old_value: Record<string, unknown> | null;
  new_value: Record<string, unknown> | null;
  timestamp: string;
  username: string | null;
  display_name: string | null;
}

export async function listAuditLogs(params?: {
  entity_type?: string;
  entity_id?: number;
  user_id?: number;
  action?: string;
  limit?: number;
  offset?: number;
}): Promise<AuditLogData[]> {
  const query = new URLSearchParams();
  if (params?.entity_type) query.set('entity_type', params.entity_type);
  if (params?.entity_id) query.set('entity_id', String(params.entity_id));
  if (params?.user_id) query.set('user_id', String(params.user_id));
  if (params?.action) query.set('action', params.action);
  if (params?.limit) query.set('limit', String(params.limit));
  if (params?.offset) query.set('offset', String(params.offset));
  const qs = query.toString();
  return apiFetch<AuditLogData[]>(`/api/audit-logs${qs ? '?' + qs : ''}`);
}

export interface IssueAuditSummaryData {
  issue_id: number;
  issue_type: string;
  issue_status: string;
  issue_title: string;
  project_id: number | null;
  project_name: string | null;
  project_is_system: boolean;
  product_id: number | null;
  product_version_id: number | null;
  product_version_number: string | null;
  product_version_status: string | null;
  project_version_id: number | null;
  project_version_number: string | null;
  project_version_status: string | null;
  product_name: string | null;
  product_is_system: boolean;
  support_group_id: number | null;
  support_group_name: string | null;
  owner_group_id: number | null;
  owner_group_name: string | null;
  assignee_id: number | null;
  assignee_username: string | null;
  assignee_display_name: string | null;
  created_at: string;
  updated_at: string | null;
  latest_event_at: string;
  latest_event_label: string;
  event_count: number;
  notification_count: number;
}

export interface IssueAuditTimelineEventData {
  audit_log_id: number;
  timestamp: string;
  action: string;
  entity_type: string;
  label: string;
  summary: string;
  actor_user_id: number;
  actor_username: string | null;
  actor_display_name: string | null;
  recipient_user_id: number | null;
  recipient_username: string | null;
  recipient_display_name: string | null;
  is_notification: boolean;
  details: Record<string, unknown> | null;
}

export async function listIssueAuditSummaries(params?: {
  status?: string;
  issue_type?: string;
  search?: string;
  limit?: number;
}): Promise<IssueAuditSummaryData[]> {
  const query = new URLSearchParams();
  if (params?.status) query.set('status', params.status);
  if (params?.issue_type) query.set('issue_type', params.issue_type);
  if (params?.search) query.set('search', params.search);
  if (params?.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return apiFetch<IssueAuditSummaryData[]>(`/api/audit-logs/issues${qs ? '?' + qs : ''}`);
}

export async function getIssueAuditTimeline(issueId: number): Promise<IssueAuditTimelineEventData[]> {
  return apiFetch<IssueAuditTimelineEventData[]>(`/api/audit-logs/issues/${issueId}`);
}

export interface ExportPayloadData {
  export_type: string;
  generated_at: string;
  filters: Record<string, unknown> | null;
  data: Record<string, unknown>;
}

export async function getAuditExport(params?: {
  status?: string;
  issue_type?: string;
  search?: string;
  limit?: number;
}): Promise<ExportPayloadData> {
  const query = new URLSearchParams();
  if (params?.status) query.set('status', params.status);
  if (params?.issue_type) query.set('issue_type', params.issue_type);
  if (params?.search) query.set('search', params.search);
  if (params?.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return apiFetch<ExportPayloadData>(`/api/audit-logs/export${qs ? '?' + qs : ''}`);
}

// ── Analytics API ────────────────────────────────────────────────────

export interface IssueTypeCount {
  type: string;
  count: number;
}

export interface AnalyticsBreakdownItem {
  id: number | null;
  name: string;
  count: number;
}

export interface MonthlyAnalyticsData {
  period_granularity: 'month' | 'week';
  year: number;
  month: number;
  week_start: string | null;
  period_label: string;
  period_start: string;
  period_end: string;
  total: number;
  by_type: IssueTypeCount[];
  by_product: AnalyticsBreakdownItem[];
  by_project: AnalyticsBreakdownItem[];
  manual_interventions: number;
  avg_resolution_minutes: number;
  max_resolution_minutes: number;
  sla_compliance_rate: number;
  sla_compliant_count: number;
  sla_total_count: number;
  open_count: number;
  in_progress_count: number;
  resolved_count: number;
}

export interface ProductAnalyticsData extends MonthlyAnalyticsData {
  product_id: number;
  product_name: string;
  project_id: number | null;
  project_name: string | null;
}

export interface ProjectAnalyticsData extends MonthlyAnalyticsData {
  project_id: number;
  project_name: string;
}

export interface ProductHealthItemData {
  product_id: number;
  product_name: string;
  project_id: number | null;
  project_name: string | null;
  job_runs_total: number;
  job_failures: number;
  failure_rate_percent: number;
  open_issue_count: number;
  max_repeat_failure_streak: number;
  anomaly_score: number;
  severity: string;
  is_anomaly: boolean;
  anomaly_reasons: string[];
  last_failed_at: string | null;
  last_issue_at: string | null;
}

export interface ProductHealthAnomalyRulesData {
  min_runs: number;
  min_failure_rate_percent: number;
  min_repeat_failure_streak: number;
  min_open_issues: number;
  recent_failure_hours: number;
}

export interface ProductHealthAnalyticsData {
  period_granularity: 'month' | 'week';
  year: number;
  month: number;
  week_start: string | null;
  period_label: string;
  period_start: string;
  period_end: string;
  total_products: number;
  anomaly_products: number;
  avg_failure_rate_percent: number;
  open_high_risk_issues: number;
  anomaly_rules: ProductHealthAnomalyRulesData;
  items: ProductHealthItemData[];
}

export interface ProductHealthTrendPointData {
  date: string;
  job_runs_total: number;
  job_failures: number;
  failure_rate_percent: number;
}

export interface ProductHealthJobItemData {
  job_id: number;
  job_name: string;
  job_runs_total: number;
  job_failures: number;
  failure_rate_percent: number;
  open_issue_count: number;
  max_repeat_failure_streak: number;
  last_failed_at: string | null;
}

export interface ProductHealthDrilldownData {
  period_granularity: 'month' | 'week';
  year: number;
  month: number;
  week_start: string | null;
  period_label: string;
  period_start: string;
  period_end: string;
  anomaly_rules: ProductHealthAnomalyRulesData;
  summary: ProductHealthItemData;
  daily_trend: ProductHealthTrendPointData[];
  jobs: ProductHealthJobItemData[];
  issues: IssueData[];
}

export interface ProductHealthAnomalyParams {
  min_runs?: number;
  min_failure_rate_percent?: number;
  min_repeat_failure_streak?: number;
  min_open_issues?: number;
  recent_failure_hours?: number;
}

export async function getMonthlyAnalytics(params?: {
  year?: number;
  month?: number;
  week_start?: string;
}): Promise<MonthlyAnalyticsData> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  const qs = query.toString();
  return apiFetch<MonthlyAnalyticsData>(`/api/analytics/monthly${qs ? '?' + qs : ''}`);
}

export async function getProductAnalytics(productId: number, params?: {
  year?: number;
  month?: number;
  week_start?: string;
}): Promise<ProductAnalyticsData> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  const qs = query.toString();
  return apiFetch<ProductAnalyticsData>(`/api/analytics/product/${productId}${qs ? '?' + qs : ''}`);
}

export async function getProjectAnalytics(projectId: number, params?: {
  year?: number;
  month?: number;
  week_start?: string;
}): Promise<ProjectAnalyticsData> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  const qs = query.toString();
  return apiFetch<ProjectAnalyticsData>(`/api/analytics/project/${projectId}${qs ? '?' + qs : ''}`);
}

export async function getProductsHealthAnalytics(params?: {
  year?: number;
  month?: number;
  week_start?: string;
  project_id?: number;
  min_runs?: number;
  min_failure_rate_percent?: number;
  min_repeat_failure_streak?: number;
  min_open_issues?: number;
  recent_failure_hours?: number;
}): Promise<ProductHealthAnalyticsData> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  if (params?.project_id) query.set('project_id', String(params.project_id));
  if (params?.min_runs) query.set('min_runs', String(params.min_runs));
  if (params?.min_failure_rate_percent) query.set('min_failure_rate_percent', String(params.min_failure_rate_percent));
  if (params?.min_repeat_failure_streak) query.set('min_repeat_failure_streak', String(params.min_repeat_failure_streak));
  if (params?.min_open_issues) query.set('min_open_issues', String(params.min_open_issues));
  if (params?.recent_failure_hours) query.set('recent_failure_hours', String(params.recent_failure_hours));
  const qs = query.toString();
  return apiFetch<ProductHealthAnalyticsData>(`/api/analytics/products/health${qs ? '?' + qs : ''}`);
}

export async function getProductHealthDrilldown(
  productId: number,
  params?: {
    year?: number;
    month?: number;
    week_start?: string;
    min_runs?: number;
    min_failure_rate_percent?: number;
    min_repeat_failure_streak?: number;
    min_open_issues?: number;
    recent_failure_hours?: number;
  },
): Promise<ProductHealthDrilldownData> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  if (params?.min_runs) query.set('min_runs', String(params.min_runs));
  if (params?.min_failure_rate_percent) query.set('min_failure_rate_percent', String(params.min_failure_rate_percent));
  if (params?.min_repeat_failure_streak) query.set('min_repeat_failure_streak', String(params.min_repeat_failure_streak));
  if (params?.min_open_issues) query.set('min_open_issues', String(params.min_open_issues));
  if (params?.recent_failure_hours) query.set('recent_failure_hours', String(params.recent_failure_hours));
  const qs = query.toString();
  return apiFetch<ProductHealthDrilldownData>(`/api/analytics/products/${productId}/drilldown${qs ? '?' + qs : ''}`);
}

export async function getProductHealthDrilldownExport(
  productId: number,
  params?: {
    year?: number;
    month?: number;
    week_start?: string;
    min_runs?: number;
    min_failure_rate_percent?: number;
    min_repeat_failure_streak?: number;
    min_open_issues?: number;
    recent_failure_hours?: number;
  },
): Promise<ExportPayloadData> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  if (params?.min_runs) query.set('min_runs', String(params.min_runs));
  if (params?.min_failure_rate_percent) query.set('min_failure_rate_percent', String(params.min_failure_rate_percent));
  if (params?.min_repeat_failure_streak) query.set('min_repeat_failure_streak', String(params.min_repeat_failure_streak));
  if (params?.min_open_issues) query.set('min_open_issues', String(params.min_open_issues));
  if (params?.recent_failure_hours) query.set('recent_failure_hours', String(params.recent_failure_hours));
  const qs = query.toString();
  return apiFetch<ExportPayloadData>(`/api/analytics/products/${productId}/drilldown/export${qs ? '?' + qs : ''}`);
}

export async function listAnalyticsIssues(params?: {
  year?: number;
  month?: number;
  week_start?: string;
  project_id?: number;
  product_id?: number;
  type?: string;
  status?: string;
  owner?: string;
  assignee?: string;
  search?: string;
  limit?: number;
}): Promise<IssueData[]> {
  const query = new URLSearchParams();
  if (params?.year) query.set('year', String(params.year));
  if (params?.month) query.set('month', String(params.month));
  if (params?.week_start) query.set('week_start', params.week_start);
  if (params?.project_id) query.set('project_id', String(params.project_id));
  if (params?.product_id) query.set('product_id', String(params.product_id));
  if (params?.type) query.set('type', params.type);
  if (params?.status) query.set('status', params.status);
  if (params?.owner) query.set('owner', params.owner);
  if (params?.assignee) query.set('assignee', params.assignee);
  if (params?.search) query.set('search', params.search);
  if (params?.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return apiFetch<IssueData[]>(`/api/analytics/issues${qs ? '?' + qs : ''}`);
}

export async function getProjectExport(projectId: number): Promise<ExportPayloadData> {
  return apiFetch<ExportPayloadData>(`/api/projects/${projectId}/export`);
}

export async function getProductExport(productId: number): Promise<ExportPayloadData> {
  return apiFetch<ExportPayloadData>(`/api/products/${productId}/export`);
}

export async function getIssueExport(issueId: number): Promise<ExportPayloadData> {
  return apiFetch<ExportPayloadData>(`/api/issues/${issueId}/export`);
}

export async function getOpsCenterExport(): Promise<ExportPayloadData> {
  return apiFetch<ExportPayloadData>('/api/analytics/export');
}

// ── API Keys ──────────────────────────────────────────────────────────

export interface ApiKeyData {
  id: number;
  name: string;
  key_prefix: string;
  is_active: number;
  created_by: number;
  created_at: string;
}

export interface ApiKeyCreateData extends ApiKeyData {
  key: string; // plaintext key, only returned on creation
}

export async function listApiKeys(): Promise<ApiKeyData[]> {
  return apiFetch<ApiKeyData[]>('/api/api-keys');
}

export async function createApiKey(name: string): Promise<ApiKeyCreateData> {
  return apiFetch<ApiKeyCreateData>('/api/api-keys', {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
}

export async function revokeApiKey(id: number): Promise<void> {
  await apiFetch<void>(`/api/api-keys/${id}`, { method: 'DELETE' });
}

// ── Job Executions ────────────────────────────────────────────────────

export interface JobExecutionData {
  id: number;
  job_id: number;
  status: string;
  timestamp: string;
  metadata_json: Record<string, unknown> | null;
  received_at: string;
  // True when this run's failure/stale alert was dismissed as a false
  // positive — charts/timelines treat it as a success point, not a failure.
  false_positive?: boolean;
}

export async function listJobExecutions(jobId: number, limit?: number): Promise<JobExecutionData[]> {
  const qs = limit ? `?limit=${limit}` : '';
  return apiFetch<JobExecutionData[]>(`/api/job-executions/${jobId}${qs}`);
}

// ── Application Health Checks (time-series) ──────────────────────────

export interface ApplicationHealthCheckData {
  id: number;
  application_id: number;
  checked_at: string;
  relayops_health: string;
  cml_status: string | null;
  error: string | null;
}

export async function listApplicationHealthChecks(
  appId: number,
  params?: { from?: string; to?: string; limit?: number },
): Promise<ApplicationHealthCheckData[]> {
  const search = new URLSearchParams();
  if (params?.from) search.set('from', params.from);
  if (params?.to) search.set('to', params.to);
  if (params?.limit) search.set('limit', String(params.limit));
  const qs = search.toString();
  return apiFetch<ApplicationHealthCheckData[]>(
    `/api/apps/${appId}/health-checks${qs ? `?${qs}` : ''}`,
  );
}

// ── Job MMP Drift Snapshots (time-series) ────────────────────────────

export interface MmpDriftSnapshotData {
  id: number;
  job_id: number;
  cml_model_id: number | null;
  drifted: boolean;
  drift_details: string | null;
  observed_at: string;
}

export async function listJobMmpDriftSnapshots(
  jobId: number,
  params?: { from?: string; to?: string; limit?: number },
): Promise<MmpDriftSnapshotData[]> {
  const search = new URLSearchParams();
  if (params?.from) search.set('from', params.from);
  if (params?.to) search.set('to', params.to);
  if (params?.limit) search.set('limit', String(params.limit));
  const qs = search.toString();
  return apiFetch<MmpDriftSnapshotData[]>(
    `/api/jobs/${jobId}/mmp-drift-snapshots${qs ? `?${qs}` : ''}`,
  );
}

// ── CML Status (batch) ───────────────────────────────────────────────

export interface CmlJobStatus {
  cml_status: string | null;
  last_run: string | null;
  /** Server timestamp of the poll that produced this status (or attempted to). */
  last_checked_at?: string | null;
}

export interface ProductCmlStatusResponse {
  product_id: number;
  jobs: Record<string, CmlJobStatus>;
  error?: string;
}

export async function getProductCmlStatus(productId: number): Promise<ProductCmlStatusResponse> {
  return apiFetch<ProductCmlStatusResponse>(`/api/products/${productId}/cml-status`);
}

// ── Notifications ─────────────────────────────────────────────────────

export interface NotificationData {
  id: number;
  user_id: number;
  title: string;
  message: string;
  type: string;
  is_read: number;
  related_entity_type: string | null;
  related_entity_id: number | null;
  issue_id: number | null;
  issue_type: string | null;
  issue_title: string | null;
  project_id: number | null;
  project_name: string | null;
  project_is_system: boolean;
  product_id: number | null;
  product_name: string | null;
  product_is_system: boolean;
  support_group_id: number | null;
  support_group_name: string | null;
  owner_group_id: number | null;
  owner_group_name: string | null;
  created_at: string;
}

export async function listNotifications(): Promise<NotificationData[]> {
  return apiFetch<NotificationData[]>('/api/notifications');
}

export async function markNotificationRead(id: number): Promise<NotificationData> {
  return apiFetch<NotificationData>(`/api/notifications/${id}/read`, { method: 'PUT' });
}

export async function markAllNotificationsRead(): Promise<void> {
  await apiFetch('/api/notifications/read-all', { method: 'PUT' });
}

// ── Duty morning report (值班晨报) ────────────────────────────────────

export interface DutyReportIssueBrief {
  id: number;
  type: string;
  status: string;
  title: string;
  assignee_id: number | null;
  assignee_name: string | null;
  sla_deadline: string | null;
  created_at: string | null;
  resolved_at: string | null;
  product_id: number | null;
  job_id: number | null;
  app_id: number | null;
  external_url: string | null;
  minutes_to_deadline?: number;
  auto_closed?: boolean;
}

export interface DutyReportSection {
  total: number;
  items: DutyReportIssueBrief[];
}

export interface DutyReportSections {
  window_start: string | null;
  window_end: string | null;
  open_issues: DutyReportSection;
  sla: { overdue: DutyReportSection; at_risk: DutyReportSection };
  mmp_pending: DutyReportSection;
  last_24h: { created: DutyReportSection; recovered: DutyReportSection };
  duty_activity: {
    total_events: number;
    activity_counts: Array<{
      user_id: number; username: string | null; action: string;
      entity_type: string; count: number;
    }>;
    notable_events: Array<{
      timestamp: string | null; user_id: number; username: string | null;
      action: string; entity_type: string; entity_id: number;
    }>;
  };
  on_duty: Array<{
    user_id: number; display_name: string | null;
    duty_role: string; shift_end: string | null;
  }>;
  stats: Record<string, number>;
}

export interface DutyReportSummaryData {
  headline: string;
  risk_highlights: string[];
  handover_notes: string[];
}

export interface DutyReportData {
  id: number;
  report_date: string;
  status: string;
  window_start: string | null;
  window_end: string | null;
  sections: DutyReportSections | null;
  llm_summary: DutyReportSummaryData | null;
  llm_status: string;
  email_status: string;
  recipient_user_ids: number[] | null;
  created_at: string;
}

export interface DutyReportTodayResponse {
  report: DutyReportData | null;
  show_popup: boolean;
}

export async function getDutyReportToday(): Promise<DutyReportTodayResponse> {
  return apiFetch<DutyReportTodayResponse>('/api/duty-report/today');
}

export async function getDutyReport(id: number): Promise<DutyReportData> {
  return apiFetch<DutyReportData>(`/api/duty-report/${id}`);
}

export async function dismissDutyReport(id: number): Promise<void> {
  await apiFetch(`/api/duty-report/${id}/dismiss`, { method: 'POST' });
}

// ── Scheduler: Product Check ─────────────────────────────────────────

export interface ProductCheckJobCheck {
  job_id?: number;
  name?: string;
  /** Normalized Ops status (success / failed / running / waiting / completed). */
  status?: string | null;
  /** Raw CML engine status string (ENGINE_*). */
  cml_status?: string;
  cml_run_id?: string;
  last_run?: string | null;
  /** "unresolved" when the row has no cached cml_project_id/cml_job_id — the
   *  check is silently skipped because there's nothing to query CML with. */
  binding?: 'unresolved';
  /** Operational note when there's no run history but the binding is fine. */
  note?: string;
  error?: string;
}

export interface ProductCheckAppCheck {
  app_id?: number;
  serving_url?: string;
  app_type?: string;
  healthy?: boolean;
  /** AppChecker bucket name: HEALTHY / ANOMALY / INCONCLUSIVE. */
  status?: string;
  /** Set when the app row had no resolved CML binding. */
  skipped?: string;
}

export interface ProductCheckMmpCheck {
  job_id?: number;
  project_id?: string;
  action?: string;
  reason?: string;
}

export interface ProductCheckResult {
  product_id: number;
  checked_at: string;
  results: {
    job_checks: ProductCheckJobCheck[];
    app_checks: ProductCheckAppCheck[];
    mmp_checks: ProductCheckMmpCheck[];
    issues_created: Array<{ type: string; id: number }>;
    issues_closed?: Array<{ type: string; id: number }>;
  };
  summary: string;
}

export async function checkProductNow(productId: number): Promise<ProductCheckResult> {
  return apiFetch<ProductCheckResult>(`/api/products/${productId}/check-now`, {
    method: 'POST',
  });
}

// ── Scenario templates ───────────────────────────────────────────────
// A workspace-wide library of reusable Job/App scenario drafts (including
// the owner-email template). ``payload`` mirrors the frontend scenario form
// state minus the per-instance id, so applying a template is a plain form
// fill — the server never interprets the blob.

export type ScenarioTemplateAssetKind = 'job' | 'app';

export interface ScenarioTemplateData {
  id: number;
  asset_kind: ScenarioTemplateAssetKind;
  name: string;
  scenario_type: string;
  description: string;
  payload: Record<string, unknown>;
  created_by: number | null;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export async function listScenarioTemplates(
  assetKind?: ScenarioTemplateAssetKind,
): Promise<ScenarioTemplateData[]> {
  const qs = assetKind ? `?asset_kind=${encodeURIComponent(assetKind)}` : '';
  return apiFetch<ScenarioTemplateData[]>(`/api/scenario-templates${qs}`);
}

export async function createScenarioTemplate(data: {
  asset_kind: ScenarioTemplateAssetKind;
  name: string;
  scenario_type?: string;
  description?: string;
  payload: Record<string, unknown>;
}): Promise<ScenarioTemplateData> {
  return apiFetch<ScenarioTemplateData>(`/api/scenario-templates`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateScenarioTemplate(
  id: number,
  data: Partial<{
    name: string;
    scenario_type: string;
    description: string;
    payload: Record<string, unknown>;
    is_active: boolean;
  }>,
): Promise<ScenarioTemplateData> {
  return apiFetch<ScenarioTemplateData>(`/api/scenario-templates/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteScenarioTemplate(id: number): Promise<void> {
  await apiFetch(`/api/scenario-templates/${id}`, { method: 'DELETE' });
}

// ── Onboarding agent (handover-document import wizard) ───────────────
// Types mirror core/agent/schemas.py — every field optional-ish (empty
// string = "the document didn't give this"), plus per-node source_quote
// so the reviewer can check a value against the original document.

/** Owner-notification email — same canonical 4-field shape the scenario
 *  entities store; auto-filled by the pipeline, editable by the reviewer. */
export interface OnboardingEmailTemplateDraft {
  to: string;
  cc: string;
  subject: string;
  body: string;
}

export interface OnboardingJobScenarioDraft {
  scenario_type: string;
  scenario_name: string;
  condition_description: string;
  diagnostic_steps: string[];
  action_steps: string[];
  verification_steps: string[];
  escalation_target: string;
  email_template: OnboardingEmailTemplateDraft;
  source_quote: string;
}

export interface OnboardingAppScenarioDraft {
  scenario_type: string;
  scenario_name: string;
  condition_description: string;
  action_steps: string[];
  verification_steps: string[];
  escalation_target: string;
  email_template: OnboardingEmailTemplateDraft;
  source_quote: string;
}

export interface OnboardingJobDraft {
  cml_project_name: string;
  cml_job_name: string;
  control_m_job_name: string;
  control_m_cron: string;
  schedule_cron: string;
  mmp_project_id: string;
  mmp_model_id: string;
  owner_contact: string;
  description: string;
  dependency_notes: string;
  scenarios: OnboardingJobScenarioDraft[];
  source_quote: string;
  /** Control-M rerun-request identifiers from the doc's email template
   *  (Application / Group / Table / CHG). A server-side extraction conduit:
   *  the backend folds them into the scenario email bodies then clears them,
   *  so these are normally empty by the time the client sees a draft. */
  control_m_application?: string;
  control_m_group?: string;
  control_m_table?: string;
  change_number?: string;
}

export interface OnboardingAppDraft {
  cml_project_name: string;
  cml_application_name: string;
  cml_subdomain: string;
  cml_app_type: string;
  application_url: string;
  health_check_url: string;
  owner_contact: string;
  description: string;
  scenarios: OnboardingAppScenarioDraft[];
  source_quote: string;
  /** See OnboardingJobDraft — server-side conduit, normally empty client-side. */
  control_m_application?: string;
  change_number?: string;
}

export interface OnboardingProductDraft {
  name: string;
  jobs: OnboardingJobDraft[];
  apps: OnboardingAppDraft[];
}

export interface OnboardingProjectDraft {
  name: string;
  description: string;
  /** "Project Owner" name from the doc; drives the owner_contact email fallback. */
  owner_name: string;
  cml_project_name: string;
  mmp_project_id: string;
  prod_stat_url: string;
}

export interface OnboardingPayload {
  project: OnboardingProjectDraft;
  products: OnboardingProductDraft[];
  warnings: string[];
}

export interface OnboardingFieldIssue {
  field_path: string;
  message: string;
}

export interface OnboardingClarificationOption {
  /** What gets written back into the payload on confirm. */
  value: string;
  /** Display-only context: match quality / owner / model count. */
  detail: string;
}

export interface OnboardingClarification {
  id: string;
  field_path: string;
  question: string;
  reason: string;
  /** Input widget hint: text | cml_project | mmp_project. */
  kind?: string;
  /** Server-verified candidates from the CML/MMP cross-check. */
  options?: OnboardingClarificationOption[];
}

export interface OnboardingValidation {
  errors: OnboardingFieldIssue[];
  warnings: OnboardingFieldIssue[];
  clarifications: OnboardingClarification[];
}

export interface OnboardingClarificationAnswer {
  id: string;
  field_path: string;
  answer: string;
}

export interface OnboardingBindingReport {
  entity: 'job' | 'app';
  id: number;
  name: string;
  /** false for jobs with no CML name — binding doesn't apply to them. */
  applicable: boolean;
  bound: boolean | null;
  error: string;
}

export interface OnboardingSubmitResult {
  project_id: number;
  /** Update mode only — the project the assets were folded into. */
  project_name?: string;
  /** 'update' when the draft targeted an existing project; absent for create. */
  mode?: 'update';
  products: Array<{
    product_id: number;
    name: string;
    jobs: Array<{ job_id: number; cml_job_name: string; change?: OnboardingChange }>;
    apps: Array<{ app_id: number; cml_application_name: string; change?: OnboardingChange }>;
  }>;
  bindings: OnboardingBindingReport[];
  /** Update mode only — how much was created vs patched. */
  created?: OnboardingChangeCounts;
  updated?: OnboardingChangeCounts & { project_fields?: string[] };
}

export interface OnboardingChangeCounts {
  products: number;
  jobs: number;
  apps: number;
  scenarios: number;
}

// ── Update mode ("add assets to an existing project") ────────────────

export type OnboardingChange = 'new' | 'update' | 'unchanged';

/** How one payload node compares to the platform row it maps onto. Mirrors
 *  core/agent/schemas.py NodeDiff. `previous` holds today's stored value for
 *  exactly the fields this draft would change (lists joined with newlines). */
export interface OnboardingNodeDiff {
  existing_id: number | null;
  change: OnboardingChange;
  previous: Record<string, string>;
}

/** Keyed by the same field_path the validation report uses: `project`,
 *  `products[0]`, `products[0].jobs[1].scenarios[2]`, … Recomputed server-side
 *  on every extraction / save / refine, so it always describes the payload as
 *  currently stored. */
export interface OnboardingDiff {
  project_id: number;
  project_name: string;
  nodes: Record<string, OnboardingNodeDiff>;
}

/** A project the caller may fold a handover document into. */
export interface OnboardingTargetProject {
  id: number;
  name: string;
  cml_project_name: string;
  product_count: number;
  job_count: number;
  app_count: number;
}

export async function listOnboardingTargetProjects(): Promise<OnboardingTargetProject[]> {
  return apiFetch<OnboardingTargetProject[]>('/api/agent/onboarding/target-projects');
}

export type OnboardingDraftStatus =
  | 'extracting'
  | 'ready'
  | 'refining'
  | 'submitted'
  | 'completed'
  | 'failed';

export interface OnboardingDraftData {
  id: number;
  /** Shared across sibling drafts split from one cross-project document; null
   *  for an ordinary single-project draft. */
  group_id: string | null;
  status: OnboardingDraftStatus;
  created_by: number;
  source_filename: string;
  /** Set = this draft adds assets to that existing project instead of
   *  creating a new one; `diff` is then populated. */
  target_project_id: number | null;
  payload: OnboardingPayload | null;
  validation: OnboardingValidation | null;
  diff: OnboardingDiff | null;
  result: OnboardingSubmitResult | null;
  error: string | null;
  submitted_project_id: number | null;
  created_at: string | null;
  updated_at: string | null;
  /** Only present on the GET-by-id response. */
  source_text?: string;
}

/** Multipart upload — can't go through apiFetch (it forces JSON Content-Type;
 *  FormData needs the browser to set the multipart boundary itself). */
export async function ingestOnboardingDocument(
  input: {
    file?: File;
    text?: string;
    url?: string;
    confluenceToken?: string;
    /** Fold the document into this existing project instead of creating one. */
    projectId?: number;
  },
): Promise<OnboardingDraftData> {
  const form = new FormData();
  if (input.file) form.append('file', input.file);
  if (input.text) form.append('text', input.text);
  if (input.url) form.append('url', input.url);
  if (input.confluenceToken) form.append('confluence_token', input.confluenceToken);
  if (input.projectId != null) form.append('project_id', String(input.projectId));
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch('/api/agent/onboarding', { method: 'POST', headers, body: form });
  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`;
    let payload: unknown = null;
    try {
      const body = await res.json();
      payload = body;
      detail = summarizeApiDetail(body.detail, detail);
    } catch {
      // ignore parse errors
    }
    throw new ApiError(res.status, detail, payload);
  }
  return res.json();
}

export interface OnboardingCapabilities {
  /** A shared Confluence service account is configured server-side, so the
   *  per-user PAT field can be hidden for URL fetches. */
  confluence_service_token: boolean;
}

export async function getOnboardingCapabilities(): Promise<OnboardingCapabilities> {
  return apiFetch<OnboardingCapabilities>('/api/agent/onboarding/capabilities');
}

export async function listOnboardingDrafts(): Promise<OnboardingDraftData[]> {
  return apiFetch<OnboardingDraftData[]>('/api/agent/onboarding');
}

export async function getOnboardingDraft(id: number): Promise<OnboardingDraftData> {
  return apiFetch<OnboardingDraftData>(`/api/agent/onboarding/${id}`);
}

export async function saveOnboardingDraft(
  id: number,
  payload: OnboardingPayload,
  answers: OnboardingClarificationAnswer[] = [],
): Promise<OnboardingDraftData> {
  return apiFetch<OnboardingDraftData>(`/api/agent/onboarding/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ payload, answers }),
  });
}

/** Multi-round review: persist the current edits + answers, then have the AI
 *  fill the remaining gaps (status flips to `refining`; poll until `ready`). */
export async function refineOnboardingDraft(
  id: number,
  payload: OnboardingPayload,
  answers: OnboardingClarificationAnswer[] = [],
  comment = '',
): Promise<OnboardingDraftData> {
  return apiFetch<OnboardingDraftData>(`/api/agent/onboarding/${id}/refine`, {
    method: 'POST',
    body: JSON.stringify({ payload, answers, comment }),
  });
}

/** Upload a Control-M job sheet (xlsx/csv/tsv/txt/html) onto an existing draft;
 *  its real Control-M names + schedules merge into the draft's jobs in the
 *  background (status flips to `refining`; poll until `ready`). Multipart, so
 *  it can't go through apiFetch (which forces a JSON content type). */
export async function importControlmSheet(
  id: number,
  file: File,
): Promise<OnboardingDraftData> {
  const form = new FormData();
  form.append('file', file);
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`/api/agent/onboarding/${id}/controlm`, {
    method: 'POST',
    headers,
    body: form,
  });
  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`;
    let payload: unknown = null;
    try {
      const body = await res.json();
      payload = body;
      detail = summarizeApiDetail(body.detail, detail);
    } catch {
      /* ignore parse errors */
    }
    throw new ApiError(res.status, detail, payload);
  }
  return res.json();
}

export async function submitOnboardingDraft(id: number): Promise<OnboardingDraftData> {
  return apiFetch<OnboardingDraftData>(`/api/agent/onboarding/${id}/submit`, {
    method: 'POST',
  });
}

export async function deleteOnboardingDraft(id: number): Promise<void> {
  await apiFetch<void>(`/api/agent/onboarding/${id}`, { method: 'DELETE' });
}

/** Split a cross-project draft into one sibling draft per CML project.
 *  Returns the sibling drafts (origin first), all sharing a new group_id. */
export async function splitOnboardingDraft(id: number): Promise<OnboardingDraftData[]> {
  return apiFetch<OnboardingDraftData[]>(`/api/agent/onboarding/${id}/split`, {
    method: 'POST',
  });
}

/** All sibling drafts produced by splitting one cross-project document. */
export async function getOnboardingGroup(groupId: string): Promise<OnboardingDraftData[]> {
  return apiFetch<OnboardingDraftData[]>(`/api/agent/onboarding/group/${groupId}`);
}
