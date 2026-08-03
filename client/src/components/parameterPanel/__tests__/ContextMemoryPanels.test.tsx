import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders as render } from '../../../test/providers';
import ContextPanel from '../ContextPanel';
import MemoryToolPanel from '../MemoryToolPanel';

const sendRequest = vi.fn();

vi.mock('../../../contexts/WebSocketContext', () => ({
  useWebSocket: () => ({ sendRequest }),
}));

beforeEach(() => {
  sendRequest.mockReset();
});

describe('ContextPanel', () => {
  it('loads authorized journal content through get_agent_context', async () => {
    sendRequest.mockImplementation(async (operation: string) => {
      if (operation === 'get_agent_context') {
        return {
          context: {
            thread_id: 'session:abc',
            epoch: 3,
            revision: 9,
            provider: 'openai',
            fidelity: 'provider_replayable',
            resumable: true,
            active_token_count: 800,
            context_window: 10_000,
            provider_binding_status: 'bound',
            events: [
              {
                sequence: 7,
                event_type: 'assistant_message',
                message_wire_v2: { role: 'assistant', blocks: [{ type: 'text', text: 'hello' }] },
              },
            ],
          },
        };
      }
      return {};
    });

    render(<ContextPanel nodeId="ctx-1" workflowId="wf-1" />);

    expect(await screen.findByText('session:abc / 3')).toBeInTheDocument();
    expect(screen.getByText('assistant_message')).toBeInTheDocument();
    expect(sendRequest).toHaveBeenCalledWith(
      'get_agent_context',
      expect.objectContaining({
        workflow_id: 'wf-1',
        context_node_id: 'ctx-1',
        view: 'journal',
        limit: 50,
      }),
    );
  });

  it('invokes backend epoch clearing without synthesizing local Context', async () => {
    sendRequest.mockImplementation(async (operation: string) => {
      if (operation === 'get_agent_context') return { context: { events: [] } };
      if (operation === 'clear_agent_context') return { success: true };
      return {};
    });
    const user = userEvent.setup();
    render(<ContextPanel nodeId="ctx-1" workflowId="wf-1" />);

    await screen.findByText('No committed Context events yet.');
    await user.click(screen.getByRole('button', { name: /Clear epoch/i }));

    await waitFor(() =>
      expect(sendRequest).toHaveBeenCalledWith('clear_agent_context', {
        workflow_id: 'wf-1',
        context_node_id: 'ctx-1',
      }),
    );
  });
});

describe('MemoryToolPanel', () => {
  it('lists and remembers items through the backend Memory operations', async () => {
    sendRequest.mockImplementation(async (operation: string) => {
      if (operation === 'list_memory_items') {
        return { items: [], indexing_state: 'lexical_ready' };
      }
      if (operation === 'remember_memory') {
        return {
          item: {
            id: 'mem-1',
            version: 1,
            content: 'The launch is Tuesday',
          },
        };
      }
      if (operation === 'get_memory_item') {
        return {
          item: {
            id: 'mem-1',
            version: 1,
            content: 'The launch is Tuesday',
          },
        };
      }
      return {};
    });
    const user = userEvent.setup();
    render(
      <MemoryToolPanel
        nodeId="memory-1"
        workflowId="wf-1"
        parameters={{ reset_policy: 'preserve' }}
        onParameterChange={vi.fn()}
      />,
    );

    await screen.findByText('No Memory items match this query.');
    await user.type(
      screen.getByPlaceholderText('Durable fact, preference, decision, or note'),
      'The launch is Tuesday',
    );
    await user.click(screen.getByRole('button', { name: 'Remember' }));

    await waitFor(() =>
      expect(sendRequest).toHaveBeenCalledWith(
        'remember_memory',
        expect.objectContaining({
          workflow_id: 'wf-1',
          memory_node_id: 'memory-1',
          content: 'The launch is Tuesday',
        }),
      ),
    );
  });
});
