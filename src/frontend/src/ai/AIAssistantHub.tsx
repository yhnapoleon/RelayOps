/**
 * AI Assistant tab — the v2 split workspace: chat on the left, the onboarding
 * wizard on the right. The right pane opens on demand (toggle) or when the
 * assistant emits a `form_sync` event. Replaces the older segmented chat/
 * onboarding switch with a side-by-side collaboration layout.
 */

import SplitWorkspace from './SplitWorkspace';
import './ai-assistant-hub.css';

export default function AIAssistantHub({
  onOpenProject,
  onNavigateTab,
}: {
  // (projectId, productId?) — productId focuses a product inside the project
  // view; the chat's quick-jump buttons pass it, the wizard only sends one arg.
  onOpenProject?: (projectId: number, productId?: number | null) => void;
  // Jump to a top-level tab (guide deeplinks for high-risk actions).
  onNavigateTab?: (tab: string) => void;
}) {
  return (
    <div className="ai-assistant-hub">
      <SplitWorkspace onOpenProject={onOpenProject} onNavigateTab={onNavigateTab} />
    </div>
  );
}
