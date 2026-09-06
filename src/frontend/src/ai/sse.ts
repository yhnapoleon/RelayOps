/**
 * SSE stream client for the AI assistant — implements the frozen contract in
 * docs/AGENT_CHAT_CONTRACT.md (event/data line pairs over a fetch body
 * reader; this is NOT EventSource because we need POST + Authorization).
 */

import { getToken } from '../api';

export interface ChatEvent {
  event: string;
  data: any;
}

/** Live state of the open onboarding form, attached to a chat turn so the
 * assistant can answer grounded in what the user is editing. */
export interface FormContext {
  draftId: number | null;
  /** Split-group id when this draft is one of several siblings from one
   *  cross-project document; lets the chat see every sibling's form. */
  groupId?: string;
  /** Set when the draft adds assets to an existing project rather than
   *  creating one — the chat must not describe it as a new registration. */
  targetProjectId?: number | null;
  /** The per-node diff behind the New / Updated / Unchanged badges. */
  diff?: unknown;
  payload: unknown;
  clarifications: unknown[];
  answers: Record<string, string>;
  dirty: boolean;
}

/** A pending write proposal (the `write_proposal` SSE event payload). */
export interface FieldChange {
  field_path: string;
  label: string;
  old_value: string;
  new_value: string;
  rationale: string;
}
export interface WriteProposal {
  kind: string;
  entity_type: string;
  entity_id: number;
  title: string;
  changes: FieldChange[];
  impact_note: string;
  requires_resolution: boolean;
  confirm_token: string;
  commit_path: 'direct' | 'version_flow';
}

/** A guided step (the `guide_step` SSE event): clickable options + a follow-up
 * question, and/or a T2 high-risk deeplink (a "go to page" jump — never a
 * confirm token, the user performs the action themselves). */
export interface GuideOption {
  key: string;
  label: string;
  desc?: string;
}
export interface GuideStep {
  options: GuideOption[];
  prompt: string;
  deeplink?: { tab: string; label: string };
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

/** A saved conversation summary (GET /conversations). */
export interface ConversationSummary {
  id: number;
  thread_id: string;
  title: string;
  message_count: number;
  created_at: string | null;
  updated_at: string | null;
}

/** One persisted message inside a saved conversation. */
export interface StoredMessage {
  role: 'user' | 'assistant';
  content: string;
  steps: { name: string; args: Record<string, unknown> }[];
  created_at: string | null;
}

export interface ConversationDetail extends ConversationSummary {
  messages: StoredMessage[];
}

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: authHeaders(), ...init });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const payload = await res.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

/** The caller's saved conversations, most recent first. */
export function listConversations(): Promise<ConversationSummary[]> {
  return jsonFetch<ConversationSummary[]>('/api/agent/chat/conversations');
}

/** Full transcript of one saved conversation. */
export function getConversation(id: number): Promise<ConversationDetail> {
  return jsonFetch<ConversationDetail>(`/api/agent/chat/conversations/${id}`);
}

export function deleteConversation(id: number): Promise<void> {
  return jsonFetch<void>(`/api/agent/chat/conversations/${id}`, { method: 'DELETE' });
}

export async function fetchChatHealth(): Promise<{ available: boolean; reason: string }> {
  try {
    const res = await fetch('/api/agent/chat/health', { headers: authHeaders() });
    if (!res.ok) return { available: false, reason: `HTTP ${res.status}` };
    return await res.json();
  } catch (err: any) {
    return { available: false, reason: String(err?.message || err) };
  }
}

/** POST an SSE endpoint and dispatch each contract event to `onEvent`. */
async function streamSse(
  path: string,
  body: unknown,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(path, {
    method: 'POST',
    headers: authHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      const payload = await res.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    // SSE frames are separated by a blank line.
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = 'message';
      let data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (!data) continue;
      try {
        onEvent({ event, data: JSON.parse(data) });
      } catch {
        /* skip malformed frame */
      }
    }
  }
}

export interface ChatOpts {
  mode?: string;
  draftRef?: number | null;
  formSnapshot?:
    | {
        draftId?: number | null;
        groupId?: string;
        payload?: unknown;
        clarifications?: unknown[];
        answers?: Record<string, string>;
        dirty?: boolean;
      }
    | null;
  // Set by the page assistant (floating ball): the current tab + optional
  // sub-view. Forces the turn read-only and grounds it in the page's KB.
  pageContext?: { tab: string; subView?: string | null } | null;
}

/** Page-help payload (GET /page-help/{tab}) — coach bubbles + preset FAQs for a
 *  tab, role-scoped. Static content; no LLM, so it loads even when chat is down. */
export interface PageHelp {
  tab: string;
  title: string;
  coach: string[];
  faqs: { q: string }[];
}

export function fetchPageHelp(tab: string, subView?: string | null): Promise<PageHelp> {
  const qs = subView ? `?sub_view=${encodeURIComponent(subView)}` : '';
  return jsonFetch<PageHelp>(`/api/agent/chat/page-help/${encodeURIComponent(tab)}${qs}`);
}

export function streamChat(
  message: string,
  threadId: string | null,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
  opts?: ChatOpts,
): Promise<void> {
  return streamSse(
    '/api/agent/chat',
    {
      message,
      thread_id: threadId || undefined,
      mode: opts?.mode,
      draft_ref: opts?.draftRef ?? undefined,
      form_snapshot: opts?.formSnapshot ?? undefined,
      page_context: opts?.pageContext
        ? { tab: opts.pageContext.tab, sub_view: opts.pageContext.subView ?? undefined }
        : undefined,
    },
    onEvent,
    signal,
  );
}

export function streamDiagnose(
  issueId: number,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse(`/api/agent/diagnose/${issueId}`, undefined, onEvent, signal);
}

/** Confirm a pending write proposal (deterministic commit, server-side). */
export async function confirmWriteAction(token: string): Promise<any> {
  const res = await fetch(`/api/agent/action/${token}/confirm`, {
    method: 'POST',
    headers: authHeaders(),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const payload = await res.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch {
      /* non-JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}
