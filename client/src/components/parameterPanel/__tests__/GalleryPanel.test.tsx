import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders as render } from '../../../test/providers';
import type { WorkspaceEntry } from '../../../types/workspaceFiles';

const { wsMock } = vi.hoisted(() => ({ wsMock: { sendRequest: vi.fn() } }));
vi.mock('../../../contexts/WebSocketContext', () => ({ useWebSocket: () => wsMock }));

import GalleryPanel from '../GalleryPanel';

const dir = (name: string): WorkspaceEntry => ({
  name, path: name, is_dir: true, size_bytes: 0,
  modified_at: '2026-07-20T10:00:00', mime_type: null, url: null,
  preview: 'none', ref: null,
});

// Mirrors what the server sends: the ref and the preview verdict arrive
// with the row, so the panel never derives either.
const file = (path: string, mime = 'audio/wav'): WorkspaceEntry => {
  const name = path.split('/').pop() as string;
  const url = `/api/workspace/wf-1/files/${path}`;
  return {
    name,
    path,
    is_dir: false,
    size_bytes: 20844,
    modified_at: '2026-07-25T22:47:10',
    mime_type: mime,
    url,
    preview: 'audio',
    ref: {
      kind: 'file', path, workflow_id: 'wf-1', filename: name,
      mime_type: mime, size_bytes: 20844,
      modified_at: '2026-07-25T22:47:10', url,
    },
  };
};

const listing = (entries: WorkspaceEntry[], extra: Record<string, unknown> = {}) => ({
  success: true, workflow_id: 'wf-1', path: '', parent: null, crumbs: [],
  entries, count: entries.length, truncated: false,
  workspace_exists: true, path_exists: true, ...extra,
});

const noop = () => {};

describe('GalleryPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    wsMock.sendRequest.mockResolvedValue(listing([dir('audio'), file('greeting.wav')]));
  });

  it('lists the workspace and shows a size for files but not folders', async () => {
    render(<GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={noop} />);

    expect(await screen.findByText('greeting.wav')).toBeInTheDocument();
    expect(screen.getByText('audio')).toBeInTheDocument();
    expect(screen.getByText('20 KB')).toBeInTheDocument();
    expect(screen.getByText('Folder')).toBeInTheDocument();
    expect(wsMock.sendRequest).toHaveBeenCalledWith('list_workspace_files', {
      workflow_id: 'wf-1', path: '', limit: 500,
    });
  });

  it('writes the node path when a folder is opened, so the node lists what you browse', async () => {
    const onParameterChange = vi.fn();
    render(<GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={onParameterChange} />);

    fireEvent.click(await screen.findByText('audio'));

    expect(onParameterChange).toHaveBeenCalledWith('path', 'audio');
  });

  it('renders the breadcrumb trail the server computed', async () => {
    wsMock.sendRequest.mockResolvedValue(listing([file('audio/clips/one.wav')], {
      path: 'audio/clips',
      parent: 'audio',
      crumbs: [{ name: 'audio', path: 'audio' }, { name: 'clips', path: 'audio/clips' }],
    }));
    render(<GalleryPanel workflowId="wf-1" parameters={{ path: 'audio/clips' }} onParameterChange={noop} />);

    await waitFor(() => expect(wsMock.sendRequest).toHaveBeenCalledWith('list_workspace_files', {
      workflow_id: 'wf-1', path: 'audio/clips', limit: 500,
    }));
    // Not re-split from `path` here — the trail arrives ready to render.
    expect(await screen.findByRole('button', { name: 'audio' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'clips' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Workspace root' })).toBeInTheDocument();
  });

  it('pins a file into `selection` and unpins it again', async () => {
    const onParameterChange = vi.fn();
    const { rerender } = render(
      <GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={onParameterChange} />,
    );

    fireEvent.click(await screen.findByRole('checkbox', { name: /Pin greeting\.wav/ }));
    expect(onParameterChange).toHaveBeenCalledWith('selection', ['greeting.wav']);

    rerender(
      <GalleryPanel
        workflowId="wf-1"
        parameters={{ selection: ['greeting.wav'] }}
        onParameterChange={onParameterChange}
      />,
    );
    fireEvent.click(await screen.findByRole('checkbox', { name: /Pin greeting\.wav/ }));
    expect(onParameterChange).toHaveBeenLastCalledWith('selection', []);
  });

  it('never calls the backend without a workflow, and says why', async () => {
    render(<GalleryPanel parameters={{}} onParameterChange={noop} />);

    expect(await screen.findByText(/Save this workflow to give it a workspace/)).toBeInTheDocument();
    expect(wsMock.sendRequest).not.toHaveBeenCalled();
  });

  it('distinguishes "no workspace yet" from "empty folder"', async () => {
    wsMock.sendRequest.mockResolvedValue(listing([], { workspace_exists: false }));
    const { unmount } = render(
      <GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={noop} />,
    );
    expect(await screen.findByText(/No workspace yet/)).toBeInTheDocument();
    unmount();

    wsMock.sendRequest.mockResolvedValue(listing([]));
    render(<GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={noop} />);
    expect(await screen.findByText('This folder is empty.')).toBeInTheDocument();
  });

  it('surfaces a failed listing instead of showing an empty workspace', async () => {
    wsMock.sendRequest.mockResolvedValue({ success: false, error: 'outside this workflow' });
    render(<GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={noop} />);

    expect(await screen.findByText('outside this workflow')).toBeInTheDocument();
  });

  it('sends the raw search term and lets the server decide what it means', async () => {
    render(<GalleryPanel workflowId="wf-1" parameters={{ path: 'audio' }} onParameterChange={noop} />);
    await screen.findByText('greeting.wav');

    fireEvent.change(screen.getByLabelText('Search this workspace'), { target: { value: 'greet' } });

    // No glob is assembled here: term-to-pattern is `search_to_pattern` on
    // the server, so the node and the panel cannot disagree about it.
    await waitFor(() => expect(wsMock.sendRequest).toHaveBeenCalledWith('list_workspace_files', {
      workflow_id: 'wf-1', path: 'audio', search: 'greet', limit: 500,
    }));
  });

  it('makes files draggable and folders not', async () => {
    render(<GalleryPanel workflowId="wf-1" parameters={{}} onParameterChange={noop} />);
    await screen.findByText('greeting.wav');

    const tileOf = (label: string) =>
      screen.getByText(label).closest('[role="button"]') as HTMLElement;

    expect(tileOf('greeting.wav')).toHaveAttribute('draggable', 'true');
    expect(tileOf('audio')).toHaveAttribute('draggable', 'false');
  });
});
