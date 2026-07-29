/**
 * Canvas edit-lock presentation helper.
 *
 * The DECISION of whether a workflow's canvas may be edited is
 * server-owned: `serialize_control` (backend) emits the `can_edit`
 * capability alongside `can_start` / `can_pause` / …, and the frontend
 * renders it — it never re-derives the rule from state strings. This
 * module only maps the capability (plus the legacy broadcaster lock for
 * deployments driven outside the control plane) to a boolean + a
 * human-readable reason, which are pure UI concerns.
 */

export interface CanvasLockControlInput {
  state?: string | null;
  can_edit?: boolean;
}

export interface CanvasLockInput {
  /** Durable control-plane status for the workflow (server-normalized). */
  control: CanvasLockControlInput | null | undefined;
  /** Legacy broadcaster lock singleton (`workflowLock` from WebSocketContext). */
  legacyLock: { locked: boolean; workflow_id: string | null } | null | undefined;
  /** The workflow currently open on the canvas. */
  workflowId: string | null | undefined;
}

export interface CanvasLockResult {
  locked: boolean;
  /** Human-readable cause for the indicator / blocked-edit toast. */
  reason: string | null;
}

const TRANSITION_STATES = new Set(['pausing', 'resuming', 'resetting', 'starting']);

export function deriveCanvasLock({
  control,
  legacyLock,
  workflowId,
}: CanvasLockInput): CanvasLockResult {
  // Once the control plane governs the workflow (a generation exists),
  // its `can_edit` capability is rendered verbatim — granted or denied —
  // and the legacy broadcaster lock is not consulted. Control-plane
  // deployments hold the broadcaster lock for their whole armed lifetime
  // (paused included), so letting it override would deny the server's
  // paused-is-editable grant. `never_started` is the control plane's
  // explicit "no generation governs this workflow" answer, so it falls
  // through to the legacy fallback below.
  const governed =
    !!control && control.state != null && control.state !== 'never_started';
  if (governed) {
    if (control.can_edit === false) {
      const state = control.state ?? null;
      return {
        locked: true,
        reason:
          state && TRANSITION_STATES.has(state) && state !== 'running'
            ? 'Workflow is changing state — wait for the transition to finish'
            : 'Workflow is running — pause it to edit the canvas',
      };
    }
    return { locked: false, reason: null };
  }
  // Legacy fallback: deployments driven outside the control plane never
  // create a control row, so the broadcaster lock decides for them.
  if (
    legacyLock?.locked &&
    !!workflowId &&
    legacyLock.workflow_id === workflowId
  ) {
    return { locked: true, reason: 'Workflow is deployed — stop it to edit the canvas' };
  }
  return { locked: false, reason: null };
}
