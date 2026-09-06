/**
 * PagePanel — the floating page assistant's chat popover. A lightweight,
 * read-only, guide-focused sibling of AIWorkbench: no write mode, no onboarding,
 * no saved-history sidebar. Every turn is sent with `pageContext` so the backend
 * grounds the answer in the current tab's KB (kb_search scoped to this tab).
 * The thread is ephemeral — it resets when the tab/sub-view changes or the panel
 * closes. Output defaults to English; it mirrors Chinese when the user writes it.
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { AlertTriangle, Compass, Loader2, Send, Sparkles, Wrench, X } from 'lucide-react';
import avatar from '../../assets/relayops-mark.svg';
import GuidePanel from '../GuidePanel';
import { renderMarkdown } from '../markdown';
import { fetchChatHealth, streamChat, type GuideStep } from '../sse';
import type { PanelRequest } from './PageAssistantContext';

interface Step { name: string; done: boolean; }
interface Msg {
  role: 'user' | 'assistant';
  content: string;
  steps: Step[];
  guideSteps?: GuideStep[];
  pending?: boolean;
}

export default function PagePanel({
  tab,
  subView,
  title,
  faqs,
  coach,
  autoPrompt,
  onClose,
  onNavigateTab,
}: {
  tab: string;
  subView: string | null;
  title: string;
  faqs: { q: string }[];
  coach: string[];
  // An imperative prompt to auto-send once (e.g. Workbench "AI Diagnose").
  autoPrompt?: PanelRequest | null;
  onClose: () => void;
  onNavigateTab?: (tab: string) => void;
}) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [streamError, setStreamError] = useState('');
  const [health, setHealth] = useState<{ available: boolean; reason: string } | null>(null);
  const threadRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const lastAutoNonce = useRef<number>(0);

  useEffect(() => {
    fetchChatHealth().then(setHealth);
    return () => abortRef.current?.abort();
  }, []);

  // Ephemeral session: a tab / sub-view change starts a fresh thread.
  useEffect(() => {
    abortRef.current?.abort();
    threadRef.current = null;
    setMessages([]);
    setStreamError('');
    setBusy(false);
  }, [tab, subView]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy]);

  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || busy) return;
      setInput('');
      setStreamError('');
      setBusy(true);
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: message, steps: [] },
        { role: 'assistant', content: '', steps: [], pending: true },
      ]);
      const patchLast = (fn: (m: Msg) => Msg) =>
        setMessages((prev) => prev.map((m, i) => (i === prev.length - 1 ? fn({ ...m }) : m)));

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
              patchLast((m) => ({ ...m, steps: [...m.steps, { name: ev.data.name, done: false }] }));
            } else if (ev.event === 'tool_result') {
              patchLast((m) => {
                const steps = m.steps.slice();
                const idx = steps.findIndex((s) => !s.done && s.name === ev.data.name);
                if (idx >= 0) steps[idx] = { ...steps[idx], done: true };
                return { ...m, steps };
              });
            } else if (ev.event === 'guide_step') {
              patchLast((m) => ({ ...m, guideSteps: [...(m.guideSteps || []), ev.data as GuideStep] }));
            } else if (ev.event === 'answer') {
              patchLast((m) => ({ ...m, content: ev.data.text || '', pending: false }));
            } else if (ev.event === 'error') {
              setStreamError(ev.data.message || 'Unknown error');
              patchLast((m) => ({ ...m, pending: false }));
            }
          },
          controller.signal,
          { pageContext: { tab, subView } },
        );
      } catch (err: any) {
        if (err?.name !== 'AbortError') setStreamError(String(err?.message || err));
        patchLast((m) => ({ ...m, pending: false }));
      } finally {
        setBusy(false);
        abortRef.current = null;
      }
    },
    [busy, tab, subView],
  );

  // Fire an imperative prompt (e.g. "AI Diagnose") once per nonce.
  useEffect(() => {
    if (!autoPrompt || autoPrompt.nonce === lastAutoNonce.current) return;
    lastAutoNonce.current = autoPrompt.nonce;
    send(autoPrompt.prompt);
  }, [autoPrompt, send]);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  };

  const unavailable = health !== null && !health.available;

  return (
    <div className="pa-panel" role="dialog" aria-label="Page assistant">
      <div className="pa-panel-header">
        <div className="pa-panel-agent">
          <img className="pa-panel-avatar" src={avatar} alt="Relay Assistant" />
          <div>
            <div className="pa-panel-name">Relay Assistant</div>
            <div className="pa-panel-sub">Read-only guide · {title}</div>
          </div>
        </div>
        <button type="button" className="pa-panel-close" onClick={onClose} title="Close">
          <X size={16} />
        </button>
      </div>

      <div className="pa-panel-scroll" ref={scrollRef}>
        {messages.length === 0 ? (
          <div className="pa-panel-intro">
            <div className="pa-panel-hint">
              <Compass size={14} />
              <span>{coach[0] || `Ask me anything about the ${title} page.`}</span>
            </div>
            {unavailable ? (
              <div className="pa-error">
                <AlertTriangle size={14} /> Assistant unavailable: {health?.reason}
              </div>
            ) : (
              faqs.length > 0 && (
                <div className="pa-chips">
                  {faqs.map((f) => (
                    <button key={f.q} type="button" className="pa-chip" onClick={() => send(f.q)}>
                      <Sparkles size={12} />
                      {f.q}
                    </button>
                  ))}
                </div>
              )
            )}
          </div>
        ) : (
          <div className="pa-messages">
            {messages.map((m, i) => (
              <div className={`pa-msg ${m.role}`} key={i}>
                {m.role === 'assistant' && m.steps.length > 0 && (
                  <div className="pa-steps">
                    <Wrench size={12} className={m.pending ? 'pa-pulse' : undefined} />
                    {m.pending ? 'Looking things up…' : `Checked ${m.steps.length} source${m.steps.length === 1 ? '' : 's'}`}
                  </div>
                )}
                {m.role === 'assistant' &&
                  m.guideSteps?.map((g, gi) => (
                    <GuidePanel key={gi} step={g} onPick={(t) => send(t)} onNavigateTab={onNavigateTab} />
                  ))}
                {m.role === 'user' ? (
                  <div className="pa-bubble">{m.content}</div>
                ) : m.content ? (
                  <div className="pa-bubble">
                    <div className="md-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }} />
                  </div>
                ) : m.pending && m.steps.length === 0 ? (
                  <div className="pa-bubble pa-pulse">Thinking…</div>
                ) : null}
              </div>
            ))}
            {streamError && (
              <div className="pa-error">
                <AlertTriangle size={14} /> {streamError}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="pa-input-wrap">
        <textarea
          className="pa-textarea"
          rows={1}
          placeholder={unavailable ? 'Assistant unavailable' : `Ask about ${title}…`}
          value={input}
          disabled={unavailable || busy}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button
          type="button"
          className="pa-send"
          disabled={unavailable || busy || !input.trim()}
          onClick={() => send(input)}
          title="Send"
        >
          {busy ? <Loader2 size={15} className="pa-pulse" /> : <Send size={15} />}
        </button>
      </div>
      <div className="pa-foot">Read-only guidance · answers may be imperfect. Enter to send.</div>
    </div>
  );
}
