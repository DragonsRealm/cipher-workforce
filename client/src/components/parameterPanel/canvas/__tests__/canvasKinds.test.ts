import { describe, expect, it } from 'vitest';

import type { CanvasItem } from '../../../../lib/canvasBoard';
import {
  itemLabel,
  parentDirOf,
  prismLanguageFor,
  resolveRenderKind,
} from '../canvasKinds';

const fileItem = (
  filename: string,
  mime?: string,
  overrides: Partial<CanvasItem> = {},
): CanvasItem => ({
  id: `item-${filename}`,
  kind: 'file',
  title: null,
  ref: {
    kind: 'file',
    path: `media/${filename}`,
    filename,
    mime_type: mime,
    workflow_id: 'wf-1',
    url: `/api/workspace/wf-1/files/media/${filename}`,
  },
  url: null,
  content: null,
  language: null,
  source: 'workflow',
  created_at: null,
  ...overrides,
});

describe('resolveRenderKind', () => {
  it('routes notes and urls before any file inspection', () => {
    expect(
      resolveRenderKind({ ...fileItem('x'), kind: 'note', ref: null, content: '# hi' }),
    ).toBe('note');
    expect(
      resolveRenderKind({ ...fileItem('x'), kind: 'url', ref: null, url: 'https://a.dev' }),
    ).toBe('web-external');
  });

  it('routes media by mime prefix, and by ref.kind when mime is absent', () => {
    expect(resolveRenderKind(fileItem('a.png', 'image/png'))).toBe('media-image');
    expect(resolveRenderKind(fileItem('a.mp4', 'video/mp4'))).toBe('media-video');
    expect(resolveRenderKind(fileItem('a.wav', 'audio/wav'))).toBe('media-audio');

    const probedAudio = fileItem('a.bin');
    probedAudio.ref!.kind = 'audio';
    probedAudio.ref!.mime_type = undefined;
    expect(resolveRenderKind(probedAudio)).toBe('media-audio');
  });

  it('mime beats extension', () => {
    // A .txt the server identified as an image renders as an image.
    expect(resolveRenderKind(fileItem('shot.txt', 'image/png'))).toBe('media-image');
  });

  it('routes pdf and html to their dedicated surfaces before the text tiers', () => {
    expect(resolveRenderKind(fileItem('report.pdf', 'application/pdf'))).toBe('pdf');
    expect(resolveRenderKind(fileItem('report.pdf'))).toBe('pdf');
    expect(resolveRenderKind(fileItem('page.html', 'text/html'))).toBe('web-srcdoc');
    expect(resolveRenderKind(fileItem('page.htm'))).toBe('web-srcdoc');
  });

  it('routes markdown, json, code, plain text, then binary', () => {
    expect(resolveRenderKind(fileItem('notes.md', 'text/markdown'))).toBe('markdown');
    expect(resolveRenderKind(fileItem('data.json', 'application/json'))).toBe('json');
    expect(resolveRenderKind(fileItem('script.py'))).toBe('code');
    expect(resolveRenderKind(fileItem('run.log', 'text/plain'))).toBe('text');
    expect(resolveRenderKind(fileItem('blob.bin', 'application/octet-stream'))).toBe('binary');
  });

  it('is binary when a file item somehow has no ref', () => {
    expect(resolveRenderKind({ ...fileItem('x'), ref: null })).toBe('binary');
  });
});

describe('prismLanguageFor', () => {
  it('derives the grammar from the extension', () => {
    expect(prismLanguageFor(fileItem('a.py'))).toBe('python');
    expect(prismLanguageFor(fileItem('a.tsx'))).toBe('typescript');
    expect(prismLanguageFor(fileItem('a.rs'))).toBeNull();
  });

  it('lets item.language override the extension', () => {
    const item = fileItem('a.txt');
    item.language = 'yaml';
    expect(prismLanguageFor(item)).toBe('yaml');
  });
});

describe('helpers', () => {
  it('parentDirOf returns the workspace-relative folder', () => {
    expect(parentDirOf('media/shots/a.png')).toBe('media/shots');
    expect(parentDirOf('a.png')).toBe('');
  });

  it('itemLabel prefers title, then filename, then url, then kind', () => {
    expect(itemLabel({ ...fileItem('a.png'), title: 'Chart' })).toBe('Chart');
    expect(itemLabel(fileItem('a.png'))).toBe('a.png');
    expect(
      itemLabel({ ...fileItem('x'), kind: 'url', ref: null, url: 'https://a.dev' }),
    ).toBe('https://a.dev');
    expect(
      itemLabel({ ...fileItem('x'), kind: 'note', ref: null, content: 'hi' }),
    ).toBe('Note');
  });
});
