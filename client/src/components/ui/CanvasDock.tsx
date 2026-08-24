/**
 * Docked, resizable Canvas sidebar — the persistent right-hand viewing
 * surface (Claude-sidebar posture): stays open while working on the graph,
 * auto-opens when content is pushed (canvas_updated -> notifyPushed), and
 * doubles as the ephemeral click-to-preview surface for workspace files.
 *
 * Two modes (canvasDockStore):
 * - 'node': renders the selected Canvas node's durable board.
 * - 'ephemeral': renders one transient item that lives in no board.
 */

import React, { useMemo } from 'react';
import { Monitor, PanelRightClose, Undo2 } from 'lucide-react';
import type { Node } from 'reactflow';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { usePanelResize } from '../../hooks/usePanelResize';
import {
  useCanvasBoardQuery,
  useCanvasRemove,
} from '../../hooks/useCanvasBoard';
import { resolveNodeDescription } from '../../lib/nodeSpec';
import { useAppStore } from '../../store/useAppStore';
import {
  DOCK_MAX_WIDTH,
  DOCK_MIN_WIDTH,
  useCanvasDockStore,
} from '../../stores/canvasDockStore';
import CanvasContent from '../parameterPanel/canvas/CanvasContent';

interface CanvasDockProps {
  nodes: Node[];
}

const isCanvasNode = (node: Node): boolean =>
  resolveNodeDescription(node.type || '')?.uiHints?.isCanvasPanel === true;

const nodeLabel = (node: Node): string =>
  (node.data?.label as string | undefined) || node.type || node.id;

const CanvasDock: React.FC<CanvasDockProps> = ({ nodes }) => {
  const open = useCanvasDockStore((s) => s.open);
  const widthPx = useCanvasDockStore((s) => s.widthPx);
  const mode = useCanvasDockStore((s) => s.mode);
  const selectedNodeId = useCanvasDockStore((s) => s.selectedNodeId);
  const ephemeralItem = useCanvasDockStore((s) => s.ephemeralItem);
  const followMode = useCanvasDockStore((s) => s.followMode);
  const setFollowMode = useCanvasDockStore((s) => s.setFollowMode);
  const setWidth = useCanvasDockStore((s) => s.setWidth);
  const close = useCanvasDockStore((s) => s.close);
  const selectNode = useCanvasDockStore((s) => s.selectNode);
  const backToNode = useCanvasDockStore((s) => s.backToNode);

  const workflowId = useAppStore((s) => s.currentWorkflow?.id);

  const canvasNodes = useMemo(() => nodes.filter(isCanvasNode), [nodes]);

  // A stale selection (workflow switch, node deleted) falls back to the
  // first Canvas node rather than a dead board.
  const effectiveNodeId = useMemo(() => {
    if (selectedNodeId && canvasNodes.some((n) => n.id === selectedNodeId)) {
      return selectedNodeId;
    }
    return canvasNodes[0]?.id ?? null;
  }, [canvasNodes, selectedNodeId]);

  const showBoard = open && mode === 'node';
  const board = useCanvasBoardQuery(
    showBoard ? workflowId : null,
    showBoard ? effectiveNodeId : null,
  );
  const removeItem = useCanvasRemove(workflowId, effectiveNodeId);

  const resize = usePanelResize({
    axis: 'x',
    cursor: 'ew-resize',
    onMove: (deltaPx, startValue) => {
      // The handle sits on the dock's LEFT edge — dragging left widens.
      // Ceiling is viewport-relative and deliberately generous: the dock may
      // take nearly the whole window; the 160px remainder keeps the handle
      // and a sliver of canvas reachable to drag it back.
      const viewportCap = Math.min(DOCK_MAX_WIDTH, window.innerWidth - 160);
      setWidth(
        Math.min(viewportCap, Math.max(DOCK_MIN_WIDTH, startValue - deltaPx)),
      );
    },
    getStartValue: () => useCanvasDockStore.getState().widthPx,
  });

  return (
    <div
      className={`relative flex h-full shrink-0 overflow-hidden bg-bg-panel ${
        open ? 'border-l border-border-default' : ''
      } ${resize.isResizing ? '[transition:none]' : 'transition-[width] duration-300'}`}
      style={{ width: open ? widthPx : 0 }}
      aria-hidden={!open}
    >
      <div
        className={`h-full w-1.5 shrink-0 cursor-ew-resize transition-colors ${
          resize.isResizing
            ? 'bg-node-agent transition-none'
            : 'bg-border hover:bg-node-agent-soft'
        }`}
        onMouseDown={resize.start}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize canvas panel"
      />

      {/* pointer-events-none while dragging: an embedded iframe (web/PDF
          items) would otherwise swallow the document mousemove and freeze
          the resize the moment the cursor crosses it. */}
      <div
        className={`flex min-h-0 min-w-0 flex-1 flex-col ${
          resize.isResizing ? 'pointer-events-none' : ''
        }`}
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-border-default px-3 py-2">
          <Monitor className="h-4 w-4 shrink-0 text-fg-muted" />
          <span className="shrink-0 text-sm font-semibold text-fg-default">Canvas</span>

          {mode === 'ephemeral' ? (
            <>
              <Badge variant="secondary" className="shrink-0">Preview</Badge>
              <span className="min-w-0 flex-1 truncate text-xs text-fg-muted">
                {ephemeralItem?.title || ephemeralItem?.ref?.filename || ''}
              </span>
              {canvasNodes.length > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={backToNode}
                  className="shrink-0"
                >
                  <Undo2 className="h-3.5 w-3.5" /> Back
                </Button>
              )}
            </>
          ) : canvasNodes.length > 1 ? (
            <div className="min-w-0 flex-1">
              <Select
                value={effectiveNodeId ?? undefined}
                onValueChange={selectNode}
              >
                <SelectTrigger className="h-7 text-xs">
                  <SelectValue placeholder="Select canvas node" />
                </SelectTrigger>
                <SelectContent>
                  {canvasNodes.map((node) => (
                    <SelectItem key={node.id} value={node.id}>
                      {nodeLabel(node)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : (
            <span className="min-w-0 flex-1 truncate text-xs text-fg-muted">
              {canvasNodes[0] ? nodeLabel(canvasNodes[0]) : ''}
            </span>
          )}

          <Button
            variant="ghost"
            size="icon-sm"
            onClick={close}
            aria-label="Close canvas panel"
            className="shrink-0"
          >
            <PanelRightClose className="h-4 w-4" />
          </Button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col p-3">
          {mode === 'ephemeral' && ephemeralItem ? (
            <CanvasContent items={[ephemeralItem]} workflowId={workflowId} />
          ) : !effectiveNodeId ? (
            <div className="flex flex-1 items-center justify-center p-4 text-center text-sm text-fg-muted">
              No Canvas node in this workflow yet — add one from the palette,
              or open a file&apos;s preview and choose &quot;Open in side
              panel&quot;.
            </div>
          ) : (
            <CanvasContent
              items={board.data?.items ?? []}
              workflowId={workflowId}
              followLatestDefault={followMode}
              onFollowLatestChange={setFollowMode}
              onRemove={(itemId) =>
                removeItem.mutate(itemId, {
                  onError: (error) => toast.error(error.message),
                })
              }
              emptyHint="Nothing here yet — agents push content with the canvas tool, or wire a node into the Canvas input."
            />
          )}
        </div>
      </div>
    </div>
  );
};

export default CanvasDock;
