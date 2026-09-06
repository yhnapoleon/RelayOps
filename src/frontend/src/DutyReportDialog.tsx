import React from 'react';
import { Sunrise, AlertTriangle, ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import * as api from './api';

// ── Morning prompt: "Today's report is ready — view it?" ────────────────

export function DutyReportPrompt({
  report,
  open,
  onView,
  onDismiss,
}: {
  report: api.DutyReportData | null;
  open: boolean;
  onView: () => void;
  onDismiss: () => void;
}) {
  if (!report) return null;
  const stats = report.sections?.stats || {};
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onDismiss(); }}>
      <DialogContent className="max-w-[92vw] sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sunrise className="w-5 h-5 text-amber-500" />
            Duty Morning Report {report.report_date}
          </DialogTitle>
          <DialogDescription>
            {report.llm_summary?.headline ||
              `Open ${stats.open_total ?? 0} · SLA risk ${
                (stats.sla_overdue_count ?? 0) + (stats.sla_at_risk_count ?? 0)
              } · MMP pending ${stats.mmp_pending_count ?? 0}`}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="ghost" onClick={onDismiss}>Don't show again today</Button>
          <Button className="bg-relayops-brand hover:bg-relayops-brand/90" onClick={onView}>
            View report
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Full report view ─────────────────────────────────────────────────

function StatChip({ label, value, tone }: { label: string; value: number; tone?: 'red' | 'amber' }) {
  const toneCls =
    tone === 'red' && value > 0
      ? 'bg-red-50 text-red-700 border-red-200'
      : tone === 'amber' && value > 0
        ? 'bg-amber-50 text-amber-700 border-amber-200'
        : 'bg-slate-50 text-slate-600 border-slate-200';
  return (
    <div className={`rounded-lg border px-3 py-1.5 text-sm ${toneCls}`}>
      <span className="font-semibold mr-1">{value}</span>
      <span className="text-xs">{label}</span>
    </div>
  );
}

function IssueSection({
  title,
  section,
  onOpenWorkbench,
  emptyText = '(none)',
}: {
  title: string;
  section?: api.DutyReportSection;
  onOpenWorkbench?: (issueId: number) => void;
  emptyText?: string;
}) {
  const items = section?.items || [];
  const total = section?.total ?? items.length;
  const more = total - items.length;
  return (
    <section>
      <h3 className="text-sm font-semibold text-slate-700 mb-2">
        {title} <span className="text-slate-400 font-normal">({total})</span>
      </h3>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">{emptyText}</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-16">ID</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Assignee</TableHead>
              <TableHead>SLA</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow
                key={item.id}
                className={onOpenWorkbench ? 'cursor-pointer hover:bg-slate-50' : undefined}
                onClick={() => onOpenWorkbench?.(item.id)}
              >
                <TableCell className="font-medium">#{item.id}</TableCell>
                <TableCell>
                  <Badge variant="outline" className="text-xs">{item.type}</Badge>
                </TableCell>
                <TableCell className="max-w-[260px] truncate" title={item.title}>
                  {item.title}
                  {item.external_url && (
                    <a
                      href={item.external_url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      className="inline-flex items-center ml-1 text-blue-600 hover:text-blue-800 align-middle"
                    >
                      <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
                </TableCell>
                <TableCell className="text-slate-500">{item.assignee_name || '—'}</TableCell>
                <TableCell className="text-xs text-slate-500">
                  {item.minutes_to_deadline != null ? (
                    item.minutes_to_deadline < 0 ? (
                      <span className="text-red-600 font-medium">overdue {-item.minutes_to_deadline} min</span>
                    ) : (
                      <span>{item.minutes_to_deadline} min left</span>
                    )
                  ) : (
                    item.sla_deadline || '—'
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {more > 0 && (
        <p className="text-xs text-slate-400 mt-1">…and {more} more, see the corresponding list page</p>
      )}
    </section>
  );
}

export function DutyReportView({
  report,
  open,
  onClose,
  onOpenWorkbench,
}: {
  report: api.DutyReportData | null;
  open: boolean;
  onClose: () => void;
  onOpenWorkbench?: (issueId: number) => void;
}) {
  if (!report) return null;
  const sections = report.sections;
  const stats = sections?.stats || {};
  const summary = report.llm_summary;
  const dutyLine =
    (sections?.on_duty || [])
      .map((d) => `${d.display_name || d.user_id} (${d.duty_role})`)
      .join(', ') || 'Unscheduled';
  const notable = sections?.duty_activity?.notable_events || [];

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      {/* Base DialogContent defaults to ``sm:max-w-sm`` (384px); a plain
          ``max-w-3xl`` (no ``sm:`` prefix) gets overridden by it on ≥640px
          screens, collapsing this 5-column report to an unreadable strip.
          Mirror the wide-dialog pattern used elsewhere: override the ``sm:``
          cap explicitly and widen further on large screens. */}
      <DialogContent className="w-full max-w-[92vw] sm:max-w-4xl lg:max-w-5xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sunrise className="w-5 h-5 text-amber-500" />
            Duty Morning Report {report.report_date}
          </DialogTitle>
          <DialogDescription>On duty today: {dutyLine}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap gap-2">
          <StatChip label="Open issues" value={stats.open_total ?? 0} />
          <StatChip label="SLA overdue" value={stats.sla_overdue_count ?? 0} tone="red" />
          <StatChip label="SLA at-risk" value={stats.sla_at_risk_count ?? 0} tone="amber" />
          <StatChip label="MMP pending" value={stats.mmp_pending_count ?? 0} tone="amber" />
          <StatChip label="New 24h" value={stats.created_24h ?? 0} />
          <StatChip label="Recovered 24h" value={stats.recovered_24h ?? 0} />
        </div>

        {summary?.headline && (
          <div className="rounded-lg border-l-4 border-blue-500 bg-blue-50/60 px-4 py-3 space-y-2">
            <p className="text-sm font-medium text-slate-800">{summary.headline}</p>
            {summary.risk_highlights.length > 0 && (
              <ul className="space-y-1">
                {summary.risk_highlights.map((line, i) => (
                  <li key={i} className="text-sm text-amber-700 flex items-start gap-1.5">
                    <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                    {line}
                  </li>
                ))}
              </ul>
            )}
            {summary.handover_notes.length > 0 && (
              <ul className="list-disc pl-5 space-y-0.5">
                {summary.handover_notes.map((line, i) => (
                  <li key={i} className="text-sm text-slate-600">{line}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        <div className="space-y-5">
          <IssueSection title="SLA Overdue" section={sections?.sla?.overdue} onOpenWorkbench={onOpenWorkbench} />
          <IssueSection title="SLA At-Risk" section={sections?.sla?.at_risk} onOpenWorkbench={onOpenWorkbench} />
          <IssueSection title="MMP Pending Approval" section={sections?.mmp_pending} onOpenWorkbench={onOpenWorkbench} />
          <IssueSection title="Open Issues" section={sections?.open_issues} onOpenWorkbench={onOpenWorkbench} />
          <IssueSection title="New Anomalies (last 24h)" section={sections?.last_24h?.created} onOpenWorkbench={onOpenWorkbench} />
          <IssueSection title="Recovered (last 24h)" section={sections?.last_24h?.recovered} onOpenWorkbench={onOpenWorkbench} />

          <section>
            <h3 className="text-sm font-semibold text-slate-700 mb-2">
              Duty Activity{' '}
              <span className="text-slate-400 font-normal">
                ({sections?.duty_activity?.total_events ?? 0} audit events)
              </span>
            </h3>
            {notable.length === 0 ? (
              <p className="text-sm text-slate-400">(none)</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Time</TableHead>
                    <TableHead>User</TableHead>
                    <TableHead>Action</TableHead>
                    <TableHead>Entity</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {notable.map((ev, i) => (
                    <TableRow key={i}>
                      <TableCell className="text-xs text-slate-500">{ev.timestamp}</TableCell>
                      <TableCell>{ev.username || ev.user_id}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">{ev.action}</Badge>
                      </TableCell>
                      <TableCell className="text-slate-500">
                        {ev.entity_type}#{ev.entity_id}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </section>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
