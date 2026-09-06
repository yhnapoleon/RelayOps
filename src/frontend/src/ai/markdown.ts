/**
 * Markdown rendering with syntax highlighting and sanitization.
 *
 * Ported from Frontier's lib/markdown.js (framework-agnostic). Tables get a
 * CSV-export button in the wrapper; the click is handled by delegation in
 * AIWorkbench (the rendered HTML stays framework-agnostic).
 */

import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js';
import taskLists from 'markdown-it-task-lists';

const md: MarkdownIt = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  breaks: true,
  highlight: (str: string, lang: string): string => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return (
          '<pre class="hljs"><code>' +
          hljs.highlight(str, { language: lang, ignoreIllegals: true }).value +
          '</code></pre>'
        );
      } catch {
        /* fall through to escaped output */
      }
    }
    return '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + '</code></pre>';
  },
}).use(taskLists);

md.enable(['table', 'strikethrough']);

// Open links in a new tab so the chat isn't navigated away from.
const defaultLinkOpen =
  md.renderer.rules.link_open ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx];
  token.attrSet('target', '_blank');
  token.attrSet('rel', 'noopener noreferrer');
  return defaultLinkOpen(tokens, idx, options, env, self);
};

// Scrollable wrapper so wide tables don't blow up the bubble width.
const defaultTableOpen =
  md.renderer.rules.table_open ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));
const defaultTableClose =
  md.renderer.rules.table_close ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));
md.renderer.rules.table_open = (tokens, idx, options, env, self) =>
  '<div class="table-wrapper">' +
  '<button class="md-table-export" type="button" title="Export CSV">CSV</button>' +
  defaultTableOpen(tokens, idx, options, env, self);
md.renderer.rules.table_close = (tokens, idx, options, env, self) =>
  defaultTableClose(tokens, idx, options, env, self) + '</div>';

export function renderMarkdown(markdown: string): string {
  if (!markdown) return '';
  const html = md.render(markdown);
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'p', 'br', 'strong', 'em', 'u', 's', 'code', 'pre',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'ul', 'ol', 'li', 'blockquote', 'a', 'img',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'hr', 'del', 'ins', 'div', 'span', 'input', 'button',
    ],
    ALLOWED_ATTR: ['href', 'title', 'alt', 'src', 'class', 'target', 'rel', 'type', 'checked', 'disabled'],
    ALLOW_DATA_ATTR: false,
  });
}
