/**
 * GuidePanel — renders a `guide_step` SSE event: a set of clickable options
 * (the pages the user can see) plus a follow-up question, and/or a T2 high-risk
 * deeplink ("go to page" jump). Clicking an option asks the assistant to explain
 * it; the deeplink navigates to the page — there is NO confirm token here, the
 * assistant never executes high-risk actions on the user's behalf.
 */
import { ArrowRight, Compass } from 'lucide-react';
import type { GuideStep } from './sse';

export default function GuidePanel({
  step,
  onPick,
  onNavigateTab,
}: {
  step: GuideStep;
  // Clicking an option sends this text back to the assistant as a follow-up.
  onPick: (text: string) => void;
  // Clicking the deeplink jumps to a top-level tab (if the host wired it).
  onNavigateTab?: (tab: string) => void;
  // @types/react isn't installed (React 19), so TS doesn't strip `key` from
  // explicitly-annotated props — declare it to keep list rendering type-clean.
  key?: number | string;
}) {
  return (
    <div className="aiw-guide-card">
      {step.options.length > 0 && (
        <div className="aiw-guide-options">
          {step.options.map((o) => (
            <button
              key={o.key}
              type="button"
              className="aiw-guide-option"
              onClick={() => onPick(`How do I use the "${o.label}" page?`)}
              title={o.desc || o.label}
            >
              <Compass size={13} />
              <span className="aiw-guide-option-label">{o.label}</span>
              {o.desc && <span className="aiw-guide-option-desc">{o.desc}</span>}
            </button>
          ))}
        </div>
      )}

      {step.prompt && <div className="aiw-guide-prompt">{step.prompt}</div>}

      {step.deeplink && (
        <button
          type="button"
          className="aiw-guide-deeplink"
          disabled={!onNavigateTab}
          onClick={() => onNavigateTab?.(step.deeplink!.tab)}
        >
          {step.deeplink.label}
          <ArrowRight size={13} />
        </button>
      )}
    </div>
  );
}
