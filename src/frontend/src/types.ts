// Global login role — independent of the per-project role on
// ProjectMember.role. 'regular_user' replaces the older 'business_owner'
// global role; the old value is still accepted on read so legacy JWTs /
// DB rows continue to authenticate. New code should write 'regular_user'.
export type UserRole = 'admin' | 'regular_user' | 'business_owner' | 'relayops_member';

export interface UserProfile {
  uid: string;
  email: string;
  displayName: string;
  role: UserRole;
}

export interface Project {
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
  /** Optional MMP project binding (project_repo_name). Independent of CML.
   *  When set, Job-level MMP model pickers under this Project default to
   *  filtering on this binding. */
  mmp_project_id: string;
  /** Single prod-stat dashboard URL for the project (one repo, one URL). */
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

export interface Product {
  id: number;
  project_id: number;
  name: string;
  /** Lightweight status — gates monitoring (archived assets aren't checked).
   *  Version/submit/approve lifecycle now lives on the project. */
  status: string;
  is_system: boolean;
  created_at: string;
  updated_at: string | null;
}

/** App probe contract. Drives which serving-URL endpoints
 *  the backend AppInterface hits and how it interprets the payload. */
export type CmlAppType = 'fastapi' | 'runtime' | 'ray' | 'generic';

/** Binary app-health verdict surfaced by the backend AppInterface in
 *  CanonicalAppHealthEvent.relayops_health. Health is derived from the CML
 *  application lifecycle only: 'up' | 'down' | 'unknown'. The remaining
 *  values are legacy — retained so historical health samples (recorded
 *  before the model was simplified) still type-check when rendered. */
export type RelayOpsHealth =
  | 'up'
  | 'down'
  | 'unknown'
  // legacy values (pre-binary model) — no longer produced going forward
  | 'healthy'
  | 'degraded'
  | 'unhealthy'
  | 'starting'
  | 'stopping'
  | 'stopped'
  | 'failed';

export interface Job {
  id: number;
  product_id: number;
  mmp_project_id: string;
  mmp_model_id: string;
  /** Deprecated: kept for back-compat. Use cml_job_name going forward. */
  control_m_job_name: string;
  control_m_cron: string;
  /** CML v2 binding inputs (user-editable). */
  cml_project_name: string;
  cml_job_name: string;
  /** CML v2 ids resolved server-side at create/update time. Read-only. */
  cml_project_id: string | null;
  cml_job_id: string | null;
  cml_binding_error: string | null;
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
  /** Per-job staleness strictness. NULL means "normal" (the global default).
   *  When sla_custom_minutes is set, it overrides the preset-derived window. */
  sla_preset: 'strict' | 'normal' | 'loose' | null;
  sla_custom_minutes: number | null;
  is_system: boolean;
  /** Last time we polled CML for this job (live, periodic checker, or Check Now). */
  last_checked_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface Application {
  id: number;
  product_id: number;
  application_url: string;
  health_check_url: string;
  /** CML v2 binding inputs (user-editable). */
  cml_project_name: string;
  cml_application_name: string;
  cml_subdomain: string;
  cml_app_type: CmlAppType;
  cml_serving_url: string;
  /** CML v2 ids resolved server-side. Read-only. */
  cml_project_id: string | null;
  cml_application_id: string | null;
  cml_binding_error: string | null;
  cml_binding_status: 'resolved' | 'pending' | 'error' | 'unconfigured';
  /** Latest AppChecker outcome — null until the first check has run. */
  last_cml_status: string | null;
  last_relayops_health: RelayOpsHealth | null;
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

export type ActionStatus = 'pending' | 'in-progress' | 'completed';

export interface Action {
  id: string;
  title: string;
  description: string;
  assignedTo: string; // Ops Member UID
  status: ActionStatus;
  slaDeadline: number; // Timestamp
  createdAt: number;
  completedAt?: number;
  productId?: string;
}

export interface OpsMember {
  uid: string;
  name: string;
  email: string;
}

export interface Schedule {
  id: string;
  startTime: string; // ISO Date string
  endTime: string; // ISO Date string
  onDutyMemberId: string;
  dutyRole: string;
}

export interface SLASettings {
  timeToRespond: number; // minutes
  timeToResolution: number; // minutes
}
