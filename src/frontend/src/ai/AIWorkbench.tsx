/**
 * AI Workbench — chat UI for the Ops assistant.
 *
 * Look & interaction patterns ported from Frontier's ChatArea (greeting +
 * sample-question chips, red user bubbles, collapsible tool-step blocks,
 * markdown answers); state/rendering rewritten in React against the frozen
 * SSE contract (docs/AGENT_CHAT_CONTRACT.md). Conversation history sidebar,
 * feedback/share/embed were deliberately cut.
 */

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Compass,
  Copy,
  Download,
  FileUp,
  History,
  Loader2,
  Pencil,
  RotateCcw,
  Send,
  Sparkles,
  Trash2,
  User as UserIcon,
  Wrench,
  X,
} from 'lucide-react';
import assistantAvatar from '../assets/relayops-mark.svg';
import ArtifactCard, { downloadCsv, type ArtifactData, type ArtifactNavigate } from './ArtifactCard';
import WriteConfirmGroup from './WriteConfirmGroup';
import GuidePanel from './GuidePanel';
import { renderMarkdown } from './markdown';
import {
  deleteConversation,
  fetchChatHealth,
  getConversation,
  listConversations,
  streamChat,
  type ConversationSummary,
  type FormContext,
  type GuideStep,
  type WriteProposal,
} from './sse';
import './ai-workbench.css';

interface Step {
  name: string;
  args: Record<string, unknown>;
  preview: string;
  done: boolean;
}

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  steps: Step[];
  artifacts: ArtifactData[];
  proposals?: WriteProposal[];
  guideSteps?: GuideStep[];
  pending?: boolean;
}

const AGENT_NAME = 'Relay Assistant';
const WELCOME =
  'I can look things up across pages: project and product assets, Job/App status and runbooks, ' +
  'Issues and SLA risk, and the current on-call. I can also run statistical analysis ' +
  '(distributions, SLA attainment, health, period-over-period trends) and generate exportable charts. ' +
  'My data scope matches your page permissions. For changes (e.g. marking an issue a false positive) ' +
  'I only propose them — nothing is applied until you confirm.';
const SAMPLE_QUESTIONS = [
  "Who's on duty today?",
  'Which Issues are close to breaching SLA?',
  "Draw a pie chart of this month's Issue types",
  'Which product has the worst health lately? Analyze it',
];
const TOOL_LABELS: Record<string, string> = {
  relayops_projects_overview: 'Query projects overview',
  relayops_product_assets: 'Query product assets',
  relayops_list_issues: 'Query Issues',
  relayops_issue_breakdown: 'Aggregate Issues by dimension',
  relayops_query: 'Structured aggregate query',
  cml_live_projects: 'CML live project lookup',
  cml_live_jobs: 'CML live Job lookup',
  cml_live_job_runs: 'CML live run history',
  cml_live_apps: 'CML live App status',
  mmp_live_projects: 'MMP live project catalog',
  mmp_live_project_status: 'MMP live model status',
  relayops_on_duty_now: 'Query on-call schedule',
  relayops_job_runbook: 'Read Job runbook',
  relayops_app_runbook: 'Read App runbook',
  relayops_domain_glossary: 'Look up domain glossary',
  relayops_issue_detail: 'Read Issue detail',
  relayops_issue_stats: 'Compute Issue metrics',
  relayops_product_health: 'Compute product health',
  relayops_product_health_drilldown: 'Product health drilldown',
  relayops_job_execution_history: 'Query execution history',
  relayops_mmp_overview: 'Query MMP governance status',
  relayops_compare_periods: 'Period-over-period comparison',
  relayops_search_resolutions: 'Search past resolutions/runbooks',
  relayops_render_chart: 'Generate chart',
  relayops_nav_buttons: 'Generate navigation buttons',
};

/** Serialize the visible conversation to a Markdown transcript and download it
 *  — an offline copy on top of the server-side history (the History panel). */
function exportConversation(messages: Msg[]) {
  const lines: string[] = [`# ${AGENT_NAME} conversation`, '', `_Saved ${new Date().toLocaleString()}_`, ''];
  for (const m of messages) {
    if (m.pending && !m.content) continue;
    lines.push(m.role === 'user' ? '## You' : `## ${AGENT_NAME}`);
    if (m.role === 'assistant' && m.steps.length) {
      lines.push(
        '',
        `_Queried ${m.steps.length} data source${m.steps.length === 1 ? '' : 's'}: ` +
          m.steps.map((s) => TOOL_LABELS[s.name] || s.name).join(', ') +
          '_',
      );
    }
    lines.push('', m.content || '_(no text answer)_', '');
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `relayops-chat-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Delegated handler for the CSV button markdown.ts injects on every table. */
function handleMarkdownTableExport(e: MouseEvent<HTMLDivElement>) {
  const btn = (e.target as HTMLElement).closest('.md-table-export');
  if (!btn) return;
  const table = btn.closest('.table-wrapper')?.querySelector('table');
  if (!table) return;
  const rows = Array.from(table.querySelectorAll('tr')).map((tr) =>
    Array.from(tr.querySelectorAll('th,td')).map((cell) => cell.textContent ?? ''),
  );
  if (rows.length === 0) return;
  downloadCsv('table.csv', rows[0], rows.slice(1));
}

function StepsBlock({ steps, busy }: { steps: Step[]; busy: boolean }) {
  const [open, setOpen] = useState(true);
  if (steps.length === 0) return null;
  return (
    <div className="aiw-steps">
      <button type="button" className="aiw-steps-header" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Wrench size={13} className={busy ? 'aiw-pulse' : undefined} />
        <span>
          {busy ? 'Querying…' : `Queried ${steps.length} data source${steps.length === 1 ? '' : 's'}`}
        </span>
      </button>
      {open &&
        steps.map((s, i) => (
          <div className="aiw-step" key={i}>
            <span className="aiw-step-name">{TOOL_LABELS[s.name] || s.name}</span>
            <span className="aiw-step-preview">
              {Object.keys(s.args || {}).length ? JSON.stringify(s.args) : ''}
              {s.done ? '' : ' …'}
            </span>
          </div>
        ))}
    </div>
  );
}

function AssistantActions({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    navigator.clipboard?.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [content]);
  if (!content) return null;
  return (
    <div className="aiw-actions">
      <button type="button" className="aiw-action-btn" onClick={copy} title="Copy">
        {copied ? <Check size={13} /> : <Copy size={13} />}
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export default function AIWorkbench({
  onNavigate,
  onFormSync,
  onNavigateTab,
  getFormContext,
}: {
  onNavigate?: ArtifactNavigate;
  // Fired when the assistant asks to open/refresh the onboarding form (the
  // `form_sync` event). draftId is null for "open a new draft".
  onFormSync?: (draftId: number | null, reason: string) => void;
  // Fired when a guide deeplink (T2) wants to jump to a top-level tab.
  onNavigateTab?: (tab: string) => void;
  // Reads the live onboarding form (when the wizard is open) so each chat turn
  // can be answered grounded in what the user is editing. Null when closed.
  getFormContext?: () => FormContext | null;
}) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  // Sticky Write mode: while on, every turn is sent with mode='write' so the
  // backend mounts the propose-only write tools (read-only chat otherwise).
  const [writeMode, setWriteMode] = useState(false);
  const [health, setHealth] = useState<{ available: boolean; reason: string } | null>(null);
  const [streamError, setStreamError] = useState('');
  // Saved-conversation history (server-side persistence). Null list = not loaded.
  const [showHistory, setShowHistory] = useState(false);
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [historyError, setHistoryError] = useState('');
  const [historyBusy, setHistoryBusy] = useState(false);
  const threadRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    fetchChatHealth().then(setHealth);
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy]);

  const send = useCallback(
    async (text: string, opts?: { forceReadonly?: boolean }) => {
      const message = text.trim();
      if (!message || busy) return;
      setInput('');
      setStreamError('');
      setBusy(true);
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: message, steps: [], artifacts: [] },
        { role: 'assistant', content: '', steps: [], artifacts: [], pending: true },
      ]);

      const patchLast = (fn: (m: Msg) => Msg) =>
        setMessages((prev) => prev.map((m, i) => (i === prev.length - 1 ? fn({ ...m }) : m)));

      const fctx = getFormContext?.() ?? null;
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await streamChat(
          message,
          threadRef.current,
          (ev) => {
            if (ev.event === 'meta') {
              threadRef.current = ev.data.thread_id || threadRef.current;
            } else if (ev.event === 'tool_call') {
              patchLast((m) => ({
                ...m,
                steps: [...m.steps, { name: ev.data.name, args: ev.data.args || {}, preview: '', done: false }],
              }));
            } else if (ev.event === 'tool_result') {
              patchLast((m) => {
                const steps = m.steps.slice();
                const idx = steps.findIndex((s) => !s.done && s.name === ev.data.name);
                if (idx >= 0) steps[idx] = { ...steps[idx], preview: ev.data.preview || '', done: true };
                return { ...m, steps };
              });
            } else if (ev.event === 'artifact') {
              patchLast((m) => ({ ...m, artifacts: [...m.artifacts, ev.data as ArtifactData] }));
            } else if (ev.event === 'write_proposal') {
              // One event per proposed change — a batch turn emits several.
              patchLast((m) => ({
                ...m,
                proposals: [...(m.proposals || []), ev.data as WriteProposal],
              }));
            } else if (ev.event === 'guide_step') {
              patchLast((m) => ({ ...m, guideSteps: [...(m.guideSteps || []), ev.data as GuideStep] }));
            } else if (ev.event === 'form_sync') {
              onFormSync?.(ev.data.draft_id ?? null, ev.data.reason || 'opened');
            } else if (ev.event === 'answer') {
              patchLast((m) => ({ ...m, content: ev.data.text || '', pending: false }));
            } else if (ev.event === 'error') {
              setStreamError(ev.data.message || 'Unknown error');
              patchLast((m) => ({ ...m, pending: false }));
            }
          },
          controller.signal,
          {
            mode: opts?.forceReadonly ? undefined : writeMode ? 'write' : undefined,
            // Attach the live onboarding form so the assistant answers about
            // what the user is editing (split view stays coherent).
            draftRef: fctx?.draftId ?? undefined,
            formSnapshot: fctx
              ? {
                  draftId: fctx.draftId,
                  groupId: fctx.groupId,
                  payload: fctx.payload,
                  clarifications: fctx.clarifications,
                  answers: fctx.answers,
                  dirty: fctx.dirty,
                }
              : undefined,
          },
        );
      } catch (err: any) {
        if (err?.name !== 'AbortError') setStreamError(String(err?.message || err));
        patchLast((m) => ({ ...m, pending: false }));
      } finally {
        setBusy(false);
        abortRef.current = null;
      }
    },
    [busy, onFormSync, writeMode, getFormContext],
  );

  const reset = useCallback(() => {
    abortRef.current?.abort();
    threadRef.current = null;
    setMessages([]);
    setStreamError('');
    setBusy(false);
  }, []);

  const openHistory = useCallback(async () => {
    setShowHistory(true);
    setHistoryError('');
    setConversations(null);
    try {
      setConversations(await listConversations());
    } catch (err: any) {
      setHistoryError(String(err?.message || err));
    }
  }, []);

  // Load a saved conversation into the view and resume its thread, so the user
  // can keep chatting where they left off (artifacts/proposals aren't persisted
  // — the transcript text and tool-step summaries are).
  const loadConversation = useCallback(async (id: number) => {
    setHistoryBusy(true);
    setHistoryError('');
    try {
      const detail = await getConversation(id);
      abortRef.current?.abort();
      threadRef.current = detail.thread_id;
      setMessages(
        detail.messages.map((m) => ({
          role: m.role,
          content: m.content,
          steps: (m.steps || []).map((s) => ({ name: s.name, args: s.args || {}, preview: '', done: true })),
          artifacts: [],
        })),
      );
      setStreamError('');
      setBusy(false);
      setShowHistory(false);
    } catch (err: any) {
      setHistoryError(String(err?.message || err));
    } finally {
      setHistoryBusy(false);
    }
  }, []);

  const removeConversation = useCallback(async (id: number) => {
    if (!window.confirm('Delete this saved conversation? This cannot be undone.')) return;
    try {
      await deleteConversation(id);
      setConversations((prev) => (prev ? prev.filter((c) => c.id !== id) : prev));
    } catch (err: any) {
      setHistoryError(String(err?.message || err));
    }
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  };

  const unavailable = health !== null && !health.available;

  return (
    <div className="ai-workbench">
      <div className="aiw-header">
        <div className="aiw-agent">
          <div className="aiw-avatar">
            <img src={assistantAvatar} alt={AGENT_NAME} />
          </div>
          <div>
            <div className="aiw-agent-name">{AGENT_NAME}</div>
            <div className="aiw-agent-sub">Read-only queries · data scope matches your permissions</div>
          </div>
        </div>
        <div className="aiw-header-actions">
          <button
            type="button"
            className={`aiw-header-btn ${showHistory ? 'active' : ''}`}
            onClick={() => (showHistory ? setShowHistory(false) : openHistory())}
            title="Browse saved conversations"
          >
            <History size={14} />
            History
          </button>
          <button
            type="button"
            className="aiw-header-btn"
            onClick={() => exportConversation(messages)}
            disabled={messages.length === 0}
            title="Export this conversation as a Markdown file"
          >
            <Download size={14} />
            Export
          </button>
          <button type="button" className="aiw-header-btn" onClick={reset} title="New chat">
            <RotateCcw size={14} />
            New chat
          </button>
        </div>
      </div>

      {showHistory && (
        <div className="aiw-history">
          <div className="aiw-history-header">
            <span className="aiw-history-title">
              <History size={15} /> Saved conversations
            </span>
            <button
              type="button"
              className="aiw-history-close"
              onClick={() => setShowHistory(false)}
              title="Close"
            >
              <X size={16} />
            </button>
          </div>
          {historyError && (
            <div className="aiw-error">
              <AlertTriangle size={15} />
              {historyError}
            </div>
          )}
          <div className="aiw-history-list">
            {conversations === null ? (
              <div className="aiw-history-empty">
                <Loader2 size={16} className="aiw-pulse" /> Loading…
              </div>
            ) : conversations.length === 0 ? (
              <div className="aiw-history-empty">No saved conversations yet.</div>
            ) : (
              conversations.map((c) => (
                <div className="aiw-history-row" key={c.id}>
                  <button
                    type="button"
                    className="aiw-history-open"
                    disabled={historyBusy}
                    onClick={() => loadConversation(c.id)}
                  >
                    <span className="aiw-history-row-title">{c.title}</span>
                    <span className="aiw-history-row-sub">
                      {c.message_count} message{c.message_count === 1 ? '' : 's'}
                      {c.updated_at ? ` · ${new Date(c.updated_at).toLocaleString()}` : ''}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="aiw-history-del"
                    title="Delete this conversation"
                    onClick={() => removeConversation(c.id)}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      <div className="aiw-scroll" ref={scrollRef}>
        {messages.length === 0 ? (
          <div className="aiw-centered">
            <div className="aiw-avatar" style={{ width: 56, height: 56 }}>
              <img src={assistantAvatar} alt={AGENT_NAME} />
            </div>
            <div className="aiw-greeting">How can I help?</div>
            <p className="aiw-welcome">{WELCOME}</p>
            {unavailable ? (
              <div className="aiw-error">
                <AlertTriangle size={15} />
                AI assistant is currently unavailable: {health?.reason}
              </div>
            ) : (
              <div className="aiw-chips">
                {SAMPLE_QUESTIONS.map((q) => (
                  <button key={q} type="button" className="aiw-chip" onClick={() => send(q)}>
                    <Sparkles size={13} />
                    {q}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="aiw-messages">
            {messages.map((m, i) => (
              <div className={`aiw-msg ${m.role}`} key={i}>
                <div className="aiw-msg-content">
                  <div className="aiw-msg-avatar">
                    {m.role === 'user' ? <UserIcon size={17} /> : <img src={assistantAvatar} alt={AGENT_NAME} />}
                  </div>
                  <div className="aiw-stack">
                    {m.role === 'assistant' && <StepsBlock steps={m.steps} busy={!!m.pending} />}
                    {/* Charts/tables render above the answer (the text refers to
                        them by title); nav buttons render below it — they are the
                        "where to go next" affordance and must not appear before
                        the answer that explains them. */}
                    {m.role === 'assistant' &&
                      m.artifacts
                        .filter((a) => a.kind !== 'nav')
                        .map((a) => (
                          <ArtifactCard key={a.id} artifact={a} onNavigate={onNavigate} />
                        ))}
                    {m.role === 'assistant' && m.proposals && m.proposals.length > 0 && (
                      <WriteConfirmGroup proposals={m.proposals} />
                    )}
                    {m.role === 'assistant' &&
                      m.guideSteps?.map((g, gi) => (
                        <GuidePanel
                          key={gi}
                          step={g}
                          onPick={(text) => send(text)}
                          onNavigateTab={onNavigateTab}
                        />
                      ))}
                    {m.role === 'user' ? (
                      <div className="aiw-bubble">{m.content}</div>
                    ) : m.content ? (
                      <div className="aiw-bubble" onClick={handleMarkdownTableExport}>
                        <div
                          className="md-content"
                          dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }}
                        />
                      </div>
                    ) : m.pending && m.steps.length === 0 ? (
                      <div className="aiw-bubble aiw-pulse">Thinking…</div>
                    ) : null}
                    {m.role === 'assistant' &&
                      !m.pending &&
                      m.artifacts
                        .filter((a) => a.kind === 'nav')
                        .map((a) => (
                          <ArtifactCard key={a.id} artifact={a} onNavigate={onNavigate} />
                        ))}
                    {m.role === 'assistant' && !m.pending && <AssistantActions content={m.content} />}
                  </div>
                </div>
              </div>
            ))}
            {streamError && (
              <div className="aiw-error">
                <AlertTriangle size={15} />
                {streamError}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="aiw-input-wrap">
        <div className="aiw-modes">
          <button
            type="button"
            className="aiw-mode"
            onClick={() => send('How do I use this platform?', { forceReadonly: true })}
            disabled={unavailable}
            title="Show the pages you can use, then explain any one of them"
          >
            <Compass size={13} />
            Guide
          </button>
          <button
            type="button"
            className={`aiw-mode ${writeMode ? 'active' : ''}`}
            onClick={() => setWriteMode((v) => !v)}
            disabled={unavailable}
            title={writeMode ? 'Write mode on — turn off to return to read-only' : 'Propose changes (resolve/close, mark false positive). Nothing applies until you confirm.'}
          >
            <Pencil size={13} />
            Write mode{writeMode ? ' · on' : ''}
          </button>
          <button
            type="button"
            className="aiw-mode"
            onClick={() => onFormSync?.(null, 'opened')}
            disabled={unavailable}
            title="Open the onboarding wizard to import a handover document"
          >
            <FileUp size={13} />
            Onboarding
          </button>
        </div>
        <div className={`aiw-input-card ${writeMode ? 'aiw-input-write' : ''}`}>
          <textarea
            className="aiw-textarea"
            rows={1}
            placeholder={
              unavailable
                ? 'AI assistant unavailable'
                : writeMode
                  ? 'Write mode · describe the change (e.g. mark #71 false positive)…'
                  : `Ask ${AGENT_NAME}…`
            }
            value={input}
            disabled={unavailable || busy}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <button
            type="button"
            className="aiw-send"
            disabled={unavailable || busy || !input.trim()}
            onClick={() => send(input)}
            title="Send"
          >
            <Send size={16} />
          </button>
        </div>
        <div className="aiw-hint">
          {writeMode
            ? 'Write mode: I draft a change for you to confirm — nothing is applied until you do. Toggle off for read-only.'
            : 'AI answers are based on platform data but may be wrong; rely on the page data for critical actions. Enter to send, Shift+Enter for a new line.'}
        </div>
      </div>
    </div>
  );
}
