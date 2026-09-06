/**
 * One-click issue diagnosis panel — mounted inside the issue Action
 * Workbench. Drives POST /api/agent/diagnose/{id} (SSE) and
 * renders the v2 report: stage progress, handling state, ranked root causes,
 * runbook-instantiated steps, escalation + email draft, self-check warnings,
 * the evidence checklist, and embedded table artifacts (shared ArtifactCard
 * renderer → same CSV export). Exports: copy-as-Markdown / download JSON.
 *
 * Styling matches the surrounding App.tsx workbench (tailwind utility
 * classes), not the chat workbench CSS.
 */

import { useCallback, useRef, useState, type ReactNode } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  Loader2,
  Mail,
  Sparkles,
} from 'lucide-react';
import ArtifactCard, { type ArtifactData } from './ArtifactCard';
import { fetchChatHealth, streamDiagnose } from './sse';

interface RootCause {
  hypothesis: string;
  confidence: string;
  evidence: string;
}

interface EmailDraft {
  to: string;
  subject: string;
  body: string;
}

export interface DiagnosisReportData {
  summary: string;
  root_causes: RootCause[];
  recommended_steps: string[];
  verification_steps: string[];
  escalation_needed: boolean;
  escalation_target: string;
  escalation_reason: string;
  email_draft: EmailDraft | null;
  // v2 additions
  handling_state?: string;
  evidence_checklist?: string[];
  warnings?: string[];
  artifacts?: ArtifactData[];
}

const STAGE_LABELS: Record<string, string> = {
  collecting: 'Collecting evidence…',
  enriching: 'Targeted evidence gathering…',
  retrieving: 'Searching past cases…',
  analyzing: 'Analyzing…',
  verifying: 'Self-checking…',
};

// Mirrors HandlingState.LABELS (core/agent/knowledge.py).
const HANDLING_LABELS: Record<string, string> = {
  unclaimed: 'Unclaimed',
  claimed_not_started: 'Claimed, not started',
  in_progress: 'In progress',
  escalated_waiting: 'Escalated, waiting on external',
  returned_to_owner: 'Returned to project team',
  resolved: 'Resolved',
  closed_auto: 'Closed (auto)',
  closed_manual: 'Closed (manual)',
  false_positive: 'Closed (false positive)',
};

const CONFIDENCE_STYLE: Record<string, string> = {
  high: 'bg-red-50 text-red-700 border-red-200',
  medium: 'bg-amber-50 text-amber-700 border-amber-200',
  low: 'bg-slate-50 text-slate-600 border-slate-200',
};

function reportToMarkdown(issueId: number, r: DiagnosisReportData): string {
  const lines: string[] = [`# Issue #${issueId} Diagnosis Report`, '', r.summary, ''];
  if (r.handling_state) lines.push(`**Current handling state**: ${HANDLING_LABELS[r.handling_state] || r.handling_state}`, '');
  if (r.root_causes?.length) {
    lines.push('## Likely root causes');
    r.root_causes.forEach((c, i) =>
      lines.push(`${i + 1}. **${c.hypothesis}** (confidence ${c.confidence})`, `   Evidence: ${c.evidence}`));
    lines.push('');
  }
  if (r.recommended_steps?.length) {
    lines.push('## Recommended steps', ...r.recommended_steps.map((s, i) => `${i + 1}. ${s}`), '');
  }
  if (r.verification_steps?.length) {
    lines.push('## Verification steps', ...r.verification_steps.map((s) => `- ${s}`), '');
  }
  if (r.escalation_needed) {
    lines.push('## Escalation', `Target: ${r.escalation_target}`, `Reason: ${r.escalation_reason}`, '');
    if (r.email_draft) {
      lines.push('### Escalation email draft (not sent)',
        `To: ${r.email_draft.to}`, `Subject: ${r.email_draft.subject}`, '', r.email_draft.body, '');
    }
  }
  if (r.warnings?.length) lines.push('## Self-check notes', ...r.warnings.map((w) => `- ${w}`), '');
  if (r.evidence_checklist?.length) lines.push('## Evidence checklist', ...r.evidence_checklist.map((c) => `- ${c}`), '');
  return lines.join('\n');
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="aiw-action-btn"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
      {copied ? 'Copied' : label}
    </button>
  );
}

function Section({ title, children, defaultOpen = true }: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-sm font-semibold text-slate-900"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {title}
      </button>
      {open && <div className="border-t border-slate-100 px-3 py-2.5">{children}</div>}
    </div>
  );
}

export default function DiagnosisPanel({ issueId }: { issueId: number }) {
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'error' | 'unavailable'>('idle');
  const [stage, setStage] = useState('');
  const [report, setReport] = useState<DiagnosisReportData | null>(null);
  const [error, setError] = useState('');
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    setStatus('running');
    setStage('collecting');
    setReport(null);
    setError('');
    const health = await fetchChatHealth();
    if (!health.available) {
      setStatus('unavailable');
      setError(health.reason);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      let got: DiagnosisReportData | null = null;
      let failed = '';
      await streamDiagnose(
        issueId,
        (ev) => {
          if (ev.event === 'stage') setStage(ev.data.stage || '');
          else if (ev.event === 'report') got = ev.data as DiagnosisReportData;
          else if (ev.event === 'error') failed = ev.data.message || 'Diagnosis failed';
        },
        controller.signal,
      );
      if (got) {
        setReport(got);
        setStatus('done');
      } else {
        setError(failed || 'Diagnosis returned no report');
        setStatus('error');
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        setError(String(err?.message || err));
        setStatus('error');
      } else {
        setStatus('idle');
      }
    } finally {
      abortRef.current = null;
    }
  }, [issueId]);

  const downloadJson = useCallback(() => {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `issue-${issueId}-diagnosis.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, [issueId, report]);

  return (
    <div className="rounded-xl border border-indigo-200 bg-indigo-50/40 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles size={16} className="text-indigo-600" />
          <span className="text-sm font-semibold text-slate-900">AI Diagnosis</span>
        </div>
        <div className="flex items-center gap-2">
          {report && (
            <>
              <CopyButton text={reportToMarkdown(issueId, report)} label="Copy Markdown" />
              <button type="button" className="aiw-action-btn" onClick={downloadJson}>
                <Download size={13} /> JSON
              </button>
            </>
          )}
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            onClick={run}
            disabled={status === 'running'}
          >
            {status === 'running' ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                {STAGE_LABELS[stage] || 'Diagnosing…'}
              </>
            ) : (
              <>{report ? 'Re-run diagnosis' : 'One-click diagnosis'}</>
            )}
          </button>
        </div>
      </div>

      {(status === 'error' || status === 'unavailable') && (
        <div className="mt-3 flex items-center gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          <AlertTriangle size={14} />
          {status === 'unavailable' ? `AI assistant unavailable: ${error}` : error}
        </div>
      )}

      {report && (
        <div className="mt-3 space-y-3">
          <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              {report.handling_state && (
                <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700">
                  {HANDLING_LABELS[report.handling_state] || report.handling_state}
                </span>
              )}
              <span className="text-sm text-slate-800">{report.summary}</span>
            </div>
          </div>

          {(report.warnings?.length ?? 0) > 0 && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {report.warnings!.map((w, i) => (
                <div key={i} className="flex items-start gap-1.5">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                  {w}
                </div>
              ))}
            </div>
          )}

          {report.root_causes.length > 0 && (
            <Section title="Likely root causes">
              <ol className="space-y-2">
                {report.root_causes.map((c, i) => (
                  <li key={i} className="text-sm text-slate-800">
                    <span
                      className={`mr-2 inline-block rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase ${CONFIDENCE_STYLE[c.confidence] || CONFIDENCE_STYLE.low}`}
                    >
                      {c.confidence || '?'}
                    </span>
                    {c.hypothesis}
                    {c.evidence && <div className="mt-0.5 text-xs text-slate-500">Evidence: {c.evidence}</div>}
                  </li>
                ))}
              </ol>
            </Section>
          )}

          {report.recommended_steps.length > 0 && (
            <Section title="Recommended steps (instantiated from runbook)">
              <ol className="list-decimal space-y-1 pl-5 text-sm text-slate-800">
                {report.recommended_steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
            </Section>
          )}

          {report.verification_steps.length > 0 && (
            <Section title="Verification steps">
              <ul className="list-disc space-y-1 pl-5 text-sm text-slate-800">
                {report.verification_steps.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </Section>
          )}

          {report.escalation_needed && (
            <Section title="Escalation suggestion">
              <div className="space-y-1 text-sm text-slate-800">
                <div><span className="font-semibold">Target: </span>{report.escalation_target || '—'}</div>
                {report.escalation_reason && (
                  <div><span className="font-semibold">Reason: </span>{report.escalation_reason}</div>
                )}
              </div>
              {report.email_draft && (
                <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-2.5">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
                      <Mail size={13} />
                      Escalation email draft (draft only — the system will not send it)
                    </div>
                    <CopyButton
                      text={`To: ${report.email_draft.to}\nSubject: ${report.email_draft.subject}\n\n${report.email_draft.body}`}
                      label="Copy email"
                    />
                  </div>
                  <div className="mt-1.5 text-xs text-slate-600">
                    <div>To: {report.email_draft.to}</div>
                    <div>Subject: {report.email_draft.subject}</div>
                  </div>
                  <pre className="mt-1.5 whitespace-pre-wrap break-words text-xs text-slate-700">{report.email_draft.body}</pre>
                </div>
              )}
            </Section>
          )}

          {(report.artifacts?.length ?? 0) > 0 && (
            <Section title="Evidence data (exportable)">
              <div className="space-y-3">
                {report.artifacts!.map((a) => <ArtifactCard key={a.id} artifact={a} />)}
              </div>
            </Section>
          )}

          {(report.evidence_checklist?.length ?? 0) > 0 && (
            <Section title="What this diagnosis checked (investigation trail)" defaultOpen={false}>
              <ul className="list-disc space-y-1 pl-5 text-xs text-slate-600">
                {report.evidence_checklist!.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            </Section>
          )}

          <div className="text-[11px] text-slate-400">
            AI-generated content may be wrong; verify against the runbook and actual logs before acting.
          </div>
        </div>
      )}
    </div>
  );
}
