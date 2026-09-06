/**
 * AssistantFab — the Relay Assistant floating ball.
 *
 * - Persistent across tabs (mounted once by App), hidden on the full-screen AI
 *   Assistant tab. Draggable (position persisted in localStorage).
 * - Coach bubble: shows on every page switch (tab / sub-view). Dismissing it
 *   hides it for that page for the rest of this login session; while shown it
 *   rotates through the page's tips every 60s.
 * - Mascot state machine (the assistant icon):
 *     resolved (10s) > alert (until My Actions) > working (on Workbench) >
 *     idle-cycle (no activity 2min → random normal/sleeping/playing, 1min each) >
 *     normal.
 *   A new assigned issue also shakes the ball three times.
 * - Opens the read-only PagePanel on tap; other pages can open it with an
 *   auto-prompt via `requestPanel` (Workbench "AI Diagnose").
 */
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { X } from 'lucide-react';
import imgNormal from '../../assets/relayops-mark.svg';
import imgSleeping from '../../assets/relay-assistant-sleeping.svg';
import imgPlaying from '../../assets/relay-assistant-playing.svg';
import imgWorking from '../../assets/relay-assistant-working.svg';
import imgAlert from '../../assets/relay-assistant-alert.svg';
import imgResolved from '../../assets/relay-assistant-resolved.svg';
import { fetchPageHelp, type PageHelp } from '../sse';
import { usePageAssistant, type PanelRequest } from './PageAssistantContext';
import PagePanel from './PagePanel';
import './page-assistant.css';

type AssistantState = 'normal' | 'sleeping' | 'playing' | 'working' | 'alert' | 'resolved';
const STATE_IMG: Record<AssistantState, string> = {
  normal: imgNormal,
  sleeping: imgSleeping,
  playing: imgPlaying,
  working: imgWorking,
  alert: imgAlert,
  resolved: imgResolved,
};
const IDLE_STATES: AssistantState[] = ['normal', 'sleeping', 'playing'];
const IDLE_AFTER_MS = 120_000; // 2 min of no activity → idle cycle
const IDLE_CYCLE_MS = 60_000; // each idle state lasts 1 min
const RESOLVED_MS = 10_000; // resolved celebration duration
const POS_KEY = 'pageAssistantPos';
const FAB = 60;

// Coach bubbles dismissed this login session (in-memory → resets on reload).
const sessionDismissed = new Set<string>();

function loadPos(): { right: number; bottom: number } {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (typeof p?.right === 'number' && typeof p?.bottom === 'number') return p;
    }
  } catch {
    /* ignore */
  }
  return { right: 24, bottom: 24 };
}

const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v));

export default function AssistantFab({
  onNavigateTab,
  assignedOpenCount = 0,
  resolvedCount = 0,
}: {
  onNavigateTab?: (tab: string) => void;
  // Signals from App (issues assigned to the current user): a rise in
  // assignedOpenCount → a new assignment (alert); a rise in resolvedCount → an
  // issue was resolved (celebrate). Undefined-safe defaults keep it inert.
  assignedOpenCount?: number;
  resolvedCount?: number;
}) {
  const ctx = usePageAssistant();
  const tab = ctx?.tab ?? '';
  const subView = ctx?.subView ?? null;
  const panelRequest = ctx?.panelRequest ?? null;

  const [open, setOpen] = useState(false);
  const [help, setHelp] = useState<PageHelp | null>(null);
  const [autoPrompt, setAutoPrompt] = useState<PanelRequest | null>(null);
  const [pos, setPos] = useState(loadPos);
  const drag = useRef<{ x: number; y: number; right: number; bottom: number; moved: boolean } | null>(null);

  // Coach bubble state.
  const [bubbleVisible, setBubbleVisible] = useState(false);
  const [bubbleIdx, setBubbleIdx] = useState(0);

  // Mascot state.
  const [assistantState, setOcto] = useState<AssistantState>('normal');
  const [alertActive, setAlertActive] = useState(false);
  const [shaking, setShaking] = useState(false);

  const hidden = !tab || tab === 'ai-assistant';
  const pageKey = `${tab}:${subView ?? 'default'}`;

  const tips = useMemo(() => {
    if (!help) return [] as string[];
    return [...help.coach, ...help.faqs.map((f) => `You can ask: "${f.q}"`)];
  }, [help]);

  // ── page help (coach + faqs) fetched on every page change ──────────────
  useEffect(() => {
    if (hidden) {
      setHelp(null);
      return;
    }
    let cancelled = false;
    fetchPageHelp(tab, subView)
      .then((h) => !cancelled && setHelp(h))
      .catch(() => !cancelled && setHelp(null));
    return () => {
      cancelled = true;
    };
  }, [tab, subView, hidden]);

  // Show the bubble on every page switch unless dismissed this session.
  useEffect(() => {
    if (hidden || !help || tips.length === 0 || sessionDismissed.has(pageKey)) {
      setBubbleVisible(false);
      return;
    }
    setBubbleIdx(0);
    setBubbleVisible(true);
  }, [help, pageKey, hidden, tips.length]);

  // Rotate the visible bubble through the page's tips every 60s.
  useEffect(() => {
    if (!bubbleVisible || tips.length <= 1) return;
    const t = setInterval(() => setBubbleIdx((i) => (i + 1) % tips.length), 60_000);
    return () => clearInterval(t);
  }, [bubbleVisible, tips.length]);

  // An imperative requestPanel(prompt) opens the panel and auto-sends it.
  useEffect(() => {
    if (!panelRequest) return;
    setBubbleVisible(false);
    setOpen(true);
    setAutoPrompt(panelRequest);
  }, [panelRequest?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── mascot: activity tracking + event detection ────────────────────────
  const lastActivity = useRef(Date.now());
  const tabRef = useRef(tab);
  tabRef.current = tab;
  const alertRef = useRef(false);
  alertRef.current = alertActive;
  const resolvedUntil = useRef(0);
  const idleImg = useRef<AssistantState | null>(null);
  const idleCycleAt = useRef(0);
  const prevAssigned = useRef<number | null>(null);
  const prevResolved = useRef<number | null>(null);

  useEffect(() => {
    const bump = () => {
      lastActivity.current = Date.now();
    };
    const events = ['pointermove', 'pointerdown', 'keydown', 'wheel', 'touchstart'];
    events.forEach((e) => window.addEventListener(e, bump, { passive: true }));
    return () => events.forEach((e) => window.removeEventListener(e, bump));
  }, []);

  // New assigned issue → alert + shake three times.
  useEffect(() => {
    if (prevAssigned.current !== null && assignedOpenCount > prevAssigned.current) {
      setAlertActive(true);
      setShaking(true);
      const t = setTimeout(() => setShaking(false), 1400);
      prevAssigned.current = assignedOpenCount;
      return () => clearTimeout(t);
    }
    prevAssigned.current = assignedOpenCount;
  }, [assignedOpenCount]);

  // Visiting My Actions clears the alert.
  useEffect(() => {
    if (tab === 'actions') setAlertActive(false);
  }, [tab]);

  // An issue was resolved → 10s celebration.
  useEffect(() => {
    if (prevResolved.current !== null && resolvedCount > prevResolved.current) {
      resolvedUntil.current = Date.now() + RESOLVED_MS;
    }
    prevResolved.current = resolvedCount;
  }, [resolvedCount]);

  // Ticker: recompute the mascot state once a second from the refs above.
  useEffect(() => {
    const pick = () => {
      const now = Date.now();
      if (resolvedUntil.current > now) return 'resolved' as AssistantState;
      if (alertRef.current) return 'alert' as AssistantState;
      if (tabRef.current === 'workbench') return 'working' as AssistantState;
      if (now - lastActivity.current >= IDLE_AFTER_MS) {
        if (idleImg.current === null || now - idleCycleAt.current >= IDLE_CYCLE_MS) {
          idleImg.current = IDLE_STATES[Math.floor(Math.random() * IDLE_STATES.length)];
          idleCycleAt.current = now;
        }
        return idleImg.current;
      }
      idleImg.current = null;
      return 'normal' as AssistantState;
    };
    const t = setInterval(() => setOcto(pick()), 1000);
    setOcto(pick());
    return () => clearInterval(t);
  }, []);

  if (hidden) return null;

  // ── drag ───────────────────────────────────────────────────────────────
  const onPointerDown = (e: ReactPointerEvent<HTMLButtonElement>) => {
    drag.current = { x: e.clientX, y: e.clientY, right: pos.right, bottom: pos.bottom, moved: false };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) d.moved = true;
    setPos({
      right: clamp(d.right - dx, 8, window.innerWidth - FAB - 8),
      bottom: clamp(d.bottom - dy, 8, window.innerHeight - FAB - 8),
    });
  };
  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (d.moved) {
      try {
        localStorage.setItem(POS_KEY, JSON.stringify(pos));
      } catch {
        /* ignore */
      }
    } else {
      setBubbleVisible(false);
      setOpen((v) => !v);
    }
  };

  const dismissBubble = () => {
    sessionDismissed.add(pageKey);
    setBubbleVisible(false);
  };

  const bubbleText = tips.length ? tips[bubbleIdx % tips.length] : '';

  return (
    <div className="pa-root" style={{ right: pos.right, bottom: pos.bottom }}>
      {open && help && (
        <PagePanel
          tab={tab}
          subView={subView}
          title={help.title}
          faqs={help.faqs}
          coach={help.coach}
          autoPrompt={autoPrompt}
          onClose={() => setOpen(false)}
          onNavigateTab={onNavigateTab}
        />
      )}

      {bubbleVisible && !open && bubbleText && (
        <div className="pa-coach">
          <button type="button" className="pa-coach-close" onClick={dismissBubble} title="Dismiss">
            <X size={13} />
          </button>
          <div className="pa-coach-text">{bubbleText}</div>
        </div>
      )}

      <button
        type="button"
        className={`pa-fab ${open ? 'pa-fab-open' : ''} ${shaking ? 'pa-fab-shake' : ''}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        title={open ? 'Close Relay Assistant (drag to move)' : 'Ask Relay Assistant (drag to move)'}
      >
        <img src={STATE_IMG[assistantState]} alt="Relay Assistant" draggable={false} />
        {!open && alertActive ? <span className="pa-fab-dot alert" /> : null}
      </button>
    </div>
  );
}
