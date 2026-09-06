/**
 * Artifact renderer — charts (recharts) and tables streamed by the agent via
 * the `artifact` SSE event (docs/AGENT_CHAT_CONTRACT.md §1c). Rows are
 * assembled server-side from RBAC-scoped data; this component only renders
 * and exports (table → CSV, chart → PNG).
 */

import { useCallback, useRef } from 'react';
import { Download, Image as ImageIcon } from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

export interface ArtifactSeries {
  key: string;
  label: string;
}

export interface ArtifactChart {
  type: 'line' | 'bar' | 'pie';
  x_key: string;
  series: ArtifactSeries[];
  y_label?: string;
}

export interface NavButtonData {
  target: 'project' | 'product';
  id: number;
  label: string;
  project_id?: number | null;
}

export interface ArtifactData {
  id: string;
  kind: 'chart' | 'table' | 'nav';
  title: string;
  chart: ArtifactChart | null;
  columns: ArtifactSeries[];
  rows: Record<string, unknown>[];
  buttons?: NavButtonData[];
  source?: Record<string, unknown>;
}

/** (projectId, productId|null) — matches App's openProjectContext. */
export type ArtifactNavigate = (projectId: number, productId: number | null) => void;

const PALETTE = ['#d33a2f', '#2f6fd3', '#2fa05a', '#d3922f', '#8a4fd3', '#2fb5c9', '#c92f7c', '#6b7280'];

function csvEscape(value: unknown): string {
  const s = value === null || value === undefined ? '' : String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function downloadCsv(filename: string, header: string[], rows: unknown[][]) {
  const lines = [header.map(csvEscape).join(',')];
  for (const row of rows) lines.push(row.map(csvEscape).join(','));
  // BOM so Excel opens UTF-8 (Chinese headers) correctly.
  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  triggerDownload(blob, filename);
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function safeFilename(title: string, ext: string): string {
  const base = title.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 60) || 'artifact';
  return `${base}.${ext}`;
}

/** Serialize the recharts SVG inside `container` into a white-background PNG. */
function exportPng(container: HTMLElement | null, title: string) {
  const svg = container?.querySelector('svg');
  if (!svg) return;
  const rect = svg.getBoundingClientRect();
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute('width', String(rect.width));
  clone.setAttribute('height', String(rect.height));
  const xml = new XMLSerializer().serializeToString(clone);
  const img = new Image();
  img.onload = () => {
    const scale = 2; // retina-friendly export
    const canvas = document.createElement('canvas');
    canvas.width = rect.width * scale;
    canvas.height = rect.height * scale;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0);
    canvas.toBlob((blob) => {
      if (blob) triggerDownload(blob, safeFilename(title, 'png'));
    }, 'image/png');
  };
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
}

function ChartBody({ artifact }: { artifact: ArtifactData }) {
  const chart = artifact.chart!;
  const rows = artifact.rows as Record<string, any>[];
  if (chart.type === 'pie') {
    const valueKey = chart.series[0].key;
    return (
      <ResponsiveContainer width="100%" height={260}>
        <PieChart>
          <Pie
            data={rows}
            dataKey={valueKey}
            nameKey={chart.x_key}
            cx="50%"
            cy="50%"
            outerRadius={92}
            label={(entry: any) => `${entry[chart.x_key]} (${entry[valueKey]})`}
          >
            {rows.map((_, i) => (
              <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
            ))}
          </Pie>
          <Tooltip />
        </PieChart>
      </ResponsiveContainer>
    );
  }
  const Wrapper = chart.type === 'line' ? LineChart : BarChart;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <Wrapper data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey={chart.x_key} tick={{ fontSize: 11 }} />
        <YAxis tick={{ fontSize: 11 }} label={chart.y_label ? { value: chart.y_label, angle: -90, position: 'insideLeft', fontSize: 11 } : undefined} />
        <Tooltip />
        {chart.series.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
        {chart.series.map((s, i) =>
          chart.type === 'line' ? (
            <Line key={s.key} type="monotone" dataKey={s.key} name={s.label || s.key}
                  stroke={PALETTE[i % PALETTE.length]} strokeWidth={2} dot={false} />
          ) : (
            <Bar key={s.key} dataKey={s.key} name={s.label || s.key}
                 fill={PALETTE[i % PALETTE.length]} maxBarSize={36} />
          ),
        )}
      </Wrapper>
    </ResponsiveContainer>
  );
}

export default function ArtifactCard({
  artifact,
  onNavigate,
}: {
  artifact: ArtifactData;
  onNavigate?: ArtifactNavigate;
  // @types/react isn't installed (React 19), so TS doesn't strip `key` from
  // explicitly-annotated props — declare it to keep list rendering type-clean.
  key?: string;
}) {
  const chartRef = useRef<HTMLDivElement | null>(null);
  const columns = artifact.columns.length
    ? artifact.columns
    : Object.keys(artifact.rows[0] || {}).map((k) => ({ key: k, label: k }));

  const exportCsv = useCallback(() => {
    downloadCsv(
      safeFilename(artifact.title, 'csv'),
      columns.map((c) => c.label || c.key),
      artifact.rows.map((r) => columns.map((c) => (r as Record<string, unknown>)[c.key])),
    );
  }, [artifact, columns]);

  if (artifact.kind === 'nav') {
    const buttons = artifact.buttons || [];
    if (buttons.length === 0) return null;
    return (
      <div className="aiw-artifact aiw-artifact-nav">
        <div className="aiw-artifact-head">
          <span className="aiw-artifact-title">{artifact.title}</span>
        </div>
        <div className="aiw-nav-buttons">
          {buttons.map((b, i) => (
            <button
              key={`${b.target}-${b.id}-${i}`}
              type="button"
              className="aiw-nav-btn"
              disabled={!onNavigate}
              title={onNavigate ? undefined : 'Navigation not supported in this view'}
              onClick={() => {
                if (!onNavigate) return;
                if (b.target === 'product') onNavigate(b.project_id ?? 0, b.id);
                else onNavigate(b.id, null);
              }}
            >
              <span className="aiw-nav-kind">{b.target === 'project' ? 'Project' : 'Product'}</span>
              {b.label}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="aiw-artifact">
      <div className="aiw-artifact-head">
        <span className="aiw-artifact-title">{artifact.title}</span>
        <span className="aiw-artifact-tools">
          <button type="button" className="aiw-action-btn" onClick={exportCsv} title="Export CSV">
            <Download size={13} /> CSV
          </button>
          {artifact.kind === 'chart' && (
            <button type="button" className="aiw-action-btn"
                    onClick={() => exportPng(chartRef.current, artifact.title)} title="Export PNG">
              <ImageIcon size={13} /> PNG
            </button>
          )}
        </span>
      </div>
      {artifact.kind === 'chart' && artifact.chart ? (
        <div ref={chartRef}>
          <ChartBody artifact={artifact} />
        </div>
      ) : (
        <div className="table-wrapper aiw-artifact-table">
          <table>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.key}>{c.label || c.key}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {artifact.rows.map((r, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c.key}>{String((r as Record<string, unknown>)[c.key] ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
