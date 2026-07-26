/**
 * The single write path for assigning a workspace file to a parameter.
 *
 * Two entry points reach this: the drag drop handler in `ParameterRenderer`,
 * and the "Choose from workspace" picker. They must not diverge — WCAG 2.2
 * SC 2.5.7 requires the non-drag alternative to operate *the same function*,
 * not merely a similar one, so the append-vs-replace rule lives here once and
 * both callers invoke it.
 *
 * Pure and React-free, so the parity between the two paths is unit-testable
 * rather than a claim.
 */

import {
  WORKSPACE_FILE_DRAG_TYPE,
  type WorkspaceEntry,
  type WorkspaceFileDragPayload,
} from '@/types/workspaceFiles';

/**
 * Lift a listing row into the payload both paths carry.
 *
 * Returns `null` for anything that cannot be assigned — a directory, or a row
 * the server declined to give a `ref`. Callers treat null as "not assignable"
 * (the drag hook cancels the drag; the picker disables its confirm button).
 */
export const buildWorkspaceFilePayload = (
  entry: WorkspaceEntry,
): WorkspaceFileDragPayload | null => {
  if (entry.is_dir || !entry.ref) return null;
  return {
    type: WORKSPACE_FILE_DRAG_TYPE,
    path: entry.path,
    ref: entry.ref,
  };
};

interface AssignTarget {
  /** The `INodeProperties`/`NodeParameter` type of the destination field. */
  parameterType?: string;
  /** Whatever the field currently holds — may be an object for `file` params. */
  currentValue: unknown;
  onChange: (value: unknown) => void;
}

/**
 * Write a chosen file into a parameter.
 *
 * A `file` parameter holds exactly one reference, so it is REPLACED. Every
 * other parameter is text and is APPENDED to: dropping — or picking — a file
 * while a prompt is half-written must leave the prompt intact. That asymmetry
 * is the reason this app does not reuse the pre-existing `nodeOutput` drag
 * discriminator, whose handler replaces unconditionally.
 */
export const assignWorkspaceFile = (
  payload: WorkspaceFileDragPayload,
  { parameterType, currentValue, onChange }: AssignTarget,
): void => {
  if (parameterType === 'file' && payload.ref) {
    onChange(payload.ref);
    return;
  }

  // Guarded: a `file` param's value is an object, and `.endsWith` on one throws.
  const existing = typeof currentValue === 'string' ? currentValue : '';
  const needsSpace = existing.length > 0 && !existing.endsWith(' ');
  onChange(existing + (needsSpace ? ' ' : '') + payload.path);
};
