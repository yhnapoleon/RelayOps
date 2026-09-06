/**
 * WriteConfirmCard — renders ONE pending write proposal (a `write_proposal` SSE
 * event) as a before→after diff with Confirm / Cancel. The agent only ever
 * proposes; the change is committed (deterministically, server-side) when the
 * user clicks Confirm. A `version_flow` proposal warns that it is not effective
 * immediately (it enters the project draft/version flow).
 *
 * This component is **controlled**: status/error live in the parent
 * (WriteConfirmGroup) so a batch turn can drive every card — including the
 * one-click "Confirm all" — from a single place.
 */
import { AlertTriangle, Check, X } from 'lucide-react';
import { type WriteProposal } from './sse';

export type ConfirmStatus = 'pending' | 'confirming' | 'confirmed' | 'cancelled' | 'error';

export default function WriteConfirmCard({
  proposal,
  status,
  error,
  onConfirm,
  onCancel,
}: {
  proposal: WriteProposal;
  status: ConfirmStatus;
  error?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className={`aiw-write-card aiw-write-${status}`}>
      <div className="aiw-write-title">{proposal.title}</div>

      <div className="aiw-write-diff">
        {proposal.changes.map((c, i) => (
          <div className="aiw-write-change" key={i}>
            <span className="aiw-write-field">{c.label}</span>
            <span className="aiw-write-old">{c.old_value}</span>
            <span className="aiw-write-arrow">→</span>
            <span className="aiw-write-new">{c.new_value}</span>
            {c.rationale && <div className="aiw-write-rationale">{c.rationale}</div>}
          </div>
        ))}
      </div>

      {proposal.impact_note && <div className="aiw-write-impact">{proposal.impact_note}</div>}

      {proposal.commit_path === 'version_flow' && (
        <div className="aiw-write-warn">
          <AlertTriangle size={13} />
          This enters the project version flow — it is not effective immediately and may need
          submission/approval.
        </div>
      )}

      {status === 'confirmed' ? (
        <div className="aiw-write-ok">
          <Check size={14} /> Confirmed
        </div>
      ) : status === 'cancelled' ? (
        <div className="aiw-write-cancelled">Cancelled</div>
      ) : (
        <div className="aiw-write-actions">
          <button
            type="button"
            className="aiw-write-confirm"
            disabled={status === 'confirming'}
            onClick={onConfirm}
          >
            <Check size={14} />
            {status === 'confirming' ? 'Confirming…' : 'Confirm'}
          </button>
          <button
            type="button"
            className="aiw-write-cancel"
            disabled={status === 'confirming'}
            onClick={onCancel}
          >
            <X size={14} />
            Cancel
          </button>
        </div>
      )}

      {status === 'error' && (
        <div className="aiw-write-err">
          <AlertTriangle size={13} /> {error}
        </div>
      )}
    </div>
  );
}
