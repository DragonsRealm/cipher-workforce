import { fireEvent, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders as render } from '../../test/providers';
import type { WorkspaceFileDragPayload } from '../../types/workspaceFiles';

const { wsMock, apiKeysMock } = vi.hoisted(() => ({
  wsMock: {
    getNodeParameters: vi.fn().mockResolvedValue({}),
    sendRequest: vi.fn().mockResolvedValue({ success: true }),
    getWhatsAppGroups: vi.fn().mockResolvedValue([]),
    getWhatsAppGroupInfo: vi.fn().mockResolvedValue(null),
    getWhatsAppChannels: vi.fn().mockResolvedValue([]),
  },
  apiKeysMock: {
    getStoredApiKey: vi.fn().mockResolvedValue(null),
    hasStoredKey: vi.fn().mockReturnValue(false),
    getStoredModels: vi.fn().mockResolvedValue([]),
    getProviderDefaults: vi.fn().mockResolvedValue(null),
  },
}));
vi.mock('../../contexts/WebSocketContext', () => ({ useWebSocket: () => wsMock }));
vi.mock('../../hooks/useApiKeys', () => ({ useApiKeys: () => apiKeysMock }));

import ParameterRenderer from '../ParameterRenderer';

const payload: WorkspaceFileDragPayload = {
  type: 'workspaceFile',
  path: 'audio/greeting.wav',
  ref: {
    kind: 'file',
    path: 'audio/greeting.wav',
    workflow_id: 'wf-1',
    filename: 'greeting.wav',
    mime_type: 'audio/wav',
    size_bytes: 20844,
    modified_at: '2026-07-25T22:47:10',
    url: '/api/workspace/wf-1/files/audio/greeting.wav',
  },
};

/** A dataTransfer that answers only the MIME types actually provided. */
const transfer = (data: Record<string, string>) => ({
  getData: (type: string) => data[type] ?? '',
  setData: vi.fn(),
  dropEffect: 'copy',
  effectAllowed: 'copy',
  types: Object.keys(data),
});

const dropOn = (element: Element, data: Record<string, string>) =>
  fireEvent.drop(element, { dataTransfer: transfer(data) });

const jsonPayload = { 'application/json': JSON.stringify(payload), 'text/plain': payload.path };

describe('ParameterRenderer — workspace file drop', () => {
  beforeEach(() => vi.clearAllMocks());

  it('assigns the whole reference to a file parameter', () => {
    const onChange = vi.fn();
    render(
      <ParameterRenderer
        parameter={{ displayName: 'Audio', name: 'audio_file', type: 'file' } as any}
        value=""
        onChange={onChange}
      />,
    );

    dropOn(screen.getByPlaceholderText('Enter file path or upload'), jsonPayload);

    expect(onChange).toHaveBeenCalledWith(payload.ref);
  });

  it('appends the path to a text parameter instead of replacing it', () => {
    const onChange = vi.fn();
    render(
      <ParameterRenderer
        parameter={{ displayName: 'Prompt', name: 'prompt', type: 'string' } as any}
        value="Transcribe"
        onChange={onChange}
      />,
    );

    dropOn(screen.getByRole('textbox'), jsonPayload);

    // The headline guarantee: a half-written prompt survives the drop.
    expect(onChange).toHaveBeenCalledWith('Transcribe audio/greeting.wav');
  });

  it('does not double-space when the text already ends in one', () => {
    const onChange = vi.fn();
    render(
      <ParameterRenderer
        parameter={{ displayName: 'Prompt', name: 'prompt', type: 'string' } as any}
        value="Transcribe "
        onChange={onChange}
      />,
    );

    dropOn(screen.getByRole('textbox'), jsonPayload);

    expect(onChange).toHaveBeenCalledWith('Transcribe audio/greeting.wav');
  });

  it('starts a bare text parameter with no leading space', () => {
    const onChange = vi.fn();
    render(
      <ParameterRenderer
        parameter={{ displayName: 'Prompt', name: 'prompt', type: 'string' } as any}
        value=""
        onChange={onChange}
      />,
    );

    dropOn(screen.getByRole('textbox'), jsonPayload);

    expect(onChange).toHaveBeenCalledWith('audio/greeting.wav');
  });

  it('ignores a bare path with no structured payload', () => {
    const onChange = vi.fn();
    render(
      <ParameterRenderer
        parameter={{ displayName: 'Prompt', name: 'prompt', type: 'string' } as any}
        value=""
        onChange={onChange}
      />,
    );

    // Only `{{...}}` templates are accepted off text/plain; a naked path
    // from an unrelated drag source must not silently rewrite a parameter.
    dropOn(screen.getByRole('textbox'), { 'text/plain': 'audio/greeting.wav' });

    expect(onChange).not.toHaveBeenCalled();
  });

  it('renders a dropped reference as a named chip, not raw JSON', () => {
    render(
      <ParameterRenderer
        parameter={{ displayName: 'Audio', name: 'audio_file', type: 'file' } as any}
        value={payload.ref}
        onChange={vi.fn()}
      />,
    );

    // kind is "file", not "audio" — the pre-gallery check looked only for
    // "audio" and would have fallen through to an empty input here.
    expect(screen.getByDisplayValue('greeting.wav')).toBeInTheDocument();
    expect(screen.getByText(/audio\/greeting\.wav/)).toBeInTheDocument();
  });
});
