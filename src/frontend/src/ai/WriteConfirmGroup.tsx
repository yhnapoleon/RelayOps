/**
 * WriteConfirmGroup — renders all pending write proposals for one assistant
 * turn. A single change shows one card; a batch turn ("close these 3 issues")
 * shows one card per change plus a one-click "Confirm all".
 *
 * Each proposal carries its own confirm token and is committed independently
 * (server-side, deterministic). The group owns per-card status so "Confirm all"
 * can drive every still-pending card from one place; a token already consumed
 * (e.g. confirmed individually first) is skipped, never double-confirmed.
 */
import { useCallback, useRef, useState } from 'react';
import { Check } from 'lucide-react';
import WriteConfirmCard, { type ConfirmStatus } from './WriteConfirmCard';
import { confirmWriteAction, type WriteProposal } from './sse';

const isPending = (s: ConfirmStatus) => s === 'pending' || s === 'error';

export default function WriteConfirmGroup({ proposals }: { proposals: WriteProposal[] }) {
  const [statuses, setStatuses] = useState<Record<string, ConfirmStatus>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Mirror of `statuses` for synchronous reads inside the confirm-all loop
  // (state updates are async, so the loop can't trust the closed-over value).
  const statusRef = useRef<Record<string, ConfirmStatus>>({});

  const setStatus = useCallback((token: string, s: ConfirmStatus) => {
    statusRef.current[token] = s;
    setStatuses((prev) => ({ ...prev, [token]: s }));
  }, []);

  const statusOf = (token: string): ConfirmStatus => statuses[token] || 'pending';

  const confirmOne = useCallback(
    async (token: string) => {
      const cur = statusRef.current[token] || 'pending';
      if (cur === 'confirming' || cur === 'confirmed') return;
      setStatus(token, 'confirming');
      setErrors((e) => ({ ...e, [token]: '' }));
      try {
        await confirmWriteAction(token);
        setStatus(token, 'confirmed');
      } catch (err: any) {
        setErrors((e) => ({ ...e, [token]: String(err?.message || err) }));
        setStatus(token, 'error');
      }
    },
    [setStatus],
  );

  const cancelOne = useCallback((token: string) => setStatus(token, 'cancelled'), [setStatus]);

  const confirmAll = useCallback(async () => {
    for (const p of proposals) {
      const cur = statusRef.current[p.confirm_token] || 'pending';
      if (isPending(cur)) await confirmOne(p.confirm_token);
    }
  }, [proposals, confirmOne]);

  const pendingCount = proposals.filter((p) => isPending(statusOf(p.confirm_token))).length;
  const confirming = proposals.some((p) => statusOf(p.confirm_token) === 'confirming');

  return (
    <div className="aiw-write-group">
      {proposals.length > 1 && pendingCount > 0 && (
        <div className="aiw-write-group-bar">
          <span className="aiw-write-group-count">
            {pendingCount} pending change{pendingCount > 1 ? 's' : ''}
          </span>
          <button
            type="button"
            className="aiw-write-confirm-all"
            disabled={confirming}
            onClick={confirmAll}
          >
            <Check size={14} /> Confirm all ({pendingCount})
          </button>
        </div>
      )}
      {proposals.map((p) => (
        <WriteConfirmCard
          key={p.confirm_token}
          proposal={p}
          status={statusOf(p.confirm_token)}
          error={errors[p.confirm_token]}
          onConfirm={() => confirmOne(p.confirm_token)}
          onCancel={() => cancelOne(p.confirm_token)}
        />
      ))}
    </div>
  );
}
