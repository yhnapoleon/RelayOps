/**
 * PageAssistantContext — tells the floating page assistant where the user is.
 *
 * `tab` is driven by App's active top-level tab. `subView` is an optional
 * finer-grained state a page can publish (e.g. the Issue Workbench pushes
 * 'page1'/'page2', My Projects could push 'products'/'assets') so the coach
 * bubble and the KB grounding can be sub-state aware. subView auto-resets when
 * the tab changes so a stale value can't leak across pages.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

/** An imperative request to open the panel and (optionally) auto-send a prompt.
 *  The nonce lets the FAB react to repeated requests with the same text. */
export interface PanelRequest {
  prompt: string;
  nonce: number;
}

interface PageAssistantValue {
  tab: string;
  subView: string | null;
  setSubView: (subView: string | null) => void;
  panelRequest: PanelRequest | null;
  /** Open the floating assistant and auto-send `prompt` (e.g. the Workbench
   *  "AI Diagnose" button asks it to diagnose the current issue). */
  requestPanel: (prompt: string) => void;
}

const PageAssistantCtx = createContext<PageAssistantValue | null>(null);

export function PageAssistantProvider({ tab, children }: { tab: string; children: ReactNode }) {
  const [subView, setSubView] = useState<string | null>(null);
  const [panelRequest, setPanelRequest] = useState<PanelRequest | null>(null);

  // A tab switch invalidates any sub-view the previous page published.
  useEffect(() => {
    setSubView(null);
  }, [tab]);

  const requestPanel = (prompt: string) => setPanelRequest({ prompt, nonce: Date.now() });

  const value = useMemo<PageAssistantValue>(
    () => ({ tab, subView, setSubView, panelRequest, requestPanel }),
    [tab, subView, panelRequest],
  );
  return <PageAssistantCtx.Provider value={value}>{children}</PageAssistantCtx.Provider>;
}

export function usePageAssistant(): PageAssistantValue | null {
  return useContext(PageAssistantCtx);
}

/** Publish a sub-view for as long as the calling component is mounted (clears on
 *  unmount). Safe to call when no provider is present (no-op). */
export function usePublishSubView(subView: string | null) {
  const ctx = useContext(PageAssistantCtx);
  const setSubView = ctx?.setSubView;
  useEffect(() => {
    if (!setSubView) return;
    setSubView(subView);
    return () => setSubView(null);
  }, [setSubView, subView]);
}
