import { describe, expect, it, vi } from 'vitest';

import { assignWorkspaceFile, buildWorkspaceFilePayload } from '../workspaceFileAssign';
import type { WorkspaceEntry, WorkspaceFileRef } from '../../types/workspaceFiles';

const ref: WorkspaceFileRef = {
  kind: 'file',
  path: 'audio/greeting.wav',
  workflow_id: 'wf-1',
  filename: 'greeting.wav',
  mime_type: 'audio/wav',
  size_bytes: 20844,
  modified_at: '2026-07-25T22:47:10',
  url: '/api/workspace/wf-1/files/audio/greeting.wav',
};

const entry: WorkspaceEntry = {
  name: 'greeting.wav',
  path: 'audio/greeting.wav',
  is_dir: false,
  size_bytes: 20844,
  modified_at: '2026-07-25T22:47:10',
  mime_type: 'audio/wav',
  url: ref.url as string,
  preview: 'audio',
  ref,
};

const payload = buildWorkspaceFilePayload(entry)!;

describe('buildWorkspaceFilePayload', () => {
  it('forwards the server-built ref rather than reconstructing one', () => {
    expect(payload).toEqual({ type: 'workspaceFile', path: 'audio/greeting.wav', ref });
  });

  it('refuses a directory', () => {
    expect(buildWorkspaceFilePayload({ ...entry, is_dir: true, ref: null })).toBeNull();
  });

  it('refuses a row the server gave no ref for', () => {
    expect(buildWorkspaceFilePayload({ ...entry, ref: null })).toBeNull();
  });
});

describe('assignWorkspaceFile', () => {
  it('replaces a file parameter with the whole reference', () => {
    const onChange = vi.fn();
    assignWorkspaceFile(payload, { parameterType: 'file', currentValue: '', onChange });
    // Deep equality, not just .path — a file param stores the ref object.
    expect(onChange).toHaveBeenCalledWith(ref);
  });

  it('appends to a text parameter instead of replacing it', () => {
    const onChange = vi.fn();
    assignWorkspaceFile(payload, { parameterType: 'string', currentValue: 'Transcribe', onChange });
    expect(onChange).toHaveBeenCalledWith('Transcribe audio/greeting.wav');
  });

  it('does not double-space when the text already ends in one', () => {
    const onChange = vi.fn();
    assignWorkspaceFile(payload, { parameterType: 'string', currentValue: 'Transcribe ', onChange });
    expect(onChange).toHaveBeenCalledWith('Transcribe audio/greeting.wav');
  });

  it('starts an empty text parameter with no leading space', () => {
    const onChange = vi.fn();
    assignWorkspaceFile(payload, { parameterType: 'string', currentValue: '', onChange });
    expect(onChange).toHaveBeenCalledWith('audio/greeting.wav');
  });

  it('survives a non-string current value on a text parameter', () => {
    const onChange = vi.fn();
    // A `file` param's value is an object; `.endsWith` on one would throw.
    assignWorkspaceFile(payload, { parameterType: 'json', currentValue: ref, onChange });
    expect(onChange).toHaveBeenCalledWith('audio/greeting.wav');
  });
});
