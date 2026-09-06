/**
 * Onboarding Wizard — handover-document import (AGENT_ONBOARDING_SPEC §2.5, S5).
 *
 * Route-style wizard (not chat bubbles): upload/paste → extracting (poll) →
 * preview & edit + answer the agent's clarification questions → submit report.
 * The server is the validation gate — red marks come from the draft's stored
 * ValidationReport and only refresh on save; clarification answers are written
 * back deterministically by field_path on the server (never through the LLM).
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  FilePlus2,
  FileText,
  FileUp,
  FolderPlus,
  HelpCircle,
  ListTree,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Send,
  Table2,
  Trash2,
  X,
  XCircle,
} from 'lucide-react';
import {
  deleteOnboardingDraft,
  getOnboardingCapabilities,
  getOnboardingDraft,
  getOnboardingGroup,
  importControlmSheet,
  ingestOnboardingDocument,
  listMmpProjects,
  listOnboardingDrafts,
  listOnboardingTargetProjects,
  refineOnboardingDraft,
  saveOnboardingDraft,
  searchCmlProjects,
  splitOnboardingDraft,
  submitOnboardingDraft,
  type OnboardingAppDraft,
  type OnboardingChange,
  type OnboardingClarification,
  type OnboardingDiff,
  type OnboardingDraftData,
  type OnboardingEmailTemplateDraft,
  type OnboardingJobDraft,
  type OnboardingPayload,
  type OnboardingTargetProject,
  type OnboardingValidation,
} from '../api';
import {
  EMAIL_BODY_INTRO,
  EMAIL_BODY_OUTRO_LINES,
  EMAIL_TOKEN_HINTS,
  OPTIONAL_EMAIL_ROW_TEMPLATES,
  applyEmailTemplateTokens,
  buildDefaultEmailBody,
  parseEmailBodyRows,
  serializeEmailBody,
  type EmailAssetKind,
  type EmailBodyRow,
} from '../emailTemplate';
import './onboarding-wizard.css';

// Mirrors JobFailureScenarioType.ALL minus the deprecated threshold pair
// (same policy as JOB_SCENARIO_OPTIONS in App.tsx), plus 'other'.
const JOB_SCENARIO_TYPES = [
  { value: 'not_triggered', label: 'Not Triggered' },
  { value: 'triggered_but_failed', label: 'Triggered But Failed' },
  { value: 'dependency_failed', label: 'Dependency Failed' },
  { value: 'logic_issue', label: 'Logic Issue' },
  { value: 'external_system_issue', label: 'External System Issue' },
  { value: 'mmp_drift_detected', label: 'MMP Drift Detected' },
  { value: 'mmp_fairness_risk', label: 'MMP Fairness Risk' },
  { value: 'mmp_run_pending_approval', label: 'MMP Run Pending Approval' },
  { value: 'mmp_unapproved_exp_run', label: 'MMP Unapproved Exp Run' },
  { value: 'mmp_perf_drift', label: 'MMP Perf Drift (sub-flag)' },
  { value: 'mmp_feature_drift', label: 'MMP Feature Drift (sub-flag)' },
  { value: 'mmp_data_quality_drift', label: 'MMP Data Quality Drift (sub-flag)' },
  { value: 'mmp_no_significant_drift', label: 'MMP No Significant Drift Detected' },
  { value: 'other', label: 'Other' },
];

const APP_SCENARIO_TYPES = [
  { value: 'offline', label: 'Offline' },
  { value: 'healthcheck_failed', label: 'Healthcheck Failed' },
  { value: 'restart_required', label: 'Restart Required' },
  { value: 'ray_actor_missing', label: 'Ray Actor Missing' },
  { value: 'deployment_issue', label: 'Deployment Issue' },
  { value: 'other', label: 'Other' },
];

const APP_TYPES = ['generic', 'fastapi', 'runtime', 'ray'];

const STATUS_LABELS: Record<string, string> = {
  extracting: 'Extracting',
  ready: 'Pending review',
  refining: 'AI completing',
  submitted: 'Creating',
  completed: 'Completed',
  failed: 'Failed',
};

const BUSY_STATUSES = ['extracting', 'refining', 'submitted'];

function emptyEmailTemplate(): OnboardingEmailTemplateDraft {
  return { to: '', cc: '', subject: '', body: '' };
}

function emptyJob(): OnboardingJobDraft {
  return {
    cml_project_name: '',
    cml_job_name: '',
    control_m_job_name: '',
    control_m_cron: '',
    schedule_cron: '',
    mmp_project_id: '',
    mmp_model_id: '',
    owner_contact: '',
    description: '',
    dependency_notes: '',
    scenarios: [],
    source_quote: '',
  };
}

function emptyApp(): OnboardingAppDraft {
  return {
    cml_project_name: '',
    cml_application_name: '',
    cml_subdomain: '',
    cml_app_type: 'generic',
    application_url: '',
    health_check_url: '',
    owner_contact: '',
    description: '',
    scenarios: [],
    source_quote: '',
  };
}

// ── Update mode ("add assets to an existing project") ────────────────

const CHANGE_LABELS: Record<OnboardingChange, string> = {
  new: 'New',
  update: 'Updated',
  unchanged: 'Unchanged',
};

/**
 * Per-node diff lookup for the preview form. In create-a-new-project mode
 * `diff` is null and every helper degrades to "no annotation", so the same
 * JSX renders both modes.
 */
class DiffView {
  constructor(private readonly diff: OnboardingDiff | null) {}

  get active(): boolean {
    return this.diff !== null;
  }

  get projectName(): string {
    return this.diff?.project_name ?? '';
  }

  /** '' when there is nothing to say about this node (create mode, or a node
   *  the server has not annotated — e.g. a row the reviewer just added). */
  change(path: string): OnboardingChange | '' {
    if (!this.diff) return '';
    return this.diff.nodes[path]?.change ?? 'new';
  }

  /** The value stored on the platform today, for a field this draft changes. */
  previous(path: string, field: string): string | undefined {
    return this.diff?.nodes[path]?.previous?.[field];
  }

  /** Every field this node would rewrite → its current platform value. */
  previousMap(path: string): Record<string, string> {
    return this.diff?.nodes[path]?.previous ?? {};
  }

  /** Counts for the review banner — how much this document actually moves. */
  counts(): { created: number; updated: number; unchanged: number } {
    const out = { created: 0, updated: 0, unchanged: 0 };
    for (const [path, node] of Object.entries(this.diff?.nodes ?? {})) {
      // Assets only: products and the project row are containers, and their
      // scenarios are already counted through their parent's `update`.
      if (!/^products\[\d+\]\.(jobs|apps)\[\d+\]$/.test(path)) continue;
      if (node.change === 'new') out.created += 1;
      else if (node.change === 'update') out.updated += 1;
      else out.unchanged += 1;
    }
    return out;
  }
}

function ChangeBadge({ change }: { change: OnboardingChange | '' }) {
  if (!change) return null;
  return <span className={`obw-change obw-change-${change}`}>{CHANGE_LABELS[change]}</span>;
}

/** "Currently: <old value>" under a field the document would change. */
function PreviousValue({ value }: { value: string | undefined }) {
  if (value === undefined) return null;
  return (
    <span className="obw-prev" title={value || '(empty)'}>
      Currently: {value.trim() ? value : '(empty)'}
    </span>
  );
}

interface IssueMaps {
  errors: Record<string, string[]>;
  warnings: Record<string, string[]>;
  clarByPath: Record<string, OnboardingClarification>;
  /** Rides along so every Field can show "Currently: …" without each of the
   *  ~40 call sites having to thread a second prop. */
  diff: DiffView;
}

function buildIssueMaps(
  validation: OnboardingValidation | null,
  diff: OnboardingDiff | null,
): IssueMaps {
  const maps: IssueMaps = { errors: {}, warnings: {}, clarByPath: {}, diff: new DiffView(diff) };
  for (const e of validation?.errors ?? []) {
    (maps.errors[e.field_path] = maps.errors[e.field_path] || []).push(e.message);
  }
  for (const w of validation?.warnings ?? []) {
    (maps.warnings[w.field_path] = maps.warnings[w.field_path] || []).push(w.message);
  }
  for (const c of validation?.clarifications ?? []) {
    maps.clarByPath[c.field_path] = c;
  }
  return maps;
}

/** Split `products[0].jobs[1].schedule_cron` into its node path and field. */
function splitFieldPath(path: string): [string, string] {
  const cut = path.lastIndexOf('.');
  return cut < 0 ? ['', path] : [path.slice(0, cut), path.slice(cut + 1)];
}

function previousFor(issues: IssueMaps, path: string): string | undefined {
  const [node, field] = splitFieldPath(path);
  return issues.diff.previous(node, field);
}

function StatusChip({ status }: { status: string }) {
  return (
    <span className={`obw-chip obw-chip-${status}`}>
      {BUSY_STATUSES.includes(status) ? <Loader2 size={12} className="obw-spin" /> : null}
      {STATUS_LABELS[status] || status}
    </span>
  );
}

function SourceQuote({ quote }: { quote: string }) {
  if (!quote.trim()) return null;
  return (
    <div className="obw-quote" title={quote}>
      Source: “{quote}”
    </div>
  );
}

// ── Field primitives ─────────────────────────────────────────────────

/**
 * Type-to-search input backed by the live CML / MMP pickers — the same
 * two-mode UX as App.tsx's CmlProjectCombobox: when CML reports has_more,
 * require ≥1 char before listing; otherwise show the full first page on
 * focus. The MMP directory is small, so it's fetched once and filtered
 * client-side. Free typing always stays allowed (CML-offline fallback).
 */
function SuggestInput({
  value,
  onChange,
  kind,
  mono,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  kind: 'cml' | 'mmp';
  mono?: boolean;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Array<{ value: string; detail: string }>>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [initialized, setInitialized] = useState(false);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const debounceRef = useRef<number | null>(null);
  const mmpCacheRef = useRef<Array<{ value: string; detail: string }> | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: globalThis.MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [open]);

  const fetchItems = useCallback(
    async (q: string) => {
      setLoading(true);
      setError('');
      try {
        if (kind === 'cml') {
          const resp = await searchCmlProjects(q || undefined);
          setItems(resp.items.map((o) => ({ value: o.name, detail: o.owner_username || '' })));
          setHasMore(resp.has_more);
        } else {
          if (!mmpCacheRef.current) {
            const all = await listMmpProjects();
            mmpCacheRef.current = all.map((o) => ({
              value: o.project_repo_name,
              detail: [o.business_name, `${o.model_count} models`].filter(Boolean).join(' · '),
            }));
          }
          const ql = q.trim().toLowerCase();
          setItems(
            mmpCacheRef.current.filter(
              (o) =>
                !ql || o.value.toLowerCase().includes(ql) || o.detail.toLowerCase().includes(ql),
            ),
          );
          setHasMore(false);
        }
      } catch (err: any) {
        setError(String(err?.detail || err?.message || err));
        setItems([]);
        setHasMore(false);
      } finally {
        setLoading(false);
        setInitialized(true);
      }
    },
    [kind],
  );

  useEffect(() => {
    if (!open) return;
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => fetchItems(value.trim()), 250);
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [value, open, fetchItems]);

  const trimmed = value.trim();
  const showList = open && initialized && !error && (!hasMore || trimmed.length > 0);
  const showTypeHint = open && initialized && !error && hasMore && trimmed.length === 0;
  const showEmpty =
    open && initialized && !error && !loading && items.length === 0 && (trimmed.length > 0 || !hasMore);

  return (
    <div ref={wrapperRef} className="obw-combo">
      <input
        className={`obw-input ${mono ? 'obw-mono' : ''}`}
        value={value}
        placeholder={placeholder ?? 'Click to browse or type to search…'}
        autoComplete="off"
        spellCheck={false}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          onChange(e.target.value);
          if (!open) setOpen(true);
        }}
      />
      {open && (
        <div className="obw-combo-panel">
          {loading && <div className="obw-combo-note">Loading…</div>}
          {!loading && error && (
            <div className="obw-combo-note obw-combo-warn">
              Platform unreachable ({error}) — you can type the name manually.
            </div>
          )}
          {!loading && showTypeHint && (
            <div className="obw-combo-note">Many projects are visible; type a few letters to search.</div>
          )}
          {!loading && showEmpty && <div className="obw-combo-note">No matching project.</div>}
          {!loading && showList && items.length > 0 && (
            <ul className="obw-combo-list">
              {items.map((opt) => (
                <li key={opt.value}>
                  <button
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      onChange(opt.value);
                      setOpen(false);
                    }}
                  >
                    <span className="obw-combo-name">{opt.value}</span>
                    {opt.detail && <span className="obw-combo-detail">{opt.detail}</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

interface FieldProps {
  label: string;
  path: string;
  value: string;
  issues: IssueMaps;
  onChange: (v: string) => void;
  required?: boolean;
  placeholder?: string;
  mono?: boolean;
  /** Render a live type-to-search combobox instead of a plain input. */
  suggest?: 'cml' | 'mmp';
}

function Field({ label, path, value, issues, onChange, required, placeholder, mono, suggest }: FieldProps) {
  const errs = issues.errors[path] || [];
  const clar = issues.clarByPath[path];
  const previous = previousFor(issues, path);
  return (
    <label
      className={`obw-field ${errs.length ? 'obw-field-error' : ''} ${previous !== undefined ? 'obw-field-changed' : ''}`}
    >
      <span className="obw-field-label">
        {label}
        {required && <em className="obw-required">*</em>}
        {clar && (
          <span className="obw-ask-mark" title={clar.question}>
            <HelpCircle size={11} /> Not in document
          </span>
        )}
      </span>
      {suggest ? (
        <SuggestInput value={value} onChange={onChange} kind={suggest} mono={mono} placeholder={placeholder} />
      ) : (
        <input
          className={`obw-input ${mono ? 'obw-mono' : ''}`}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      <PreviousValue value={previous} />
      {errs.map((m, i) => (
        <span className="obw-field-msg" key={i}>
          {m}
        </span>
      ))}
    </label>
  );
}

function AreaField({
  label,
  value,
  onChange,
  rows = 2,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
}) {
  return (
    <label className="obw-field obw-field-wide">
      <span className="obw-field-label">{label}</span>
      <textarea
        className="obw-input"
        rows={rows}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

/** Step list edited as one-step-per-line text. Empty lines are stripped on save. */
function StepsField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <label className="obw-field obw-field-wide">
      <span className="obw-field-label">{label} (one step per line)</span>
      <textarea
        className="obw-input obw-mono"
        rows={Math.max(2, value.length)}
        value={value.join('\n')}
        onChange={(e) => onChange(e.target.value.split('\n'))}
      />
    </label>
  );
}

/** Owner-notification email template — auto-filled by the pipeline (send-to
 *  resolution: scenario fixed contact → To, owner → Cc); edits are persisted
 *  with the scenario on submit. The body is edited as the same structured
 *  "Label: value" row table as the asset-edit page (shared ./emailTemplate),
 *  so the draft renders identically to what owners receive; a live preview
 *  shows tokens substituted. */
function EmailTemplateFields({
  template,
  assetKind,
  scenarioType,
  jobName,
  appName,
  onChange,
}: {
  template: OnboardingEmailTemplateDraft | undefined;
  assetKind: EmailAssetKind;
  scenarioType?: string;
  jobName?: string;
  appName?: string;
  onChange: (patch: Partial<OnboardingEmailTemplateDraft>) => void;
}) {
  const t = template ?? emptyEmailTemplate();
  // Parse-on-render: the body string is the single source of truth (same as the
  // asset editor) — re-parsing avoids local-state-vs-prop drift.
  const rows = parseEmailBodyRows(t.body);
  const commitRows = (next: EmailBodyRow[]) => onChange({ body: serializeEmailBody(next) });
  const updateRow = (i: number, patch: Partial<EmailBodyRow>) =>
    commitRows(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  const deleteRow = (i: number) => commitRows(rows.filter((_, idx) => idx !== i));
  const addRow = (tpl?: EmailBodyRow) => commitRows([...rows, tpl ? { ...tpl } : { label: '', value: '' }]);
  const existing = new Set(rows.map((r) => r.label.trim().toLowerCase()));
  const missingOptional = OPTIONAL_EMAIL_ROW_TEMPLATES.filter(
    (r) => !existing.has(r.label.toLowerCase()),
  );
  const preview = applyEmailTemplateTokens(t.body, {
    jobName: jobName ?? null,
    appName: appName ?? null,
    senderName: '',
  });
  // Flag when the doc's Control-M rerun-request identifiers were folded in, so
  // the reviewer notices them without opening the (collapsed) editor. A row
  // counts only when it carries a real value, not the {{token}} placeholder.
  const isFilled = (label: string) => {
    const v = rows.find((r) => r.label.trim().toLowerCase() === label)?.value?.trim() ?? '';
    return v.length > 0 && !/^\{\{.*\}\}$/.test(v);
  };
  const controlmFilled =
    isFilled('application') || isFilled('group') || isFilled('table') || isFilled('chg/tsk number');
  return (
    <details className="obw-email-tpl">
      <summary>
        Owner notification email template{t.to.trim() ? ` (→ ${t.to})` : ' (empty)'}
        {controlmFilled && <span className="obw-change obw-change-new">Control-M details filled</span>}
      </summary>
      <div className="obw-grid">
        <label className="obw-field">
          <span className="obw-field-label">To</span>
          <input className="obw-input" value={t.to} onChange={(e) => onChange({ to: e.target.value })} />
        </label>
        <label className="obw-field">
          <span className="obw-field-label">Cc</span>
          <input className="obw-input" value={t.cc} onChange={(e) => onChange({ cc: e.target.value })} />
        </label>
        <label className="obw-field obw-field-wide">
          <span className="obw-field-label">Subject</span>
          <input className="obw-input" value={t.subject} onChange={(e) => onChange({ subject: e.target.value })} />
        </label>
      </div>

      <div className="obw-field obw-field-wide">
        <span className="obw-field-label">Body (request table)</span>
        <div className="obw-email-body">
          <div className="obw-email-prose">{EMAIL_BODY_INTRO}</div>
          <table className="obw-email-table">
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td className="obw-email-empty" colSpan={3}>
                    No rows yet — add a row, or reset to the standard template.
                  </td>
                </tr>
              ) : (
                rows.map((row, i) => (
                  <tr key={i}>
                    <td className="obw-email-label-cell">
                      <input
                        className="obw-input obw-email-cell"
                        value={row.label}
                        placeholder="Label, e.g. Application"
                        onChange={(e) => updateRow(i, { label: e.target.value })}
                      />
                    </td>
                    <td>
                      <input
                        className="obw-input obw-email-cell"
                        value={row.value}
                        placeholder="Value — supports {{tokens}}"
                        onChange={(e) => updateRow(i, { value: e.target.value })}
                      />
                    </td>
                    <td className="obw-email-del-cell">
                      <button
                        type="button"
                        className="obw-email-del"
                        title="Delete this row"
                        onClick={() => deleteRow(i)}
                      >
                        <XCircle size={14} />
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          <div className="obw-email-actions">
            <button type="button" className="obw-btn" onClick={() => addRow()}>
              <Plus size={13} /> Add row
            </button>
            {missingOptional.map((r) => (
              <button
                type="button"
                key={r.label}
                className="obw-btn"
                title={`Add the standard "${r.label}" row.`}
                onClick={() => addRow(r)}
              >
                <Plus size={13} /> {r.label}
              </button>
            ))}
            <button
              type="button"
              className="obw-btn"
              title="Reset the body to the standard template for this scenario"
              onClick={() => onChange({ body: buildDefaultEmailBody(assetKind, scenarioType) })}
            >
              <RefreshCw size={13} /> Reset to default
            </button>
            <span className="obw-email-tokens">
              Tokens:{' '}
              {EMAIL_TOKEN_HINTS.map((h) => (
                <code key={h.token} title={h.description}>
                  {h.token}
                </code>
              ))}
            </span>
          </div>
          <div className="obw-email-prose">{EMAIL_BODY_OUTRO_LINES.join('\n')}</div>
        </div>
      </div>

      <details className="obw-email-preview">
        <summary>Preview (tokens substituted)</summary>
        <pre className="obw-email-preview-body">{preview}</pre>
      </details>
    </details>
  );
}

function SelectField({
  label,
  path,
  value,
  options,
  issues,
  onChange,
}: {
  label: string;
  path: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  issues: IssueMaps;
  onChange: (v: string) => void;
}) {
  const errs = issues.errors[path] || [];
  const known = options.some((o) => o.value === value);
  const previous = previousFor(issues, path);
  return (
    <label
      className={`obw-field ${errs.length ? 'obw-field-error' : ''} ${previous !== undefined ? 'obw-field-changed' : ''}`}
    >
      <span className="obw-field-label">{label}</span>
      <select className="obw-input" value={value} onChange={(e) => onChange(e.target.value)}>
        {!known && <option value={value}>{value || '(empty)'}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <PreviousValue value={previous} />
      {errs.map((m, i) => (
        <span className="obw-field-msg" key={i}>
          {m}
        </span>
      ))}
    </label>
  );
}

/** Fields a changed entity would rewrite, named. Covers what per-field
 *  "Currently: …" can't reach — the textarea/step-list fields, which are not
 *  rendered through `Field`. */
const CHANGED_FIELD_LABELS: Record<string, string> = {
  description: 'description',
  dependency_notes: 'dependency notes',
  condition_description: 'trigger condition',
  diagnostic_steps: 'diagnostic steps',
  action_steps: 'action steps',
  verification_steps: 'verification steps',
  email_template: 'email template',
  schedule_cron: 'schedule',
  control_m_cron: 'Control-M cron',
  owner_contact: 'owner contact',
  application_url: 'access URL',
  health_check_url: 'health check URL',
  cml_app_type: 'app type',
  cml_project_name: 'CML project',
  mmp_project_id: 'MMP project',
  mmp_model_id: 'MMP model',
  escalation_target: 'escalation contact',
  scenario_name: 'scenario name',
  scenario_type: 'scenario type',
  prod_stat_url: 'Prod Stat URL',
};

function ChangedFields({ issues, path }: { issues: IssueMaps; path: string }) {
  const previous = issues.diff.previousMap(path);
  const names = Object.keys(previous);
  if (names.length === 0) return null;
  return (
    <div className="obw-changed-summary">
      Will overwrite: {names.map((n) => CHANGED_FIELD_LABELS[n] || n).join(', ')}
    </div>
  );
}

/** Floating, collapsible asset navigator — jumps to the project / each product,
 *  job and app card by anchor id. Pinned to the bottom-right of the scrolling
 *  form so it stays reachable without taking layout space (default collapsed). */
function FormOutline({ payload }: { payload: OnboardingPayload }) {
  const [open, setOpen] = useState(false);
  const jump = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  if (payload.products.length === 0) return null;
  return (
    <div className="obw-outline">
      {open && (
        <div className="obw-outline-panel">
          <div className="obw-outline-head">
            <span>Assets</span>
            <button type="button" className="obw-outline-close" onClick={() => setOpen(false)} title="Close">
              <X size={14} />
            </button>
          </div>
          <div className="obw-outline-body">
            <button
              type="button"
              className="obw-outline-item obw-outline-proj"
              onClick={() => jump('obw-anchor-project')}
            >
              {payload.project.name || 'Project'}
            </button>
            {payload.products.map((prod, pi) => (
              <div className="obw-outline-group" key={pi}>
                <button
                  type="button"
                  className="obw-outline-item obw-outline-prod"
                  onClick={() => jump(`obw-anchor-product-${pi}`)}
                >
                  {prod.name || `Product #${pi + 1}`}
                </button>
                {prod.jobs.map((job, ji) => (
                  <button
                    type="button"
                    key={`j${ji}`}
                    className="obw-outline-item obw-outline-leaf"
                    onClick={() => jump(`obw-anchor-job-${pi}-${ji}`)}
                  >
                    <span className="obw-outline-tag obw-outline-tag-job">Job</span>
                    <span className="obw-outline-leaf-name">
                      {job.cml_job_name || job.control_m_job_name || `#${ji + 1}`}
                    </span>
                  </button>
                ))}
                {prod.apps.map((app, ai) => (
                  <button
                    type="button"
                    key={`a${ai}`}
                    className="obw-outline-item obw-outline-leaf"
                    onClick={() => jump(`obw-anchor-app-${pi}-${ai}`)}
                  >
                    <span className="obw-outline-tag obw-outline-tag-app">App</span>
                    <span className="obw-outline-leaf-name">
                      {app.cml_application_name || `#${ai + 1}`}
                    </span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
      <button
        type="button"
        className={`obw-outline-fab ${open ? 'obw-outline-fab-open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        title="Jump to a project / product / job / app"
      >
        <ListTree size={15} />
        Assets
      </button>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────

export default function OnboardingWizard({
  onOpenProject,
  onContext,
}: {
  onOpenProject?: (projectId: number) => void;
  // Reports the live form state up so the chat can answer in context. Fires
  // null when no draft is open (list view).
  onContext?: (ctx: import('./sse').FormContext | null) => void;
}) {
  const [view, setView] = useState<
    { kind: 'list' } | { kind: 'detail'; id: number } | { kind: 'group'; groupId: string }
  >({
    kind: 'list',
  });
  const [drafts, setDrafts] = useState<OnboardingDraftData[] | null>(null);
  const [listError, setListError] = useState('');
  const [deletingId, setDeletingId] = useState<number | null>(null);
  // Sibling drafts of a cross-project split (group landing page).
  const [groupDrafts, setGroupDrafts] = useState<OnboardingDraftData[] | null>(null);
  const [groupError, setGroupError] = useState('');

  // Ingest form — `mode` picks between registering a brand-new project and
  // folding the document into one that already exists.
  const [mode, setMode] = useState<'create' | 'update'>('create');
  const [targetProjects, setTargetProjects] = useState<OnboardingTargetProject[] | null>(null);
  const [targetProjectId, setTargetProjectId] = useState<number | null>(null);
  const [targetFilter, setTargetFilter] = useState('');
  const [targetError, setTargetError] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [pasted, setPasted] = useState('');
  const [pageUrl, setPageUrl] = useState('');
  const [confluenceToken, setConfluenceToken] = useState('');
  // When a shared Confluence service account is configured server-side, the
  // reviewer never needs to type a PAT — hide the field entirely.
  const [confluenceServiceToken, setConfluenceServiceToken] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [ingestError, setIngestError] = useState('');
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Detail state — `payload` is the local editing copy; `draft.validation`
  // is the last server-side report (refreshes on every save).
  const [draft, setDraft] = useState<OnboardingDraftData | null>(null);
  const [payload, setPayload] = useState<OnboardingPayload | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [refineComment, setRefineComment] = useState('');
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [detailError, setDetailError] = useState('');
  // Update mode: a big project's untouched assets are context, not work — let
  // the reviewer collapse them down to just what this document moves.
  const [showUnchanged, setShowUnchanged] = useState(true);
  const controlmInputRef = useRef<HTMLInputElement | null>(null);

  const loadList = useCallback(async () => {
    try {
      setListError('');
      setDrafts(await listOnboardingDrafts());
    } catch (err: any) {
      setListError(String(err?.detail || err?.message || err));
    }
  }, []);

  useEffect(() => {
    if (view.kind === 'list') loadList();
  }, [view, loadList]);

  /** Delete a draft from "My drafts". Confirms first; an in-flight draft
   *  (extracting/refining/submitting) is blocked server-side and reported. */
  const handleDeleteDraft = useCallback(
    async (d: OnboardingDraftData) => {
      const label = d.payload?.project?.name || d.source_filename || `Draft #${d.id}`;
      if (!window.confirm(`Delete this draft “${label}”? This cannot be undone.`)) return;
      setDeletingId(d.id);
      setListError('');
      try {
        await deleteOnboardingDraft(d.id);
        setDrafts((prev) => (prev ? prev.filter((x) => x.id !== d.id) : prev));
      } catch (err: any) {
        setListError(String(err?.detail || err?.message || err));
      } finally {
        setDeletingId(null);
      }
    },
    [],
  );

  // Probe once: does the server have a Confluence service account?
  useEffect(() => {
    getOnboardingCapabilities()
      .then((c) => setConfluenceServiceToken(c.confluence_service_token))
      .catch(() => setConfluenceServiceToken(false));
  }, []);

  // Load the projects this user may add assets to, the first time they switch
  // to that mode (an ordinary onboarding never pays for the call).
  useEffect(() => {
    if (mode !== 'update' || targetProjects !== null) return;
    let cancelled = false;
    setTargetError('');
    listOnboardingTargetProjects()
      .then((rows) => {
        if (!cancelled) setTargetProjects(rows);
      })
      .catch((err: any) => {
        if (!cancelled) {
          setTargetProjects([]);
          setTargetError(String(err?.detail || err?.message || err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [mode, targetProjects]);

  const visibleTargets = useMemo(() => {
    const q = targetFilter.trim().toLowerCase();
    const rows = targetProjects ?? [];
    if (!q) return rows;
    return rows.filter(
      (p) => p.name.toLowerCase().includes(q) || p.cml_project_name.toLowerCase().includes(q),
    );
  }, [targetProjects, targetFilter]);

  const openDraft = useCallback(async (id: number) => {
    setView({ kind: 'detail', id });
    setDraft(null);
    setPayload(null);
    setAnswers({});
    setDirty(false);
    setDetailError('');
    try {
      const d = await getOnboardingDraft(id);
      setDraft(d);
      setPayload(d.payload);
    } catch (err: any) {
      setDetailError(String(err?.detail || err?.message || err));
    }
  }, []);

  // Report the live form up to the chat (split-view grounding). Fires on every
  // edit so a question typed in chat carries the user's current — possibly
  // unsaved — form values; fires null in list view so closed/empty = no context.
  useEffect(() => {
    if (!onContext) return;
    if (view.kind !== 'detail' || !draft) {
      onContext(null);
      return;
    }
    onContext({
      draftId: draft.id,
      groupId: draft.group_id ?? undefined,
      targetProjectId: draft.target_project_id,
      diff: draft.diff,
      payload,
      clarifications: draft.validation?.clarifications ?? [],
      answers,
      dirty,
    });
  }, [onContext, view, draft, payload, answers, dirty]);

  // Poll while a background step (extraction / AI refine / a submit seen
  // from another window) is still running.
  useEffect(() => {
    if (view.kind !== 'detail') return;
    if (!draft || !BUSY_STATUSES.includes(draft.status)) return;
    const timer = setInterval(async () => {
      try {
        const d = await getOnboardingDraft(view.id);
        if (d.status !== draft.status) {
          setDraft(d);
          setPayload(d.payload);
        }
      } catch {
        /* transient poll error — keep trying */
      }
    }, 2500);
    return () => clearInterval(timer);
  }, [view, draft?.status]);

  // Load the sibling drafts when the group landing page is shown (e.g. opened
  // via a sibling's breadcrumb — handleSplit pre-fills them on the split path).
  useEffect(() => {
    if (view.kind !== 'group') return;
    let cancelled = false;
    setGroupError('');
    getOnboardingGroup(view.groupId)
      .then((sibs) => {
        if (!cancelled) setGroupDrafts(sibs);
      })
      .catch((err: any) => {
        if (!cancelled) setGroupError(String(err?.detail || err?.message || err));
      });
    return () => {
      cancelled = true;
    };
  }, [view]);

  const ingest = useCallback(async () => {
    if (!file && !pasted.trim() && !pageUrl.trim()) return;
    if (mode === 'update' && targetProjectId === null) return;
    setIngesting(true);
    setIngestError('');
    try {
      const projectId = mode === 'update' ? targetProjectId ?? undefined : undefined;
      const d = await ingestOnboardingDocument(
        file
          ? { file, projectId }
          : pageUrl.trim()
            ? {
                url: pageUrl.trim(),
                confluenceToken: confluenceToken.trim() || undefined,
                projectId,
              }
            : { text: pasted, projectId },
      );
      setFile(null);
      setPasted('');
      setPageUrl('');
      if (fileInputRef.current) fileInputRef.current.value = '';
      setDraft(d);
      setPayload(d.payload);
      setAnswers({});
      setDirty(false);
      setDetailError('');
      setView({ kind: 'detail', id: d.id });
    } catch (err: any) {
      setIngestError(String(err?.detail || err?.message || err));
    } finally {
      setIngesting(false);
    }
  }, [file, pasted, pageUrl, confluenceToken, mode, targetProjectId]);

  const mutate = useCallback((fn: (p: OnboardingPayload) => void) => {
    setPayload((prev) => {
      if (!prev) return prev;
      const next: OnboardingPayload = JSON.parse(JSON.stringify(prev));
      fn(next);
      return next;
    });
    setDirty(true);
  }, []);

  /** Cleaned payload + answered clarifications — shared by save and refine. */
  const buildSaveBody = useCallback(() => {
    if (!draft || !payload) return null;
    // Strip empty step lines the textarea editing leaves behind.
    const cleaned: OnboardingPayload = JSON.parse(JSON.stringify(payload));
    for (const product of cleaned.products) {
      for (const job of product.jobs) {
        for (const sc of job.scenarios) {
          sc.diagnostic_steps = sc.diagnostic_steps.filter((s) => s.trim());
          sc.action_steps = sc.action_steps.filter((s) => s.trim());
          sc.verification_steps = sc.verification_steps.filter((s) => s.trim());
        }
      }
      for (const app of product.apps) {
        for (const sc of app.scenarios) {
          sc.action_steps = sc.action_steps.filter((s) => s.trim());
          sc.verification_steps = sc.verification_steps.filter((s) => s.trim());
        }
      }
    }
    const clarList = draft.validation?.clarifications ?? [];
    const answerList = clarList
      .filter((c) => (answers[c.id] || '').trim())
      .map((c) => ({ id: c.id, field_path: c.field_path, answer: answers[c.id].trim() }));
    return { cleaned, answerList };
  }, [draft, payload, answers]);

  const doSave = useCallback(async (): Promise<OnboardingDraftData | null> => {
    const body = buildSaveBody();
    if (!draft || !body) return null;
    const res = await saveOnboardingDraft(draft.id, body.cleaned, body.answerList);
    setDraft(res);
    setPayload(res.payload);
    setAnswers({});
    setDirty(false);
    return res;
  }, [draft, buildSaveBody]);

  const hasPendingAnswers = useMemo(
    () => Object.values(answers).some((a: string) => a.trim()),
    [answers],
  );

  const handleSave = useCallback(async () => {
    setBusy(true);
    setDetailError('');
    try {
      await doSave();
    } catch (err: any) {
      setDetailError(String(err?.detail || err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [doSave]);

  /** Multi-round loop: persist the edits + answers, then let the AI fill
   *  the remaining gaps (status → refining, the poll brings it back). */
  const handleRefine = useCallback(async () => {
    const body = buildSaveBody();
    if (!draft || !body) return;
    setBusy(true);
    setDetailError('');
    try {
      const res = await refineOnboardingDraft(
        draft.id, body.cleaned, body.answerList, refineComment.trim(),
      );
      setDraft(res);
      setPayload(res.payload);
      setAnswers({});
      setRefineComment('');
      setDirty(false);
    } catch (err: any) {
      setDetailError(String(err?.detail || err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [draft, buildSaveBody, refineComment]);

  const handleControlmUpload = useCallback(
    async (sheet: File) => {
      if (!draft) return;
      setBusy(true);
      setDetailError('');
      try {
        const res = await importControlmSheet(draft.id, sheet);
        setDraft(res); // status → refining; the poller brings it back to ready
        setPayload(res.payload);
        setAnswers({});
        setDirty(false);
      } catch (err: any) {
        setDetailError(String(err?.detail || err?.message || err));
      } finally {
        setBusy(false);
        if (controlmInputRef.current) controlmInputRef.current.value = '';
      }
    },
    [draft],
  );

  const handleSubmit = useCallback(async () => {
    if (!draft) return;
    setBusy(true);
    setDetailError('');
    try {
      let current: OnboardingDraftData | null = draft;
      if (dirty || hasPendingAnswers) current = await doSave();
      if (!current) return;
      if ((current.validation?.errors?.length ?? 0) > 0) {
        setDetailError('Validation failed: fix the fields marked in red below before submitting.');
        return;
      }
      const res = await submitOnboardingDraft(current.id);
      setDraft(res);
      setPayload(res.payload);
    } catch (err: any) {
      setDetailError(String(err?.detail || err?.message || err));
      // Submit failures flip the draft to `failed` server-side — refresh so
      // the banner and the editable state match reality.
      try {
        const d = await getOnboardingDraft(draft.id);
        setDraft(d);
        setPayload(d.payload);
      } catch {
        /* keep the original error visible */
      }
    } finally {
      setBusy(false);
    }
  }, [draft, dirty, hasPendingAnswers, doSave]);

  /** Delete the draft currently open in the detail view, then return to list. */
  const handleDeleteCurrent = useCallback(async () => {
    if (!draft) return;
    const label = draft.payload?.project?.name || draft.source_filename || `Draft #${draft.id}`;
    if (!window.confirm(`Delete this draft “${label}”? This cannot be undone.`)) return;
    setBusy(true);
    setDetailError('');
    try {
      await deleteOnboardingDraft(draft.id);
      setView({ kind: 'list' });
    } catch (err: any) {
      setDetailError(String(err?.detail || err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [draft]);

  /** Split a cross-project draft into one sibling per CML project, then open
   *  the group landing page. Triggered from the project-level split card. */
  const handleSplit = useCallback(async () => {
    if (!draft) return;
    setBusy(true);
    setDetailError('');
    try {
      // Split runs against the server's stored payload — flush unsaved edits and
      // answers first so they aren't lost.
      if (dirty || hasPendingAnswers) await doSave();
      const sibs = await splitOnboardingDraft(draft.id);
      const gid = sibs[0]?.group_id;
      setGroupDrafts(sibs);
      if (gid) setView({ kind: 'group', groupId: gid });
    } catch (err: any) {
      setDetailError(String(err?.detail || err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [draft, dirty, hasPendingAnswers, doSave]);

  const issues = useMemo(
    () => buildIssueMaps(draft?.validation ?? null, draft?.diff ?? null),
    [draft],
  );
  const updating = draft?.target_project_id != null;
  const diffCounts = useMemo(() => issues.diff.counts(), [issues]);
  const errorCount = draft?.validation?.errors?.length ?? 0;
  const warningCount = draft?.validation?.warnings?.length ?? 0;
  const allClarifications = draft?.validation?.clarifications ?? [];
  // The cross-project split prompt is an action card (a button), not a field to
  // fill in — keep it out of the normal follow-up-question list and its count.
  const splitClar = allClarifications.find((c) => c.kind === 'project_split');
  const clarifications = allClarifications.filter((c) => c.kind !== 'project_split');
  // The agent is asking for Control-M names / schedules — those live in the
  // separate job sheet, so offer to upload it right here in the question card.
  const hasControlmAsk = clarifications.some(
    (c) =>
      c.field_path.endsWith('.control_m_job_name') || c.field_path.endsWith('.schedule_cron'),
  );

  // ── List view ──────────────────────────────────────────────────────

  if (view.kind === 'list') {
    return (
      <div className="onboarding-wizard">
        <div className="obw-intro">
          <div className="obw-intro-title">
            <FileUp size={18} />
            Project Handover Document Import
          </div>
          <p className="obw-intro-sub">
            Upload a handover document (txt / md / csv / html / pdf / screenshot png/jpg), paste a Confluence page URL,
            or paste the full text directly. The AI extracts project, Job, App, and scenario information and generates
            a draft, which you review item by item and fill in missing fields — you can also have the AI run another
            completion pass based on your answers. Confirm everything, then create with one click; created projects
            still go through the existing handover approval flow.
          </p>

          {/* Mode: register a new project, or fold the document into one that
              already exists. Same review form either way. */}
          <div className="obw-mode">
            <button
              type="button"
              className={`obw-mode-card ${mode === 'create' ? 'obw-mode-card-active' : ''}`}
              onClick={() => setMode('create')}
            >
              <span className="obw-mode-title">
                <FilePlus2 size={15} /> Onboard a new project
              </span>
              <span className="obw-mode-sub">
                The document becomes a brand-new Ops project with all its products, Jobs and Apps.
              </span>
            </button>
            <button
              type="button"
              className={`obw-mode-card ${mode === 'update' ? 'obw-mode-card-active' : ''}`}
              onClick={() => setMode('update')}
            >
              <span className="obw-mode-title">
                <FolderPlus size={15} /> Add assets to an existing project
              </span>
              <span className="obw-mode-sub">
                Pick a project you already own; the AI adds what's missing and flags what the document changes.
                Nothing is ever deleted.
              </span>
            </button>
          </div>

          {mode === 'update' && (
            <div className="obw-target">
              <div className="obw-target-head">
                <span>Which project should these assets go into?</span>
                <input
                  className="obw-input obw-target-filter"
                  placeholder="Filter by project or CML name…"
                  value={targetFilter}
                  onChange={(e) => setTargetFilter(e.target.value)}
                />
              </div>
              {targetError && (
                <div className="obw-banner obw-banner-error">
                  <AlertTriangle size={14} />
                  {targetError}
                </div>
              )}
              {targetProjects === null ? (
                <div className="obw-empty">
                  <Loader2 size={16} className="obw-spin" /> Loading your projects…
                </div>
              ) : visibleTargets.length === 0 ? (
                <div className="obw-empty">
                  {targetProjects.length === 0
                    ? 'You have no projects you can edit — onboard a new project instead.'
                    : 'No project matches that filter.'}
                </div>
              ) : (
                <div className="obw-target-list">
                  {visibleTargets.map((p) => (
                    <button
                      type="button"
                      key={p.id}
                      className={`obw-target-item ${targetProjectId === p.id ? 'obw-target-item-active' : ''}`}
                      onClick={() => setTargetProjectId(targetProjectId === p.id ? null : p.id)}
                    >
                      <span className="obw-target-name">{p.name}</span>
                      <span className="obw-target-meta obw-mono">
                        {p.cml_project_name || '(no CML binding)'} · {p.product_count} product(s) ·{' '}
                        {p.job_count} job(s) · {p.app_count} app(s)
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="obw-ingest">
            <div className="obw-ingest-row">
              <input
                ref={fileInputRef}
                type="file"
                accept=".txt,.md,.markdown,.csv,.tsv,.html,.htm,.pdf,.png,.jpg,.jpeg,.webp"
                style={{ display: 'none' }}
                onChange={(e: ChangeEvent<HTMLInputElement>) => setFile(e.target.files?.[0] || null)}
              />
              <button
                type="button"
                className={`obw-file-btn ${file ? 'obw-file-btn-set' : ''}`}
                onClick={() => fileInputRef.current?.click()}
                title="Choose a handover document (txt / md / csv / html / pdf / png / jpg)"
              >
                <FileUp size={15} />
                <span className="obw-file-btn-label">{file ? file.name : 'Choose file…'}</span>
              </button>
              {file && (
                <button
                  type="button"
                  className="obw-file-clear"
                  title="Remove the selected file"
                  onClick={() => {
                    setFile(null);
                    if (fileInputRef.current) fileInputRef.current.value = '';
                  }}
                >
                  <XCircle size={15} />
                </button>
              )}
              <span className="obw-ingest-or">or</span>
            </div>
            <div className="obw-ingest-row">
              <input
                className="obw-input obw-mono"
                placeholder="Confluence page URL (…/pages/<id>/… or ?pageId=… or /display/SPACE/Title)"
                value={pageUrl}
                onChange={(e) => setPageUrl(e.target.value)}
              />
            </div>
            {pageUrl.trim() && confluenceServiceToken && (
              <div className="obw-ingest-row">
                <span className="obw-ingest-note">
                  A Confluence service account is configured; fetching directly, no token needed.
                </span>
              </div>
            )}
            {pageUrl.trim() && !confluenceServiceToken && (
              <div className="obw-ingest-row">
                <input
                  className="obw-input obw-mono"
                  type="password"
                  placeholder="Your Confluence personal access token (PAT) — used only for this fetch, not saved"
                  value={confluenceToken}
                  onChange={(e) => setConfluenceToken(e.target.value)}
                />
              </div>
            )}
            <textarea
              className="obw-input obw-paste"
              rows={5}
              placeholder="Or paste the full handover document text here… (file > URL > pasted text)"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
            />
            {ingestError && (
              <div className="obw-banner obw-banner-error">
                <AlertTriangle size={14} />
                {ingestError}
              </div>
            )}
            <button
              type="button"
              className="obw-btn obw-btn-primary"
              disabled={
                ingesting ||
                (!file && !pasted.trim() && !pageUrl.trim()) ||
                (mode === 'update' && targetProjectId === null)
              }
              onClick={ingest}
              title={
                mode === 'update' && targetProjectId === null
                  ? 'Pick the project to add these assets to first'
                  : undefined
              }
            >
              {ingesting ? <Loader2 size={14} className="obw-spin" /> : <Send size={14} />}
              {mode === 'update' ? 'Start comparison' : 'Start extraction'}
            </button>
          </div>
        </div>

        <div className="obw-list">
          <div className="obw-list-header">
            <span>My drafts</span>
            <button type="button" className="obw-btn" onClick={loadList} title="Refresh">
              <RefreshCw size={13} />
            </button>
          </div>
          {listError && (
            <div className="obw-banner obw-banner-error">
              <AlertTriangle size={14} />
              {listError}
            </div>
          )}
          {drafts === null ? (
            <div className="obw-empty">
              <Loader2 size={16} className="obw-spin" /> Loading…
            </div>
          ) : drafts.length === 0 ? (
            <div className="obw-empty">No drafts yet — upload a handover document to start.</div>
          ) : (
            drafts.map((d) => (
              <div className="obw-row" key={d.id}>
                <button type="button" className="obw-row-open" onClick={() => openDraft(d.id)}>
                  {d.target_project_id != null ? (
                    <FolderPlus size={15} className="obw-row-icon" />
                  ) : (
                    <FileText size={15} className="obw-row-icon" />
                  )}
                  <div className="obw-row-main">
                    <div className="obw-row-title">
                      {d.payload?.project?.name || d.source_filename || `Draft #${d.id}`}
                    </div>
                    <div className="obw-row-sub">
                      {d.target_project_id != null
                        ? `Adding to ${d.diff?.project_name || `project #${d.target_project_id}`} · `
                        : ''}
                      {d.source_filename} · {d.updated_at ? new Date(d.updated_at).toLocaleString() : ''}
                    </div>
                  </div>
                  <StatusChip status={d.status} />
                  <ChevronRight size={15} className="obw-row-chev" />
                </button>
                <button
                  type="button"
                  className="obw-row-del"
                  disabled={deletingId === d.id || BUSY_STATUSES.includes(d.status)}
                  title={
                    BUSY_STATUSES.includes(d.status)
                      ? 'Cannot delete while processing'
                      : 'Delete this draft'
                  }
                  onClick={() => handleDeleteDraft(d)}
                >
                  {deletingId === d.id ? (
                    <Loader2 size={14} className="obw-spin" />
                  ) : (
                    <Trash2 size={14} />
                  )}
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  // ── Group landing page (one cross-project document → N registrations) ──

  if (view.kind === 'group') {
    return (
      <div className="onboarding-wizard">
        <div className="obw-toolbar">
          <button type="button" className="obw-btn" onClick={() => setView({ kind: 'list' })}>
            <ArrowLeft size={14} />
            Back to list
          </button>
        </div>
        <div className="obw-intro">
          <div className="obw-intro-title">
            <FileText size={18} />
            This document was split into {groupDrafts?.length ?? '…'} registrations
          </div>
          <div className="obw-intro-sub">
            Its assets span multiple CML projects, so each becomes its own Ops project.
            Open each card to review and submit it independently.
          </div>
        </div>
        {groupError && (
          <div className="obw-banner obw-banner-error">
            <AlertTriangle size={14} />
            {groupError}
          </div>
        )}
        <div className="obw-list">
          {!groupDrafts ? (
            <div className="obw-empty">
              <Loader2 size={16} className="obw-spin" /> Loading…
            </div>
          ) : (
            groupDrafts.map((d) => {
              const proj = d.payload?.project;
              const cml = proj?.cml_project_name || '(no CML project)';
              const jobCount = (d.payload?.products ?? []).reduce((n, p) => n + p.jobs.length, 0);
              const appCount = (d.payload?.products ?? []).reduce((n, p) => n + p.apps.length, 0);
              return (
                <div className="obw-row" key={d.id}>
                  <button type="button" className="obw-row-open" onClick={() => openDraft(d.id)}>
                    <FileText size={15} className="obw-row-icon" />
                    <div className="obw-row-main">
                      <div className="obw-row-title">{proj?.name || `Draft #${d.id}`}</div>
                      <div className="obw-row-sub obw-mono">
                        {cml} · {jobCount} job(s) · {appCount} app(s)
                      </div>
                    </div>
                    <StatusChip status={d.status} />
                    <ChevronRight size={15} className="obw-row-chev" />
                  </button>
                </div>
              );
            })
          )}
        </div>
      </div>
    );
  }

  // ── Detail view ────────────────────────────────────────────────────

  const backBtn = (
    <button
      type="button"
      className="obw-btn"
      onClick={() =>
        draft?.group_id
          ? setView({ kind: 'group', groupId: draft.group_id })
          : setView({ kind: 'list' })
      }
    >
      <ArrowLeft size={14} />
      {draft?.group_id ? 'Back to this document’s registrations' : 'Back to list'}
    </button>
  );

  if (!draft) {
    return (
      <div className="onboarding-wizard">
        <div className="obw-toolbar">{backBtn}</div>
        {detailError ? (
          <div className="obw-banner obw-banner-error">
            <AlertTriangle size={14} />
            {detailError}
          </div>
        ) : (
          <div className="obw-empty">
            <Loader2 size={16} className="obw-spin" /> Loading draft…
          </div>
        )}
      </div>
    );
  }

  if (BUSY_STATUSES.includes(draft.status)) {
    return (
      <div className="onboarding-wizard">
        <div className="obw-toolbar">
          {backBtn}
          <StatusChip status={draft.status} />
        </div>
        <div className="obw-centered">
          <Loader2 size={28} className="obw-spin" />
          <div className="obw-centered-title">
            {draft.status === 'extracting'
              ? updating
                ? 'AI is reading the document and comparing it against the project…'
                : 'AI is reading the document and distilling key information…'
              : draft.status === 'refining'
                ? 'AI is running a second completion pass based on your answers… (filled fields are left untouched)'
                : updating
                  ? 'Applying the changes to the project…'
                  : 'Creating entities…'}
          </div>
          <div className="obw-centered-sub">
            {draft.source_filename} · the page refreshes automatically, no action needed
          </div>
        </div>
      </div>
    );
  }

  if (draft.status === 'completed') {
    const result = draft.result;
    return (
      <div className="onboarding-wizard">
        <div className="obw-toolbar">
          {backBtn}
          <StatusChip status={draft.status} />
        </div>
        <div className="obw-banner obw-banner-success">
          <CheckCircle2 size={15} />
          {result?.mode === 'update' ? (
            <span>
              Project updated: {result.created?.jobs ?? 0} Job(s), {result.created?.apps ?? 0} App(s),{' '}
              {result.created?.products ?? 0} Product(s) and {result.created?.scenarios ?? 0}{' '}
              scenario(s) added; {result.updated?.jobs ?? 0} Job(s), {result.updated?.apps ?? 0}{' '}
              App(s) and {result.updated?.scenarios ?? 0} scenario(s) amended. The changes land in
              the project's editable draft version — submit it through the existing handover flow
              to get them approved.
            </span>
          ) : (
            'Creation complete. The project is currently in draft / pending-approval state; submit it for approval via the existing handover flow.'
          )}
        </div>
        {result && (
          <div className="obw-result">
            <div className="obw-card">
              <div className="obw-card-header">
                <span className="obw-card-title">
                  Project #{result.project_id} ·{' '}
                  {result.project_name || draft.payload?.project?.name || ''}
                </span>
                {onOpenProject && (
                  <button
                    type="button"
                    className="obw-btn obw-btn-primary"
                    onClick={() => onOpenProject(result.project_id)}
                  >
                    View project
                  </button>
                )}
              </div>
              {result.products.map((p) => (
                <div className="obw-result-product" key={p.product_id}>
                  <div className="obw-result-product-name">
                    Product #{p.product_id} · {p.name}
                  </div>
                  <div className="obw-result-counts">
                    {p.jobs.length} Jobs · {p.apps.length} Apps
                  </div>
                </div>
              ))}
            </div>
            <div className="obw-card">
              <div className="obw-card-header">
                <span className="obw-card-title">CML binding check</span>
              </div>
              {result.bindings.length === 0 ? (
                <div className="obw-empty">No entities need binding.</div>
              ) : (
                <table className="obw-bindings">
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Name</th>
                      <th>Binding status</th>
                      <th>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.bindings.map((b, i) => (
                      <tr key={i}>
                        <td>{b.entity === 'job' ? 'Job' : 'App'}</td>
                        <td className="obw-mono">{b.name}</td>
                        <td>
                          {!b.applicable ? (
                            <span className="obw-bind obw-bind-na">N/A</span>
                          ) : b.bound ? (
                            <span className="obw-bind obw-bind-ok">
                              <CheckCircle2 size={13} /> Bound
                            </span>
                          ) : (
                            <span className="obw-bind obw-bind-fail">
                              <XCircle size={13} /> Not bound
                            </span>
                          )}
                        </td>
                        <td className="obw-bind-error">
                          {b.error || (b.applicable && !b.bound ? 'When CML is unreachable, monitoring retries the binding automatically' : '')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}
      </div>
    );
  }

  // ready / failed → preview & edit
  if (!payload) {
    return (
      <div className="onboarding-wizard">
        <div className="obw-toolbar">
          {backBtn}
          <StatusChip status={draft.status} />
        </div>
        <div className="obw-banner obw-banner-error">
          <AlertTriangle size={14} />
          {draft.error || 'Extraction failed; there is no editable draft content. Go back to the list and re-upload the document.'}
        </div>
      </div>
    );
  }

  return (
    <div className="onboarding-wizard">
      <div className="obw-toolbar">
        {backBtn}
        <div className="obw-toolbar-info">
          <span className="obw-toolbar-file">{draft.source_filename}</span>
          <StatusChip status={draft.status} />
          {updating && (
            <>
              <span className="obw-count obw-change obw-change-new">{diffCounts.created} new</span>
              <span className="obw-count obw-change obw-change-update">
                {diffCounts.updated} updated
              </span>
            </>
          )}
          {errorCount > 0 && <span className="obw-count obw-count-error">{errorCount} errors</span>}
          {warningCount > 0 && (
            <span className="obw-count obw-count-warn">{warningCount} warnings</span>
          )}
          {clarifications.length > 0 && (
            <span className="obw-count obw-count-ask">{clarifications.length} to fill in</span>
          )}
        </div>
        <div className="obw-toolbar-actions">
          {updating && diffCounts.unchanged > 0 && (
            <button
              type="button"
              className="obw-btn"
              onClick={() => setShowUnchanged((v) => !v)}
              title="Untouched assets are shown for context; hide them to review only what this document moves"
            >
              {showUnchanged
                ? `Hide ${diffCounts.unchanged} unchanged`
                : `Show ${diffCounts.unchanged} unchanged`}
            </button>
          )}
          <input
            ref={controlmInputRef}
            type="file"
            accept=".xlsx,.csv,.tsv,.txt,.html,.htm"
            style={{ display: 'none' }}
            onChange={(e: ChangeEvent<HTMLInputElement>) => {
              const f = e.target.files?.[0];
              if (f) handleControlmUpload(f);
            }}
          />
          <button
            type="button"
            className="obw-btn"
            disabled={busy}
            onClick={() => controlmInputRef.current?.click()}
            title="Upload the Control-M job configuration sheet (Excel/CSV) to auto-fill real Control-M names and schedules"
          >
            <Table2 size={14} />
            Import Control-M sheet
          </button>
          <button type="button" className="obw-btn" disabled={busy} onClick={handleSave}>
            {busy ? <Loader2 size={14} className="obw-spin" /> : <Save size={14} />}
            Save{dirty || hasPendingAnswers ? '*' : ''}
          </button>
          <button
            type="button"
            className="obw-btn obw-btn-danger"
            disabled={busy}
            onClick={handleDeleteCurrent}
            title="Delete this draft"
          >
            <Trash2 size={14} />
            Delete draft
          </button>
          <button
            type="button"
            className="obw-btn obw-btn-primary"
            disabled={busy || (errorCount > 0 && !dirty && !hasPendingAnswers)}
            onClick={handleSubmit}
            title={
              errorCount > 0
                ? 'Validation errors exist; fix and save before submitting'
                : updating
                  ? 'Add the new assets and apply the highlighted changes to the project'
                  : 'Create the project and all assets'
            }
          >
            <Send size={14} />
            {updating ? 'Update the project' : 'Submit & create'}
          </button>
        </div>
      </div>

      <FormOutline payload={payload} />

      {draft.status === 'failed' && (
        <div className="obw-banner obw-banner-error">
          <AlertTriangle size={14} />
          {draft.error || 'The last operation failed'} — the draft is still editable; save and resubmit.
        </div>
      )}
      {detailError && (
        <div className="obw-banner obw-banner-error">
          <AlertTriangle size={14} />
          {detailError}
        </div>
      )}

      {updating && (
        <div className="obw-banner obw-banner-info">
          <FolderPlus size={14} />
          <span>
            Adding to the existing project{' '}
            <strong>{issues.diff.projectName || `#${draft.target_project_id}`}</strong> — assets
            marked <span className="obw-change obw-change-new">New</span> will be created,{' '}
            <span className="obw-change obw-change-update">Updated</span> ones will have the
            highlighted fields overwritten, and the rest stay as they are. Nothing is deleted:
            removing a row here only drops it from this review. Renaming a Job's CML/Control-M name
            (or an App's CML name) makes it a <em>new</em> asset rather than a rename — the badge
            updates after you save.
          </span>
        </div>
      )}

      {draft.group_id && (
        <div className="obw-banner obw-banner-info">
          <FileText size={14} />
          This is one of several registrations split from the same document.
          <button
            type="button"
            className="obw-link"
            onClick={() => setView({ kind: 'group', groupId: draft.group_id! })}
          >
            View all registrations
          </button>
        </div>
      )}

      {splitClar && (
        <div className="obw-card obw-card-ask">
          <div className="obw-card-header">
            <span className="obw-card-title">
              <ChevronRight size={15} /> Cross-project document detected
            </span>
          </div>
          <div className="obw-ask">
            <div className="obw-ask-q">{splitClar.question}</div>
            {(splitClar.options ?? []).length > 0 && (
              <div className="obw-ask-options">
                {(splitClar.options ?? []).map((o) => (
                  <span key={o.value} className="obw-option" title={o.detail}>
                    <span className="obw-option-value obw-mono">{o.value}</span>
                    {o.detail && <span className="obw-option-detail">{o.detail}</span>}
                  </span>
                ))}
              </div>
            )}
            <div className="obw-ask-row">
              <button
                type="button"
                className="obw-btn obw-btn-primary"
                disabled={busy}
                onClick={handleSplit}
                title="Create one separate draft per CML project"
              >
                {busy ? <Loader2 size={14} className="obw-spin" /> : <ChevronRight size={14} />}
                Split into {(splitClar.options ?? []).length} registrations
              </button>
            </div>
          </div>
        </div>
      )}

      {clarifications.length > 0 && (
        <div className="obw-card obw-card-ask">
          <div className="obw-card-header">
            <span className="obw-card-title">
              <HelpCircle size={15} /> Assistant's follow-up questions ({clarifications.length})
            </span>
            <span className="obw-card-hint">
              Answers are written straight into the form by field path (no AI involved); answered questions disappear after saving
            </span>
          </div>
          {hasControlmAsk && (
            <div className="obw-ask-controlm">
              <span>
                Need to fill in real Control-M names / schedules? Those live in a separate Control-M job
                configuration sheet — just upload it and it's auto-detected and written back to the matching Jobs (Excel / CSV supported).
              </span>
              <button
                type="button"
                className="obw-btn obw-btn-primary"
                disabled={busy}
                onClick={() => controlmInputRef.current?.click()}
              >
                <Table2 size={13} /> Upload Control-M sheet
              </button>
            </div>
          )}
          {clarifications.map((c) => {
            const opts = c.options ?? [];
            const suggestKind =
              c.kind === 'cml_project' ? 'cml' : c.kind === 'mmp_project' ? 'mmp' : null;
            return (
              <div className="obw-ask" key={c.id}>
                <div className="obw-ask-q">{c.question}</div>
                {opts.length > 0 && (
                  <div className="obw-ask-options">
                    {opts.map((o) => (
                      <button
                        type="button"
                        key={o.value}
                        className={`obw-option ${answers[c.id] === o.value ? 'obw-option-active' : ''}`}
                        title={o.detail}
                        onClick={() =>
                          setAnswers((prev) => ({
                            ...prev,
                            [c.id]: prev[c.id] === o.value ? '' : o.value,
                          }))
                        }
                      >
                        <span className="obw-option-value obw-mono">{o.value}</span>
                        {o.detail && <span className="obw-option-detail">{o.detail}</span>}
                      </button>
                    ))}
                  </div>
                )}
                <div className="obw-ask-row">
                  {suggestKind ? (
                    <SuggestInput
                      value={answers[c.id] || ''}
                      onChange={(v) => setAnswers((prev) => ({ ...prev, [c.id]: v }))}
                      kind={suggestKind}
                      mono
                      placeholder="Click a candidate, or search/type here; written to the field after saving"
                    />
                  ) : (
                    <input
                      className="obw-input"
                      placeholder="Answer here, or edit the matching field in the form below"
                      value={answers[c.id] || ''}
                      onChange={(e) => setAnswers((prev) => ({ ...prev, [c.id]: e.target.value }))}
                    />
                  )}
                  <span className="obw-ask-path obw-mono">{c.field_path}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Multi-round review: hand the draft (with this round's answers/edits)
          back to the AI for a second-pass fill, then review again. */}
      <div className="obw-card obw-card-ask">
        <div className="obw-card-header">
          <span className="obw-card-title">
            <RefreshCw size={15} /> AI re-completion (repeatable)
          </span>
          <span className="obw-card-hint">
            Answer the questions / edit the form first — your changes and answers are preserved as-is; the AI only fills the remaining blanks and gaps
          </span>
        </div>
        <div className="obw-ask-row">
          <input
            className="obw-input"
            placeholder="(Optional) extra notes for the AI: e.g. which job was missed, which scenario needs expanding, which part was misread…"
            value={refineComment}
            onChange={(e) => setRefineComment(e.target.value)}
          />
          <button
            type="button"
            className="obw-btn obw-btn-primary"
            disabled={busy}
            onClick={handleRefine}
            title="Save the current edits and answers, then have the AI run a second completion pass"
          >
            {busy ? <Loader2 size={14} className="obw-spin" /> : <RefreshCw size={14} />}
            Let AI complete again
          </button>
        </div>
      </div>

      {warningCount > 0 && (
        <details className="obw-card obw-card-warn obw-warn-details">
          <summary className="obw-card-header obw-warn-summary">
            <span className="obw-card-title">
              <AlertTriangle size={15} /> Notices ({warningCount}) — do not block submission
            </span>
            <ChevronRight size={15} className="obw-warn-chev" />
          </summary>
          <ul className="obw-warn-list">
            {(draft.validation?.warnings ?? []).map((w, i) => (
              <li key={i}>
                {w.field_path && <span className="obw-mono obw-warn-path">{w.field_path}</span>}
                {w.message}
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* ── Project section ── */}
      <div className="obw-card" id="obw-anchor-project">
        <div className="obw-card-header">
          <span className="obw-card-title">Project info</span>
          {updating && <ChangeBadge change={issues.diff.change('project')} />}
        </div>
        {updating && (
          <div className="obw-card-hint">
            Project-level fields the document only fills in (blanks) are proposed here; where it
            disagreed with a value already set, the existing one was kept and a notice raised. The
            project name is never changed by an import.
          </div>
        )}
        <div className="obw-grid">
          <Field
            label={updating ? 'Project name (not changed by an import)' : 'Project name'}
            required
            path="project.name"
            value={payload.project.name}
            issues={issues}
            onChange={(v) => mutate((p) => void (p.project.name = v))}
          />
          <Field
            label="Project Owner (used to infer email when owner contact is missing)"
            path="project.owner_name"
            value={payload.project.owner_name}
            issues={issues}
            placeholder="e.g. Alex Morgan"
            onChange={(v) => mutate((p) => void (p.project.owner_name = v))}
          />
          <Field
            label="CML Project name"
            path="project.cml_project_name"
            value={payload.project.cml_project_name}
            issues={issues}
            mono
            suggest="cml"
            onChange={(v) => mutate((p) => void (p.project.cml_project_name = v))}
          />
          <Field
            label="MMP Project ID"
            path="project.mmp_project_id"
            value={payload.project.mmp_project_id}
            issues={issues}
            mono
            suggest="mmp"
            onChange={(v) => mutate((p) => void (p.project.mmp_project_id = v))}
          />
          <Field
            label="Prod Stat URL"
            path="project.prod_stat_url"
            value={payload.project.prod_stat_url}
            issues={issues}
            mono
            onChange={(v) => mutate((p) => void (p.project.prod_stat_url = v))}
          />
        </div>
        <AreaField
          label="Project description"
          value={payload.project.description}
          onChange={(v) => mutate((p) => void (p.project.description = v))}
        />
      </div>

      {/* ── Products ── */}
      {payload.products.map((product, pi) => {
        const productChange = issues.diff.change(`products[${pi}]`);
        // Indices stay authoritative — a hidden row is skipped in place, never
        // filtered out, so every `mutate` path below still addresses the right
        // entry in the payload.
        if (updating && !showUnchanged && productChange === 'unchanged') return null;
        return (
        <div className="obw-card" id={`obw-anchor-product-${pi}`} key={pi}>
          <div className="obw-card-header">
            <span className="obw-card-title">
              Product #{pi + 1}
              {updating && <ChangeBadge change={productChange} />}
            </span>
            <button
              type="button"
              className="obw-btn obw-btn-danger"
              onClick={() => mutate((p) => void p.products.splice(pi, 1))}
              title={updating ? 'Remove from this review (the project keeps it)' : undefined}
            >
              <Trash2 size={13} /> {updating ? 'Remove from review' : 'Delete Product'}
            </button>
          </div>
          <div className="obw-grid">
            <Field
              label="Product name"
              required
              path={`products[${pi}].name`}
              value={product.name}
              issues={issues}
              onChange={(v) => mutate((p) => void (p.products[pi].name = v))}
            />
          </div>

          {/* Jobs */}
          <div className="obw-section-header">
            <span>Jobs ({product.jobs.length})</span>
            <button
              type="button"
              className="obw-btn"
              onClick={() => mutate((p) => void p.products[pi].jobs.push(emptyJob()))}
            >
              <Plus size={13} /> Add Job
            </button>
          </div>
          {product.jobs.map((job, ji) => {
            const jpath = `products[${pi}].jobs[${ji}]`;
            const rowErrs = issues.errors[jpath] || [];
            const jobChange = issues.diff.change(jpath);
            if (updating && !showUnchanged && jobChange === 'unchanged') return null;
            return (
              <div
                className={`obw-entity ${rowErrs.length ? 'obw-entity-error' : ''} ${jobChange ? `obw-entity-${jobChange}` : ''}`}
                id={`obw-anchor-job-${pi}-${ji}`}
                key={ji}
              >
                <div className="obw-entity-header">
                  <span className="obw-entity-title">
                    Job: {job.cml_job_name || job.control_m_job_name || `#${ji + 1}`}
                    {updating && <ChangeBadge change={jobChange} />}
                  </span>
                  <button
                    type="button"
                    className="obw-btn obw-btn-danger"
                    onClick={() => mutate((p) => void p.products[pi].jobs.splice(ji, 1))}
                    title={updating ? 'Remove from this review (the project keeps it)' : undefined}
                  >
                    <Trash2 size={13} /> {updating ? 'Remove from review' : 'Delete'}
                  </button>
                </div>
                {rowErrs.map((m, i) => (
                  <div className="obw-field-msg" key={i}>
                    {m}
                  </div>
                ))}
                {updating && <ChangedFields issues={issues} path={jpath} />}
                <SourceQuote quote={job.source_quote} />
                <div className="obw-grid">
                  <Field
                    label="CML Project name"
                    path={`${jpath}.cml_project_name`}
                    value={job.cml_project_name}
                    issues={issues}
                    mono
                    suggest="cml"
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].cml_project_name = v))}
                  />
                  <Field
                    label="CML Job name"
                    path={`${jpath}.cml_job_name`}
                    value={job.cml_job_name}
                    issues={issues}
                    mono
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].cml_job_name = v))}
                  />
                  <Field
                    label="Control-M real name"
                    path={`${jpath}.control_m_job_name`}
                    value={job.control_m_job_name}
                    issues={issues}
                    mono
                    onChange={(v) =>
                      mutate((p) => void (p.products[pi].jobs[ji].control_m_job_name = v))
                    }
                  />
                  <Field
                    label="Expected schedule cron"
                    path={`${jpath}.schedule_cron`}
                    value={job.schedule_cron}
                    issues={issues}
                    mono
                    placeholder="e.g. 0 2 * * 1-5"
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].schedule_cron = v))}
                  />
                  <Field
                    label="Control-M cron"
                    path={`${jpath}.control_m_cron`}
                    value={job.control_m_cron}
                    issues={issues}
                    mono
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].control_m_cron = v))}
                  />
                  <Field
                    label="MMP Project ID"
                    path={`${jpath}.mmp_project_id`}
                    value={job.mmp_project_id}
                    issues={issues}
                    mono
                    suggest="mmp"
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].mmp_project_id = v))}
                  />
                  <Field
                    label="MMP Model ID"
                    path={`${jpath}.mmp_model_id`}
                    value={job.mmp_model_id}
                    issues={issues}
                    mono
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].mmp_model_id = v))}
                  />
                  <Field
                    label="Owner Contact (project POC)"
                    path={`${jpath}.owner_contact`}
                    value={job.owner_contact}
                    issues={issues}
                    onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].owner_contact = v))}
                  />
                </div>
                <AreaField
                  label="Description"
                  value={job.description}
                  onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].description = v))}
                />
                <AreaField
                  label="Dependency notes"
                  value={job.dependency_notes}
                  onChange={(v) => mutate((p) => void (p.products[pi].jobs[ji].dependency_notes = v))}
                />

                <div className="obw-section-header obw-section-sub">
                  <span>Failure scenario runbooks ({job.scenarios.length})</span>
                  <button
                    type="button"
                    className="obw-btn"
                    onClick={() =>
                      mutate(
                        (p) =>
                          void p.products[pi].jobs[ji].scenarios.push({
                            scenario_type: 'not_triggered',
                            scenario_name: '',
                            condition_description: '',
                            diagnostic_steps: [],
                            action_steps: [],
                            verification_steps: [],
                            escalation_target: '',
                            email_template: emptyEmailTemplate(),
                            source_quote: '',
                          }),
                      )
                    }
                  >
                    <Plus size={13} /> Add scenario
                  </button>
                </div>
                {job.scenarios.map((sc, si) => {
                  const spath = `${jpath}.scenarios[${si}]`;
                  const scChange = issues.diff.change(spath);
                  return (
                    <div
                      className={`obw-scenario ${scChange ? `obw-entity-${scChange}` : ''}`}
                      key={si}
                    >
                      <div className="obw-entity-header">
                        <span className="obw-entity-title">
                          Scenario #{si + 1}
                          {updating && <ChangeBadge change={scChange} />}
                        </span>
                        <button
                          type="button"
                          className="obw-btn obw-btn-danger"
                          onClick={() =>
                            mutate((p) => void p.products[pi].jobs[ji].scenarios.splice(si, 1))
                          }
                          title={updating ? 'Remove from this review (the project keeps it)' : undefined}
                        >
                          <Trash2 size={13} /> {updating ? 'Remove' : 'Delete'}
                        </button>
                      </div>
                      {updating && <ChangedFields issues={issues} path={spath} />}
                      <SourceQuote quote={sc.source_quote} />
                      <div className="obw-grid">
                        <SelectField
                          label="Scenario type"
                          path={`${spath}.scenario_type`}
                          value={sc.scenario_type}
                          options={JOB_SCENARIO_TYPES}
                          issues={issues}
                          onChange={(v) =>
                            mutate(
                              (p) => void (p.products[pi].jobs[ji].scenarios[si].scenario_type = v),
                            )
                          }
                        />
                        <Field
                          label="Scenario name"
                          path={`${spath}.scenario_name`}
                          value={sc.scenario_name}
                          issues={issues}
                          onChange={(v) =>
                            mutate(
                              (p) => void (p.products[pi].jobs[ji].scenarios[si].scenario_name = v),
                            )
                          }
                        />
                        <Field
                          label="Escalation contact"
                          path={`${spath}.escalation_target`}
                          value={sc.escalation_target}
                          issues={issues}
                          onChange={(v) =>
                            mutate(
                              (p) =>
                                void (p.products[pi].jobs[ji].scenarios[si].escalation_target = v),
                            )
                          }
                        />
                      </div>
                      <AreaField
                        label="Trigger condition (copy verbatim)"
                        value={sc.condition_description}
                        onChange={(v) =>
                          mutate(
                            (p) =>
                              void (p.products[pi].jobs[ji].scenarios[si].condition_description = v),
                          )
                        }
                      />
                      <StepsField
                        label="Diagnostic steps"
                        value={sc.diagnostic_steps}
                        onChange={(v) =>
                          mutate(
                            (p) => void (p.products[pi].jobs[ji].scenarios[si].diagnostic_steps = v),
                          )
                        }
                      />
                      <StepsField
                        label="Action steps"
                        value={sc.action_steps}
                        onChange={(v) =>
                          mutate((p) => void (p.products[pi].jobs[ji].scenarios[si].action_steps = v))
                        }
                      />
                      <StepsField
                        label="Verification steps"
                        value={sc.verification_steps}
                        onChange={(v) =>
                          mutate(
                            (p) =>
                              void (p.products[pi].jobs[ji].scenarios[si].verification_steps = v),
                          )
                        }
                      />
                      <EmailTemplateFields
                        template={sc.email_template}
                        assetKind="job"
                        scenarioType={sc.scenario_type}
                        jobName={job.cml_job_name || job.control_m_job_name}
                        onChange={(patch) =>
                          mutate((p) => {
                            const target = p.products[pi].jobs[ji].scenarios[si];
                            target.email_template = {
                              ...emptyEmailTemplate(),
                              ...(target.email_template || {}),
                              ...patch,
                            };
                          })
                        }
                      />
                    </div>
                  );
                })}
              </div>
            );
          })}

          {/* Apps */}
          <div className="obw-section-header">
            <span>Apps ({product.apps.length})</span>
            <button
              type="button"
              className="obw-btn"
              onClick={() => mutate((p) => void p.products[pi].apps.push(emptyApp()))}
            >
              <Plus size={13} /> Add App
            </button>
          </div>
          {product.apps.map((app, ai) => {
            const apath = `products[${pi}].apps[${ai}]`;
            const rowErrs = issues.errors[apath] || [];
            const appChange = issues.diff.change(apath);
            if (updating && !showUnchanged && appChange === 'unchanged') return null;
            return (
              <div
                className={`obw-entity ${rowErrs.length ? 'obw-entity-error' : ''} ${appChange ? `obw-entity-${appChange}` : ''}`}
                id={`obw-anchor-app-${pi}-${ai}`}
                key={ai}
              >
                <div className="obw-entity-header">
                  <span className="obw-entity-title">
                    App：{app.cml_application_name || `#${ai + 1}`}
                    {updating && <ChangeBadge change={appChange} />}
                  </span>
                  <button
                    type="button"
                    className="obw-btn obw-btn-danger"
                    onClick={() => mutate((p) => void p.products[pi].apps.splice(ai, 1))}
                    title={updating ? 'Remove from this review (the project keeps it)' : undefined}
                  >
                    <Trash2 size={13} /> {updating ? 'Remove from review' : 'Delete'}
                  </button>
                </div>
                {rowErrs.map((m, i) => (
                  <div className="obw-field-msg" key={i}>
                    {m}
                  </div>
                ))}
                {updating && <ChangedFields issues={issues} path={apath} />}
                <SourceQuote quote={app.source_quote} />
                <div className="obw-grid">
                  <Field
                    label="CML Application name"
                    required
                    path={`${apath}.cml_application_name`}
                    value={app.cml_application_name}
                    issues={issues}
                    mono
                    onChange={(v) =>
                      mutate((p) => void (p.products[pi].apps[ai].cml_application_name = v))
                    }
                  />
                  <Field
                    label="CML Project name"
                    path={`${apath}.cml_project_name`}
                    value={app.cml_project_name}
                    issues={issues}
                    mono
                    suggest="cml"
                    onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].cml_project_name = v))}
                  />
                  <SelectField
                    label="App type"
                    path={`${apath}.cml_app_type`}
                    value={app.cml_app_type}
                    options={APP_TYPES.map((t) => ({ value: t, label: t }))}
                    issues={issues}
                    onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].cml_app_type = v))}
                  />
                  <Field
                    label="CML Subdomain"
                    path={`${apath}.cml_subdomain`}
                    value={app.cml_subdomain}
                    issues={issues}
                    mono
                    onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].cml_subdomain = v))}
                  />
                  <Field
                    label="Access URL"
                    path={`${apath}.application_url`}
                    value={app.application_url}
                    issues={issues}
                    mono
                    placeholder="https://…"
                    onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].application_url = v))}
                  />
                  <Field
                    label="Health check URL"
                    path={`${apath}.health_check_url`}
                    value={app.health_check_url}
                    issues={issues}
                    mono
                    onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].health_check_url = v))}
                  />
                  <Field
                    label="Owner Contact"
                    path={`${apath}.owner_contact`}
                    value={app.owner_contact}
                    issues={issues}
                    onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].owner_contact = v))}
                  />
                </div>
                <AreaField
                  label="Description"
                  value={app.description}
                  onChange={(v) => mutate((p) => void (p.products[pi].apps[ai].description = v))}
                />

                <div className="obw-section-header obw-section-sub">
                  <span>Recovery scenario runbooks ({app.scenarios.length})</span>
                  <button
                    type="button"
                    className="obw-btn"
                    onClick={() =>
                      mutate(
                        (p) =>
                          void p.products[pi].apps[ai].scenarios.push({
                            scenario_type: 'offline',
                            scenario_name: '',
                            condition_description: '',
                            action_steps: [],
                            verification_steps: [],
                            escalation_target: '',
                            email_template: emptyEmailTemplate(),
                            source_quote: '',
                          }),
                      )
                    }
                  >
                    <Plus size={13} /> Add scenario
                  </button>
                </div>
                {app.scenarios.map((sc, si) => {
                  const spath = `${apath}.scenarios[${si}]`;
                  const scChange = issues.diff.change(spath);
                  return (
                    <div
                      className={`obw-scenario ${scChange ? `obw-entity-${scChange}` : ''}`}
                      key={si}
                    >
                      <div className="obw-entity-header">
                        <span className="obw-entity-title">
                          Scenario #{si + 1}
                          {updating && <ChangeBadge change={scChange} />}
                        </span>
                        <button
                          type="button"
                          className="obw-btn obw-btn-danger"
                          onClick={() =>
                            mutate((p) => void p.products[pi].apps[ai].scenarios.splice(si, 1))
                          }
                          title={updating ? 'Remove from this review (the project keeps it)' : undefined}
                        >
                          <Trash2 size={13} /> {updating ? 'Remove' : 'Delete'}
                        </button>
                      </div>
                      {updating && <ChangedFields issues={issues} path={spath} />}
                      <SourceQuote quote={sc.source_quote} />
                      <div className="obw-grid">
                        <SelectField
                          label="Scenario type"
                          path={`${spath}.scenario_type`}
                          value={sc.scenario_type}
                          options={APP_SCENARIO_TYPES}
                          issues={issues}
                          onChange={(v) =>
                            mutate(
                              (p) => void (p.products[pi].apps[ai].scenarios[si].scenario_type = v),
                            )
                          }
                        />
                        <Field
                          label="Scenario name"
                          path={`${spath}.scenario_name`}
                          value={sc.scenario_name}
                          issues={issues}
                          onChange={(v) =>
                            mutate(
                              (p) => void (p.products[pi].apps[ai].scenarios[si].scenario_name = v),
                            )
                          }
                        />
                        <Field
                          label="Escalation contact"
                          path={`${spath}.escalation_target`}
                          value={sc.escalation_target}
                          issues={issues}
                          onChange={(v) =>
                            mutate(
                              (p) =>
                                void (p.products[pi].apps[ai].scenarios[si].escalation_target = v),
                            )
                          }
                        />
                      </div>
                      <AreaField
                        label="Trigger condition (copy verbatim)"
                        value={sc.condition_description}
                        onChange={(v) =>
                          mutate(
                            (p) =>
                              void (p.products[pi].apps[ai].scenarios[si].condition_description = v),
                          )
                        }
                      />
                      <StepsField
                        label="Action steps"
                        value={sc.action_steps}
                        onChange={(v) =>
                          mutate((p) => void (p.products[pi].apps[ai].scenarios[si].action_steps = v))
                        }
                      />
                      <StepsField
                        label="Verification steps"
                        value={sc.verification_steps}
                        onChange={(v) =>
                          mutate(
                            (p) =>
                              void (p.products[pi].apps[ai].scenarios[si].verification_steps = v),
                          )
                        }
                      />
                      <EmailTemplateFields
                        template={sc.email_template}
                        assetKind="app"
                        scenarioType={sc.scenario_type}
                        appName={app.cml_application_name}
                        onChange={(patch) =>
                          mutate((p) => {
                            const target = p.products[pi].apps[ai].scenarios[si];
                            target.email_template = {
                              ...emptyEmailTemplate(),
                              ...(target.email_template || {}),
                              ...patch,
                            };
                          })
                        }
                      />
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
        );
      })}

      <div className="obw-add-product">
        <button
          type="button"
          className="obw-btn"
          onClick={() => mutate((p) => void p.products.push({ name: '', jobs: [], apps: [] }))}
        >
          <Plus size={14} /> Add Product
        </button>
      </div>

      {draft.source_text && (
        <details className="obw-source">
          <summary>View source ({draft.source_filename || 'pasted text'})</summary>
          <pre>{draft.source_text}</pre>
        </details>
      )}
    </div>
  );
}
