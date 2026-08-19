/**
 * Wire types + React Query identity for the Canvas node's content board.
 *
 * Declarations only — every decision is made server-side
 * (`server/nodes/tool/canvas/`): items arrive with finished `FileRef`s, the
 * `canvas_updated` broadcast carries identity + revision only, and content
 * bodies flow exclusively through the authorized `canvas_list` handler
 * (files) or the workspace HTTP route (bytes/text).
 */

import type { WorkspaceFileRef } from '../types/workspaceFiles';

export type CanvasItemKind = 'file' | 'url' | 'note';

/** One board item — `server/nodes/tool/canvas/_store.py::_serialize`. */
export interface CanvasItem {
  id: string;
  kind: CanvasItemKind;
  title: string | null;
  /** Serialized FileRef for kind 'file'; null otherwise. */
  ref: WorkspaceFileRef | null;
  /** http(s) address for kind 'url'; null otherwise. */
  url: string | null;
  /** Markdown body for kind 'note' (capped server-side); null otherwise. */
  content: string | null;
  language: string | null;
  source: 'agent' | 'workflow' | string;
  created_at: string | null;
}

/** Response to `canvas_list`. */
export interface CanvasListResponse {
  success: boolean;
  error?: string;
  items?: CanvasItem[];
  revision?: number;
}

/** Identity carried by the `canvas_updated` broadcast (no content). */
export interface CanvasUpdatedIdentity {
  workflow_id?: string | null;
  node_id?: string | null;
  revision?: number;
}

/** One cache entry per workflow + canvas node (todoQuery.ts shape). */
export const canvasBoardQueryKey = (
  workflowId: string | null | undefined,
  nodeId: string,
) => ['canvasBoard', workflowId ?? 'unsaved', nodeId] as const;

/** Prefix for broadcast-driven invalidation of every mounted board. */
export const CANVAS_BOARD_QUERY_PREFIX = ['canvasBoard'] as const;
