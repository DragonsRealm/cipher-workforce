import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useDragWorkspaceFile } from '../useDragWorkspaceFile';
import type { WorkspaceEntry } from '../../types/workspaceFiles';

function makeDragEvent() {
  const setData = vi.fn();
  const preventDefault = vi.fn();
  const event = {
    preventDefault,
    dataTransfer: { setData, effectAllowed: '', getData: vi.fn() },
  } as unknown as React.DragEvent;
  return { event, setData, preventDefault };
}

const ref = {
  kind: 'file' as const,
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
  url: '/api/workspace/wf-1/files/audio/greeting.wav',
  preview: 'audio',
  ref,
};

describe('useDragWorkspaceFile', () => {
  it('carries a payload the drop handler can consume verbatim', () => {
    const { result } = renderHook(() => useDragWorkspaceFile());
    const { event, setData } = makeDragEvent();

    result.current.handleFileDragStart(event, entry);

    // Deep equality, not a shape check: a renamed key here silently breaks
    // ParameterRenderer's drop branch with no type error on either side.
    expect(JSON.parse(setData.mock.calls.find(([type]) => type === 'application/json')![1])).toEqual({
      type: 'workspaceFile',
      path: 'audio/greeting.wav',
      // Forwarded exactly as the server sent it. Rebuilding the ref here
      // would be a second, untyped copy of a Pydantic model that forbids
      // unknown fields.
      ref,
    });
  });

  it('sets a plain-text fallback and a copy effect', () => {
    const { result } = renderHook(() => useDragWorkspaceFile());
    const { event, setData } = makeDragEvent();

    result.current.handleFileDragStart(event, entry);

    expect(setData).toHaveBeenCalledWith('text/plain', 'audio/greeting.wav');
    expect(event.dataTransfer.effectAllowed).toBe('copy');
  });

  it('refuses to drag a directory', () => {
    const { result } = renderHook(() => useDragWorkspaceFile());
    const { event, setData, preventDefault } = makeDragEvent();

    result.current.handleFileDragStart(event, {
      ...entry, is_dir: true, path: 'audio', name: 'audio', ref: null, preview: 'none',
    });

    expect(setData).not.toHaveBeenCalled();
    expect(preventDefault).toHaveBeenCalled();
  });

  it('refuses a row the server gave no reference for', () => {
    const { result } = renderHook(() => useDragWorkspaceFile());
    const { event, setData, preventDefault } = makeDragEvent();

    // Rather than inventing one locally — exactly the coupling that moving
    // ref construction to the server removed.
    result.current.handleFileDragStart(event, { ...entry, ref: null });

    expect(setData).not.toHaveBeenCalled();
    expect(preventDefault).toHaveBeenCalled();
  });
});
