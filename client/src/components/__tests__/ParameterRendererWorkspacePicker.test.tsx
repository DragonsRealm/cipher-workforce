import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders as render } from '../../test/providers';
import { useAppStore } from '../../store/useAppStore';
import type { WorkspaceEntry } from '../../types/workspaceFiles';

const { wsMock, apiKeysMock } = vi.hoisted(() => ({
  wsMock: {
    getNodeParameters: vi.fn().mockResolvedValue({}),
    sendRequest: vi.fn(),
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

const file: WorkspaceEntry = {
  name: 'greeting.wav',
  path: 'audio/greeting.wav',
  is_dir: false,
  size_bytes: 20844,
  modified_at: '2026-07-25T22:47:10',
  mime_type: 'audio/wav',
  url: ref.url,
  preview: 'audio',
  ref,
};

const listing = (entries: WorkspaceEntry[]) => ({
  success: true, workflow_id: 'wf-1', path: '', parent: null, crumbs: [],
  entries, count: entries.length, truncated: false,
  workspace_exists: true, path_exists: true,
});

const fileParam = { displayName: 'Audio', name: 'audio_file', type: 'file' } as any;

/**
 * The whole assignment, using only discrete clicks.
 *
 * This is the SC 2.5.7 / F108 test procedure: the same function the drag
 * performs must be operable by single pointer activations with no dragging.
 * Nothing in here may fire a drag event or a key.
 */
const pickFileByClickingOnly = async () => {
  fireEvent.click(await screen.findByRole('button', { name: /choose from workspace/i }));
  fireEvent.click(await screen.findByText('greeting.wav'));
  fireEvent.click(await screen.findByRole('button', { name: /use this file/i }));
};

describe('ParameterRenderer — workspace picker (WCAG 2.2 SC 2.5.7)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    wsMock.sendRequest.mockResolvedValue(listing([file]));
    useAppStore.setState({ currentWorkflow: { id: 'wf-1', name: 'W', nodes: [], edges: [] } as any });
  });

  it('assigns a file using single pointer activations only — no dragging', async () => {
    const onChange = vi.fn();
    render(<ParameterRenderer parameter={fileParam} value="" onChange={onChange} />);

    await pickFileByClickingOnly();

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(ref));
  });

  it('reaches the same end state as the drag, through the same function', async () => {
    // Run A: the drag path.
    const onChangeDrag = vi.fn();
    const { unmount } = render(
      <ParameterRenderer parameter={fileParam} value="" onChange={onChangeDrag} />,
    );
    fireEvent.drop(screen.getByPlaceholderText('Enter file path or upload'), {
      dataTransfer: {
        getData: (type: string) =>
          type === 'application/json'
            ? JSON.stringify({ type: 'workspaceFile', path: file.path, ref })
            : '',
        setData: vi.fn(),
        types: ['application/json'],
      },
    });
    unmount();

    // Run B: the pointer path.
    const onChangePick = vi.fn();
    render(<ParameterRenderer parameter={fileParam} value="" onChange={onChangePick} />);
    await pickFileByClickingOnly();
    await waitFor(() => expect(onChangePick).toHaveBeenCalled());

    // G219 asks that the alternative operate *the same function*. This is
    // what turns that from a claim into an assertion.
    expect(onChangePick.mock.calls[0][0]).toEqual(onChangeDrag.mock.calls[0][0]);
  });

  it('is available with no gallery node in the workflow and no gallery pane open', async () => {
    useAppStore.setState({
      currentWorkflow: { id: 'wf-1', name: 'W', nodes: [], edges: [] } as any,
    });
    render(<ParameterRenderer parameter={fileParam} value="" onChange={vi.fn()} />);

    // The alternative must not depend on the user having placed a gallery
    // node, nor on a panel that can never be co-rendered with this input.
    expect(
      await screen.findByRole('button', { name: /choose from workspace/i }),
    ).toBeEnabled();
  });

  it('is hidden when there is no workflow, since there is no workspace to browse', () => {
    useAppStore.setState({ currentWorkflow: null as any });
    render(<ParameterRenderer parameter={fileParam} value="" onChange={vi.fn()} />);

    expect(screen.queryByRole('button', { name: /choose from workspace/i })).toBeNull();
    // Upload's own unsaved-workflow behaviour is untouched.
    expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
  });

  it('offers no drag surface inside the picker', async () => {
    const { container } = render(
      <ParameterRenderer parameter={fileParam} value="" onChange={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole('button', { name: /choose from workspace/i }));
    await screen.findByText('greeting.wav');

    // An alternative that reintroduced the barrier would defeat its purpose.
    expect(container.querySelectorAll('[draggable="true"]')).toHaveLength(0);
  });

  it('keeps the confirm button inert until a file is chosen', async () => {
    render(<ParameterRenderer parameter={fileParam} value="" onChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /choose from workspace/i }));

    expect(await screen.findByRole('button', { name: /use this file/i })).toBeDisabled();
  });
});
