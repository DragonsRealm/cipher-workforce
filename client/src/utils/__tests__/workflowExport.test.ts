import { describe, expect, it } from 'vitest';
import type { Node } from 'reactflow';

import { sanitizeNodes } from '../workflowExport';


describe('sanitizeNodes', () => {
  it('preserves backend-owned Context identity and strips runtime payloads', () => {
    const nodes: Node[] = [
      {
        id: 'ctx',
        type: 'context',
        position: { x: 10, y: 20 },
        data: {
          label: 'Context',
          systemManaged: true,
          agentNodeId: 'agent',
          contextJournal: [{ role: 'user', content: 'secret' }],
          providerBindings: { session: 'secret' },
        },
      },
    ];

    expect(sanitizeNodes(nodes)[0].data).toEqual({
      label: 'Context',
      systemManaged: true,
      agentNodeId: 'agent',
    });
  });
});
