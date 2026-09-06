/**
 * Owner-notification email template — shared, framework-agnostic engine.
 *
 * The persisted shape is just ``{to, cc, subject, body}``. ``body`` is a
 * multi-line string with a fixed intro/outro and a middle block of
 * "Label: value" rows (Order Date / Application / Job / Type of Request / …).
 * That middle block is treated as a structured row list at edit time so an
 * owner can add / delete / reorder / rename rows without hand-editing free
 * text, and per-scenario defaults can be seeded automatically.
 *
 * Tokens in double-braces are substituted at send time by
 * ``applyEmailTemplateTokens``:
 *   - ``{{date}}``        → today's date (YYYY-MM-DD)
 *   - ``{{job_name}}``    → the job's name (job scenarios)
 *   - ``{{app_name}}``    → the application's name (app scenarios)
 *   - ``{{sender_name}}`` → the signed-in user's display name
 *
 * Both the asset-edit page (App.tsx) and the onboarding wizard import these so
 * the two render identically — the wizard reuses the exact serialize/parse so a
 * draft template round-trips into the same labelled-row layout owners expect.
 */

export type EmailBodyRow = { label: string; value: string };

export type EmailAssetKind = 'job' | 'app';

export const EMAIL_BODY_INTRO =
  'Please kindly perform the following job requests. Approval email is attached above.';
export const EMAIL_BODY_OUTRO_LINES = ['Thanks,', '{{sender_name}}'];

/** Default rows seeded for a *job* scenario template. Order matches the legacy
 *  fixed body so owners get exactly the same layout they expect today. Type of
 *  Request is overridden per scenario type by ``buildDefaultEmailBody``. */
export const DEFAULT_JOB_EMAIL_ROWS: EmailBodyRow[] = [
  { label: 'Order Date', value: '{{date}}' },
  { label: 'Application', value: '' },
  { label: 'Group', value: '' },
  { label: 'Table', value: '' },
  { label: 'Job', value: '{{job_name}}' },
  { label: 'Type of Request', value: 'Re-Run' },
  { label: 'Financial Impact', value: 'No Impact' },
];

/** Default rows seeded for an *application* scenario template. Group / Table /
 *  Job are dropped (job-specific); Application auto-fills from the asset. */
export const DEFAULT_APP_EMAIL_ROWS: EmailBodyRow[] = [
  { label: 'Order Date', value: '{{date}}' },
  { label: 'Application', value: '{{app_name}}' },
  { label: 'Type of Request', value: 'Investigation' },
  { label: 'Financial Impact', value: 'No Impact' },
];

/** Rows the editor offers as "+ Add …" quick-buttons. */
export const OPTIONAL_EMAIL_ROW_TEMPLATES: EmailBodyRow[] = [
  { label: 'CHG/TSK Number', value: '' },
];

/** Per-scenario default for the "Type of Request" row, applied only when a
 *  scenario's template is first seeded; existing user edits are never touched. */
export const TYPE_OF_REQUEST_BY_SCENARIO_TYPE: Record<string, string> = {
  // Job failure scenarios
  not_triggered: 'Trigger',
  triggered_but_failed: 'Re-Run',
  dependency_failed: 'Re-Run',
  mmp_drift_detected: 'Investigation',
  mmp_fairness_risk: 'Investigation',
  mmp_run_pending_approval: 'Approval Request',
  mmp_unapproved_exp_run: 'Approval Request',
  mmp_perf_drift: 'Investigation',
  mmp_feature_drift: 'Investigation',
  mmp_data_quality_drift: 'Investigation',
  mmp_no_significant_drift: 'Investigation',
  pass_threshold: 'Investigation',
  fail_threshold: 'Investigation',
  logic_issue: 'Code Fix',
  external_system_issue: 'Investigation',
  // App recovery scenarios
  offline: 'Restart',
  healthcheck_failed: 'Investigation',
  ray_actor_missing: 'Restart',
  deployment_issue: 'Investigation',
};

/** Tokens the editor surfaces as click-to-insert chips and that
 *  ``applyEmailTemplateTokens`` substitutes at send time. */
export const EMAIL_TOKEN_HINTS: { token: string; description: string }[] = [
  { token: '{{date}}', description: "Today's date when the email is sent (YYYY-MM-DD)." },
  { token: '{{job_name}}', description: "The job's name — only meaningful for job scenarios." },
  { token: '{{app_name}}', description: "The application's name — only meaningful for app scenarios." },
  { token: '{{sender_name}}', description: "Your display name at send time." },
];

/** The legacy template hand-padded each "label:" prefix to column 19 so values
 *  line up in a single column. We preserve that minimum (owner-side filters
 *  match ``Application:`` at a fixed offset) and stretch only when a long custom
 *  label would otherwise collide with its value.
 *
 *  Padding uses NON-BREAKING SPACES (U+00A0), not regular spaces, because
 *  Outlook collapses runs of regular whitespace in an auto-converted plain-text
 *  mailto body. NBSPs survive that collapse so the column lines up in both HTML
 *  and Plain Text compose modes; ``trim`` still treats them as whitespace. */
export const EMAIL_BODY_LABEL_COLUMN = 19;
export const NBSP = ' ';

export function buildDefaultEmailBody(assetKind: EmailAssetKind, scenarioType?: string | null): string {
  const base = assetKind === 'app' ? DEFAULT_APP_EMAIL_ROWS : DEFAULT_JOB_EMAIL_ROWS;
  const presetType = scenarioType ? TYPE_OF_REQUEST_BY_SCENARIO_TYPE[scenarioType] : null;
  const rows = base.map((row) =>
    row.label === 'Type of Request' && presetType ? { ...row, value: presetType } : row,
  );
  return serializeEmailBody(rows);
}

export function serializeEmailBody(rows: EmailBodyRow[]): string {
  if (rows.length === 0) {
    return [EMAIL_BODY_INTRO, '', '', ...EMAIL_BODY_OUTRO_LINES].join('\n');
  }
  const labelWidth = rows.reduce(
    (width, row) => Math.max(width, row.label.length + 1 /* colon */ + 2 /* min gap */),
    EMAIL_BODY_LABEL_COLUMN,
  );
  const rowLines = rows.map((row) => `${(row.label + ':').padEnd(labelWidth, NBSP)}${row.value}`);
  return [EMAIL_BODY_INTRO, '', ...rowLines, '', ...EMAIL_BODY_OUTRO_LINES].join('\n');
}

/** Pull the "Label: value" rows out of a body string. Lines that don't match
 *  the row shape are prose (intro / signature) and dropped from the editable
 *  list — the canonical intro/outro are re-emitted on every serialize. Label is
 *  constrained so prose lines with a stray colon don't promote into rows. */
export function parseEmailBodyRows(body: string): EmailBodyRow[] {
  if (!body || !body.trim()) return [];
  const rows: EmailBodyRow[] = [];
  // Whitespace classes accept NBSP ( ) alongside space/tab so a body
  // round-tripped through serializeEmailBody (padded with NBSPs) still parses.
  for (const rawLine of body.split('\n')) {
    const match = rawLine.match(/^[ \t ]*([A-Za-z][A-Za-z0-9 _./()\-]{0,59})[ \t ]*:[ \t ]*(.*)$/);
    if (!match) continue;
    const label = match[1].trim();
    const value = match[2].trim();
    if (!label) continue;
    rows.push({ label, value });
  }
  return rows;
}

/** Substitute the well-known tokens at send time. Unknown tokens are left as-is
 *  so future additions don't break existing saved templates. */
export function applyEmailTemplateTokens(
  text: string,
  context: { jobName?: string | null; appName?: string | null; senderName?: string | null },
): string {
  const today = new Date().toISOString().slice(0, 10);
  return text
    .replace(/\{\{\s*date\s*\}\}/gi, today)
    .replace(/\{\{\s*job_name\s*\}\}/gi, (context.jobName || '').trim())
    .replace(/\{\{\s*app_name\s*\}\}/gi, (context.appName || '').trim())
    .replace(/\{\{\s*sender_name\s*\}\}/gi, (context.senderName || '').trim());
}
