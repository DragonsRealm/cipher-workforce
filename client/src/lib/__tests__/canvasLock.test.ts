import { describe, expect, it } from 'vitest';
import { deriveCanvasLock } from '../canvasLock';

const legacyUnlocked = { locked: false, workflow_id: null };

describe('deriveCanvasLock', () => {
  it('renders the server-owned can_edit capability verbatim', () => {
    expect(
      deriveCanvasLock({
        control: { state: 'running', can_edit: false },
        legacyLock: legacyUnlocked,
        workflowId: 'wf-1',
      }).locked,
    ).toBe(true);

    // paused is editable — controlled generations execute their
    // immutable admitted snapshot, so the server grants can_edit.
    expect(
      deriveCanvasLock({
        control: { state: 'paused', can_edit: true },
        legacyLock: legacyUnlocked,
        workflowId: 'wf-1',
      }).locked,
    ).toBe(false);
  });

  it('never re-derives the rule from state strings alone', () => {
    // A running state WITH can_edit granted stays editable — the FE
    // must not override the backend's decision.
    expect(
      deriveCanvasLock({
        control: { state: 'running', can_edit: true },
        legacyLock: legacyUnlocked,
        workflowId: 'wf-1',
      }).locked,
    ).toBe(false);
  });

  it('gives transitional states a distinct reason', () => {
    const transition = deriveCanvasLock({
      control: { state: 'pausing', can_edit: false },
      legacyLock: legacyUnlocked,
      workflowId: 'wf-1',
    });
    expect(transition.locked).toBe(true);
    expect(transition.reason).toContain('transition');

    const running = deriveCanvasLock({
      control: { state: 'running', can_edit: false },
      legacyLock: legacyUnlocked,
      workflowId: 'wf-1',
    });
    expect(running.reason).toContain('pause it to edit');
  });

  it('lets the server grant editing while paused even though the deployment lock is held', () => {
    // Regression: pause keeps the deployment armed, so the legacy
    // broadcaster lock stays held for the workflow's whole armed
    // lifetime. The server's paused-is-editable grant must win — the
    // legacy lock is a fallback for ungoverned workflows, never an
    // override of the control plane's capability.
    const legacyLocked = { locked: true, workflow_id: 'wf-1' };
    expect(
      deriveCanvasLock({
        control: { state: 'paused', can_edit: true },
        legacyLock: legacyLocked,
        workflowId: 'wf-1',
      }),
    ).toEqual({ locked: false, reason: null });
  });

  it('falls back to the legacy broadcaster lock only when no generation governs the workflow', () => {
    // `never_started` is the control plane's explicit "no generation
    // exists" answer (serialize_control(None) and the FE placeholder
    // both use it) — legacy deployments never create a control row, so
    // the broadcaster lock decides for them.
    const legacyLocked = { locked: true, workflow_id: 'wf-1' };
    expect(
      deriveCanvasLock({
        control: { state: 'never_started', can_edit: true },
        legacyLock: legacyLocked,
        workflowId: 'wf-1',
      }).locked,
    ).toBe(true);

    expect(
      deriveCanvasLock({
        control: null,
        legacyLock: legacyLocked,
        workflowId: 'wf-1',
      }).locked,
    ).toBe(true);

    // A lock held by ANOTHER workflow never locks this canvas.
    expect(
      deriveCanvasLock({
        control: { state: 'never_started', can_edit: true },
        legacyLock: legacyLocked,
        workflowId: 'wf-2',
      }).locked,
    ).toBe(false);
  });

  it('is unlocked with no control status and no legacy lock', () => {
    const result = deriveCanvasLock({
      control: null,
      legacyLock: legacyUnlocked,
      workflowId: 'wf-1',
    });
    expect(result).toEqual({ locked: false, reason: null });
  });
});
