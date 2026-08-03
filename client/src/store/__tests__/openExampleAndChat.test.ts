/**
 * Tests for useAppStore.openExampleAndChat + requestChatFocus.
 *
 * Locks the onboarding handoff contract:
 *   - case-insensitive name lookup against workflowApi.getAllWorkflows()
 *   - hit: loads the workflow (workflowApi.getWorkflow), reveals the console
 *     panel, bumps chatFocusRequest, returns the loaded workflow
 *   - miss: toast.info, returns null, state untouched
 *   - load failure (getWorkflow -> null): toast.error, returns null,
 *     state untouched
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// --- mocks (hoisted; closures dereference the spies at call time) -----------

const getAllWorkflowsMock = vi.fn();
const getWorkflowMock = vi.fn();
const saveWorkflowMock = vi.fn();
const deleteWorkflowMock = vi.fn();

vi.mock('../../services/workflowApi', () => ({
  workflowApi: {
    getAllWorkflows: (...args: unknown[]) => getAllWorkflowsMock(...args),
    getWorkflow: (...args: unknown[]) => getWorkflowMock(...args),
    saveWorkflow: (...args: unknown[]) => saveWorkflowMock(...args),
    deleteWorkflow: (...args: unknown[]) => deleteWorkflowMock(...args),
  },
}));

const toastInfoMock = vi.fn();
const toastErrorMock = vi.fn();
vi.mock('sonner', () => ({
  toast: {
    info: (...args: unknown[]) => toastInfoMock(...args),
    error: (...args: unknown[]) => toastErrorMock(...args),
    success: vi.fn(),
    message: vi.fn(),
    warning: vi.fn(),
  },
}));

import { useAppStore } from '../useAppStore';

// --- fixtures ----------------------------------------------------------------

const summary = (id: string, name: string) => ({
  id,
  name,
  slug: `${name.replace(/\s+/g, '_')}_1`,
  nodeCount: 1,
  createdAt: '2026-01-01T00:00:00Z',
  lastModified: '2026-01-02T00:00:00Z',
});

// Shape of workflowApi.getWorkflow's resolved value (services/workflowApi.ts
// WorkflowData): { id, name, slug, data: { nodes, edges }, createdAt,
// lastModified }.
const workflowRecord = (id: string, name: string) => ({
  id,
  name,
  slug: `${name.replace(/\s+/g, '_')}_1`,
  data: { nodes: [], edges: [] },
  createdAt: '2026-01-01T00:00:00Z',
  lastModified: '2026-01-02T00:00:00Z',
});

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({
    currentWorkflow: null,
    hasUnsavedChanges: false,
    selectedNode: null,
    consolePanelVisible: false,
    chatFocusRequest: 0,
  });
});

// ---------------------------------------------------------------------------

describe('useAppStore.requestChatFocus', () => {
  it('increments the monotonic chatFocusRequest counter', () => {
    expect(useAppStore.getState().chatFocusRequest).toBe(0);
    useAppStore.getState().requestChatFocus();
    expect(useAppStore.getState().chatFocusRequest).toBe(1);
    useAppStore.getState().requestChatFocus();
    expect(useAppStore.getState().chatFocusRequest).toBe(2);
  });
});

describe('useAppStore.openExampleAndChat', () => {
  it('finds the workflow case-insensitively, loads it, opens the console, and requests chat focus', async () => {
    getAllWorkflowsMock.mockResolvedValue([
      summary('wf-other', 'Other Flow'),
      summary('wf-1', 'AI Assistant'),
    ]);
    getWorkflowMock.mockResolvedValue(workflowRecord('wf-1', 'AI Assistant'));

    const result = await useAppStore.getState().openExampleAndChat('ai assistant');

    // Load path went through workflowApi.getWorkflow with the matched id.
    expect(getWorkflowMock).toHaveBeenCalledTimes(1);
    expect(getWorkflowMock).toHaveBeenCalledWith('wf-1');

    // Returns the loaded workflow.
    expect(result).not.toBeNull();
    expect(result!.id).toBe('wf-1');
    expect(result!.name).toBe('AI Assistant');

    const state = useAppStore.getState();
    expect(state.currentWorkflow?.id).toBe('wf-1');
    expect(state.currentWorkflow?.name).toBe('AI Assistant');
    expect(state.hasUnsavedChanges).toBe(false);
    expect(state.consolePanelVisible).toBe(true);
    expect(state.chatFocusRequest).toBe(1);

    expect(toastInfoMock).not.toHaveBeenCalled();
    expect(toastErrorMock).not.toHaveBeenCalled();
  });

  it('toasts info and leaves state untouched when no workflow matches the name', async () => {
    getAllWorkflowsMock.mockResolvedValue([summary('wf-other', 'Other Flow')]);

    const result = await useAppStore.getState().openExampleAndChat('AI Assistant');

    expect(result).toBeNull();
    expect(toastInfoMock).toHaveBeenCalledTimes(1);
    expect(getWorkflowMock).not.toHaveBeenCalled();

    const state = useAppStore.getState();
    expect(state.currentWorkflow).toBeNull();
    expect(state.consolePanelVisible).toBe(false);
    expect(state.chatFocusRequest).toBe(0);
    expect(toastErrorMock).not.toHaveBeenCalled();
  });

  it('toasts error and returns null when the workflow fails to load', async () => {
    getAllWorkflowsMock.mockResolvedValue([summary('wf-1', 'AI Assistant')]);
    getWorkflowMock.mockResolvedValue(null); // loadWorkflow leaves the store untouched

    const result = await useAppStore.getState().openExampleAndChat('AI Assistant');

    expect(result).toBeNull();
    expect(getWorkflowMock).toHaveBeenCalledWith('wf-1');
    expect(toastErrorMock).toHaveBeenCalledTimes(1);
    expect(toastInfoMock).not.toHaveBeenCalled();

    const state = useAppStore.getState();
    expect(state.currentWorkflow).toBeNull();
    expect(state.consolePanelVisible).toBe(false);
    expect(state.chatFocusRequest).toBe(0);
  });

  it('does not treat a different open workflow as the load target (id verification)', async () => {
    // A previously open workflow stays current when the load fails; the id
    // check must compare against the TARGET id, not merely non-null.
    useAppStore.setState({
      currentWorkflow: {
        id: 'wf-prev',
        name: 'Previous',
        slug: '',
        nodes: [],
        edges: [],
        createdAt: new Date(),
        lastModified: new Date(),
      },
    });
    getAllWorkflowsMock.mockResolvedValue([summary('wf-1', 'AI Assistant')]);
    getWorkflowMock.mockResolvedValue(null);

    const result = await useAppStore.getState().openExampleAndChat('AI Assistant');

    expect(result).toBeNull();
    expect(toastErrorMock).toHaveBeenCalledTimes(1);
    const state = useAppStore.getState();
    expect(state.currentWorkflow?.id).toBe('wf-prev');
    expect(state.consolePanelVisible).toBe(false);
    expect(state.chatFocusRequest).toBe(0);
  });
});

describe('useAppStore authoritative normalized graph handoff', () => {
  it('replaces the edit buffer with save response data and reconciles selection', async () => {
    const draftAgent = {
      id: 'draft-agent',
      type: 'aiAgent',
      data: { label: 'Agent' },
      position: { x: 100, y: 100 },
    };
    useAppStore.setState({
      currentWorkflow: {
        id: 'wf-1',
        name: 'Context flow',
        slug: 'Context_flow_1',
        nodes: [draftAgent],
        edges: [],
        createdAt: new Date(),
        lastModified: new Date(),
      },
      selectedNode: draftAgent,
      hasUnsavedChanges: true,
    });
    const normalizedAgent = { ...draftAgent, id: 'wf-1:aiAgent:1' };
    const contextNode = {
      id: 'wf-1:context:1',
      type: 'context',
      data: { systemManaged: true },
      position: { x: -120, y: 100 },
    };
    const contextEdge = {
      id: 'wf-1:edge:context:1',
      source: contextNode.id,
      target: normalizedAgent.id,
      sourceHandle: 'output-context',
      targetHandle: 'input-context',
    };
    saveWorkflowMock.mockResolvedValue({
      success: true,
      id: 'wf-1',
      slug: 'Context_flow_1',
      nodeIdAliases: { 'draft-agent': normalizedAgent.id },
      data: {
        graphVersion: 2,
        nodes: [normalizedAgent, contextNode],
        edges: [contextEdge],
      },
    });

    await useAppStore.getState().saveWorkflow();

    const state = useAppStore.getState();
    expect(state.currentWorkflow?.nodes).toEqual([normalizedAgent, contextNode]);
    expect(state.currentWorkflow?.edges).toEqual([contextEdge]);
    expect(state.selectedNode).toEqual(normalizedAgent);
    expect(state.hasUnsavedChanges).toBe(false);
  });

  it('uses aliases only as a compatibility fallback when save data is absent', async () => {
    const draftAgent = {
      id: 'draft-agent',
      type: 'aiAgent',
      data: {},
      position: { x: 0, y: 0 },
    };
    useAppStore.setState({
      currentWorkflow: {
        id: 'wf-1',
        name: 'Legacy save',
        slug: '',
        nodes: [draftAgent],
        edges: [],
        createdAt: new Date(),
        lastModified: new Date(),
      },
      selectedNode: draftAgent,
    });
    saveWorkflowMock.mockResolvedValue({
      success: true,
      id: 'wf-1',
      nodeIdAliases: { 'draft-agent': 'wf-1:aiAgent:1' },
    });

    await useAppStore.getState().saveWorkflow();

    expect(useAppStore.getState().currentWorkflow?.nodes[0].id).toBe(
      'wf-1:aiAgent:1',
    );
    expect(useAppStore.getState().selectedNode?.id).toBe('wf-1:aiAgent:1');
  });
});
