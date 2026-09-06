/**
 * SplitWorkspace — left Chat / right Form, the v2 collaboration layout.
 *
 * The chat is always mounted on the left (its thread/messages survive the right
 * pane opening and closing). The right pane hosts the onboarding wizard; it
 * opens when the user clicks the toggle OR when the assistant emits a
 * `form_sync` event (e.g. "let's onboard a new job"). When the right pane is
 * closed the chat fills the width.
 */
import { useCallback, useRef, useState } from 'react';
import { FileUp, PanelRightClose } from 'lucide-react';
import AIWorkbench from './AIWorkbench';
import OnboardingWizard from './OnboardingWizard';
import type { ArtifactNavigate } from './ArtifactCard';
import type { FormContext } from './sse';
import './ai-assistant-hub.css';

export default function SplitWorkspace({
  onOpenProject,
  onNavigateTab,
}: {
  onOpenProject?: (projectId: number, productId?: number | null) => void;
  onNavigateTab?: (tab: string) => void;
}) {
  const [rightOpen, setRightOpen] = useState(false);
  // Bumped on every form_sync so the wizard can re-fetch when chat refills it.
  const [wizardKey, setWizardKey] = useState(0);
  const lastReason = useRef<string>('');
  // Live form state lifted from the wizard; read at chat send-time so a chat
  // turn carries the form the user is currently editing.
  const formCtxRef = useRef<FormContext | null>(null);

  const handleFormSync = useCallback((_draftId: number | null, reason: string) => {
    lastReason.current = reason;
    setRightOpen(true);
    // "refreshed"/"refilled" → force the wizard to reload server-side state.
    if (reason !== 'opened') setWizardKey((k) => k + 1);
  }, []);

  const handleWizardContext = useCallback((ctx: FormContext | null) => {
    formCtxRef.current = ctx;
  }, []);
  // Only attach form context while the panel is actually open.
  const getFormContext = useCallback(() => (rightOpen ? formCtxRef.current : null), [rightOpen]);

  const navigate: ArtifactNavigate | undefined = onOpenProject
    ? (projectId, productId) => onOpenProject(projectId, productId)
    : undefined;

  return (
    <div className={`ai-split ${rightOpen ? 'ai-split-open' : ''}`}>
      <div className="ai-split-left">
        {!rightOpen && (
          <div className="ai-split-bar">
            <button type="button" className="aih-tab" onClick={() => setRightOpen(true)}>
              <FileUp size={14} />
              Onboarding · Document Import
            </button>
          </div>
        )}
        <AIWorkbench
          onNavigate={navigate}
          onFormSync={handleFormSync}
          onNavigateTab={onNavigateTab}
          getFormContext={getFormContext}
        />
      </div>

      {rightOpen && (
        <div className="ai-split-right">
          <div className="ai-split-bar ai-split-right-bar">
            <span className="ai-split-title">
              <FileUp size={14} /> Onboarding
            </span>
            <button
              type="button"
              className="aih-tab"
              title="Close panel"
              onClick={() => setRightOpen(false)}
            >
              <PanelRightClose size={14} />
              Close
            </button>
          </div>
          <div className="ai-split-right-body" key={wizardKey}>
            <OnboardingWizard onOpenProject={onOpenProject} onContext={handleWizardContext} />
          </div>
        </div>
      )}
    </div>
  );
}
