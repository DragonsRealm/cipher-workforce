/**
 * Pure renderer dispatch for Canvas board items. No React.
 *
 * Order matters: mime beats extension, and the script-bearing/document
 * verdicts (html/pdf) are decided before the generic text tiers so a
 * `.html` file never falls through to the plain code view by accident.
 */

import type { CanvasItem } from '../../../lib/canvasBoard';

export type RenderVerdict =
  | 'note'
  | 'web-external'
  | 'media-image'
  | 'media-video'
  | 'media-audio'
  | 'pdf'
  | 'web-srcdoc'
  | 'markdown'
  | 'json'
  | 'code'
  | 'text'
  | 'binary';

/** Cap above which Prism highlighting is skipped (plain <pre> instead). */
export const HIGHLIGHT_MAX_CHARS = 100_000;

const extensionOf = (name: string): string => {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
};

/** Reuses the gallery's FileCode extension family (fileIcons.tsx), minus
 * html (its own verdict) — plus plain-text extensions. */
const CODE_EXTENSIONS = new Set([
  'py', 'js', 'jsx', 'ts', 'tsx', 'sh', 'ps1', 'rb', 'go', 'rs',
  'java', 'c', 'cpp', 'h', 'sql', 'css', 'xml', 'yaml', 'yml', 'toml',
]);
const TEXT_EXTENSIONS = new Set(['txt', 'log', 'csv', 'tsv', 'rtf', 'ini', 'env']);

/** Extension -> Prism grammar name. Anything absent renders un-highlighted. */
const PRISM_LANGUAGE: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  jsonl: 'json',
  md: 'markdown',
  markdown: 'markdown',
  sh: 'bash',
  bash: 'bash',
  yaml: 'yaml',
  yml: 'yaml',
};

export const prismLanguageFor = (item: CanvasItem): string | null => {
  if (item.language) {
    const declared = item.language.toLowerCase();
    return PRISM_LANGUAGE[declared] ?? declared;
  }
  const name = item.ref?.filename ?? '';
  return PRISM_LANGUAGE[extensionOf(name)] ?? null;
};

export function resolveRenderKind(item: CanvasItem): RenderVerdict {
  if (item.kind === 'note') return 'note';
  if (item.kind === 'url') return 'web-external';

  const ref = item.ref;
  if (!ref) return 'binary';

  const mime = ref.mime_type ?? '';
  const refKind = ref.kind;
  const ext = extensionOf(ref.filename);

  if (mime.startsWith('image/') || refKind === 'image') return 'media-image';
  if (mime.startsWith('video/') || refKind === 'video') return 'media-video';
  if (mime.startsWith('audio/') || refKind === 'audio') return 'media-audio';

  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (mime === 'text/html' || ext === 'html' || ext === 'htm') return 'web-srcdoc';

  if (mime === 'text/markdown' || ext === 'md' || ext === 'markdown') return 'markdown';
  if (mime === 'application/json' || ext === 'json' || ext === 'jsonl') return 'json';

  if (CODE_EXTENSIONS.has(ext)) return 'code';
  if (TEXT_EXTENSIONS.has(ext) || mime.startsWith('text/')) return 'text';

  return 'binary';
}

/** Verdicts whose body is fetched as text over the workspace route. */
export const FETCHES_TEXT: ReadonlySet<RenderVerdict> = new Set([
  'web-srcdoc',
  'markdown',
  'json',
  'code',
  'text',
]);

/** Workspace-relative parent directory of a ref path ('' at the root). */
export const parentDirOf = (path: string): string => {
  const slash = path.lastIndexOf('/');
  return slash > 0 ? path.slice(0, slash) : '';
};

/** Display label for an item: title, then filename/url, then kind. */
export const itemLabel = (item: CanvasItem): string =>
  item.title ||
  item.ref?.filename ||
  item.url ||
  (item.kind === 'note' ? 'Note' : item.kind);
