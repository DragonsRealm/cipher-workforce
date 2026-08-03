import { describe, expect, it } from 'vitest';
import {
  WORKFLOW_CONTROL_REQUEST_TIMEOUT,
  assertWorkflowControlMutationSucceeded,
  extractWorkflowControlStatusSnapshot,
  isWorkflowControlMutationConfirmed,
  mergeWorkflowControlStatus,
  normalizeWorkflowControlStatus,
  shouldRetryResetWorkflowAfterConflict,
  type WorkflowControlMutationAction,
  type WorkflowControlState,
  type WorkflowControlStatus,
  type WorkflowStartResult,
} from '../WebSocketContext';

const status = (
  overrides: Partial<WorkflowControlStatus> = {},
): WorkflowControlStatus => ({
  workflow_id: 'workflow-1',
  generation: 1,
  state: 'running',
  revision: 1,
  active_count: 0,
  in_flight_count: 0,
  queued_count: 0,
  can_start: false,
  can_pause: true,
  can_resume: false,
  can_reset: true,
  can_edit: false,
  ...overrides,
});

describe('workflow control status ordering', () => {
  it('rejects an older generation even when it carries a larger revision', () => {
    const current = status({ generation: 3, revision: 2, state: 'running' });
    const stale = status({ generation: 2, revision: 99, state: 'paused' });

    expect(mergeWorkflowControlStatus(current, stale)).toBe(current);
  });

  it('accepts a newer pre-migration generation whose revision restarted', () => {
    const current = status({ generation: 2, revision: 8, state: 'ready' });
    const incoming = status({ generation: 3, revision: 1, state: 'starting' });

    expect(mergeWorkflowControlStatus(current, incoming)).toBe(incoming);
  });

  it('rejects a lower revision in the same generation', () => {
    const current = status({ generation: 4, revision: 7, state: 'paused' });
    const stale = status({ generation: 4, revision: 6, state: 'running' });

    expect(mergeWorkflowControlStatus(current, stale)).toBe(current);
  });

  it('accepts equal-version runtime count refreshes', () => {
    const current = status({ generation: 4, revision: 7, active_count: 1 });
    const incoming = status({ generation: 4, revision: 7, active_count: 2 });

    expect(mergeWorkflowControlStatus(current, incoming)).toBe(incoming);
  });

  it('normalizes nested snapshots and numeric version strings', () => {
    const normalized = normalizeWorkflowControlStatus({
      status: {
        workflow_id: 'workflow-1',
        generation: '5',
        revision: '11',
        state: 'paused',
        active_runs: '2',
      },
    });

    expect(normalized).toMatchObject({
      workflow_id: 'workflow-1',
      generation: 5,
      revision: 11,
      state: 'paused',
      active_count: 2,
      can_resume: true,
    });
  });
});

describe('workflow control mutation responses', () => {
  it('uses a lifecycle timeout long enough for durable setup and teardown', () => {
    expect(WORKFLOW_CONTROL_REQUEST_TIMEOUT).toBe(5 * 60 * 1000);
  });

  it('rejects explicit success:false responses with the backend error', () => {
    expect(() => assertWorkflowControlMutationSucceeded({
      success: false,
      error: 'control_revision_conflict',
      state: 'never_started',
    })).toThrow('control_revision_conflict');
  });

  it('returns successful status payloads unchanged', () => {
    const response = { success: true, generation: 2, revision: 3 };
    expect(assertWorkflowControlMutationSucceeded(response)).toBe(response);
  });

  it('preserves a successful Start graph and aliases in the typed snapshot', () => {
    const response = {
      success: true,
      workflow_id: 'workflow-1',
      generation: 2,
      state: 'running',
      revision: 3,
      graph: {
        graphVersion: 2,
        nodes: [{ id: 'workflow-1:context:1', type: 'context' }],
        edges: [{
          id: 'context-edge',
          source: 'workflow-1:context:1',
          target: 'workflow-1:agent:1',
        }],
      },
      aliases: {
        'legacy-agent': 'workflow-1:agent:1',
      },
    };

    const snapshot: WorkflowStartResult = normalizeWorkflowControlStatus(response);

    expect(snapshot.graph).toEqual(response.graph);
    expect(snapshot.aliases).toEqual(response.aliases);
  });

  it('extracts the authoritative snapshot carried by a success:false response', () => {
    const current = status({ state: 'running', revision: 3 });
    const response = {
      success: false,
      error: 'workflow_control_transition_pending',
      workflow_id: 'workflow-1',
      generation: 1,
      state: 'pausing',
      revision: 4,
      can_start: false,
      can_pause: false,
      can_resume: false,
      can_reset: true,
    };

    const snapshot = extractWorkflowControlStatusSnapshot(response);

    expect(snapshot).toMatchObject({
      workflow_id: 'workflow-1',
      state: 'pausing',
      revision: 4,
      can_reset: true,
    });
    expect(mergeWorkflowControlStatus(current, snapshot!)).toBe(snapshot);
    expect(() => assertWorkflowControlMutationSucceeded(response))
      .toThrow('workflow_control_transition_pending');
  });

  it('does not mistake a generic mutation error for a control snapshot', () => {
    expect(extractWorkflowControlStatusSnapshot({
      success: false,
      error: 'control_revision_conflict',
      workflow_id: 'workflow-1',
    })).toBeUndefined();
  });

  it('only retries Reset conflicts when the resynced snapshot still allows Reset', () => {
    const conflict = new Error('control_revision_conflict');
    expect(shouldRetryResetWorkflowAfterConflict(
      conflict,
      status({ state: 'starting', revision: 2, can_reset: true }),
    )).toBe(true);
    expect(shouldRetryResetWorkflowAfterConflict(
      conflict,
      status({ state: 'ready', revision: 3, can_reset: false }),
    )).toBe(false);
    expect(shouldRetryResetWorkflowAfterConflict(
      new Error('workflow_control_transition_pending'),
      status({ state: 'starting', revision: 2, can_reset: true }),
    )).toBe(false);
  });

  it.each([
    ['start', 'running'],
    ['pause', 'paused'],
    ['resume', 'running'],
    ['reset', 'ready'],
  ] satisfies Array<[WorkflowControlMutationAction, WorkflowControlState]>)(
    'accepts an authoritative %s resync only after it reaches %s',
    (action, stableState) => {
      expect(isWorkflowControlMutationConfirmed(action, status({
        state: stableState,
      }))).toBe(true);
    },
  );

  it.each([
    ['start', 'starting'],
    ['pause', 'pausing'],
    ['resume', 'resuming'],
    ['reset', 'resetting'],
  ] satisfies Array<[WorkflowControlMutationAction, WorkflowControlState]>)(
    'does not treat the transitional %s state as confirmed',
    (action, transitionalState) => {
      expect(isWorkflowControlMutationConfirmed(action, status({
        state: transitionalState,
      }))).toBe(false);
    },
  );
});
