/**
 * Parameter-panel host for the Canvas node (uiHints.isCanvasPanel).
 *
 * Gallery-pattern full-height panel: header + the shared CanvasContent.
 * Server state rides useCanvasBoardQuery; freshness is broadcast-driven
 * (`canvas_updated` -> ['canvasBoard'] invalidation in WebSocketContext).
 */

import React from 'react';
import { Eraser, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  useCanvasBoardQuery,
  useCanvasClear,
  useCanvasRemove,
} from '../../hooks/useCanvasBoard';
import CanvasContent from './canvas/CanvasContent';

interface Props {
  nodeId: string;
  workflowId?: string | null;
}

const CanvasPanel: React.FC<Props> = ({ nodeId, workflowId }) => {
  const board = useCanvasBoardQuery(workflowId, nodeId);
  const removeItem = useCanvasRemove(workflowId, nodeId);
  const clearBoard = useCanvasClear(workflowId, nodeId);

  const items = board.data?.items ?? [];

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-4">
      <header className="mb-3 flex shrink-0 items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-fg-default">Canvas</h2>
          <p className="text-sm text-fg-muted">
            Content pushed here by agents and workflow runs. Items are
            references — removing one never deletes the underlying file.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {items.length > 0 && <Badge variant="secondary">{items.length}</Badge>}
          <Button
            variant="ghost"
            size="sm"
            disabled={items.length === 0 || clearBoard.isPending}
            onClick={() =>
              clearBoard.mutate(undefined, {
                onError: (error) => toast.error(error.message),
              })
            }
          >
            <Eraser className="h-4 w-4" /> Clear
          </Button>
        </div>
      </header>

      {!workflowId ? (
        <p className="py-8 text-center text-sm text-fg-muted">
          Save this workflow to start collecting Canvas content.
        </p>
      ) : board.isLoading ? (
        <div className="flex flex-1 items-center justify-center">
          <RefreshCw className="h-4 w-4 animate-spin text-fg-muted" />
        </div>
      ) : board.isError ? (
        <p className="py-8 text-center text-sm text-fg-muted">
          {board.error.message}
        </p>
      ) : (
        <CanvasContent
          items={items}
          workflowId={workflowId}
          onRemove={(itemId) =>
            removeItem.mutate(itemId, {
              onError: (error) => toast.error(error.message),
            })
          }
        />
      )}
    </div>
  );
};

export default CanvasPanel;
