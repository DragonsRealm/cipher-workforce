/**
 * ConditionalEdge - Custom React Flow edge with condition label display
 *
 * Renders edges with visual indicators for conditional branching:
 * - Displays condition label as a badge on the edge
 * - Different styling for conditional vs unconditional edges
 * - Click-to-edit condition support
 */
import React, { useState, useCallback, memo } from 'react';
import {
  EdgeProps,
  getSmoothStepPath,
  EdgeLabelRenderer,
  BaseEdge,
} from 'reactflow';
import { Maximize2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { ConditionalEdgeData, formatConditionLabel } from '../types/EdgeCondition';
import EdgeConditionEditor from './EdgeConditionEditor';

interface ConditionalEdgeProps extends EdgeProps<ConditionalEdgeData> {
  onConditionUpdate?: (
    edgeId: string,
    condition: ConditionalEdgeData['condition'],
    label: string | undefined
  ) => void;
}

const ConditionalEdge: React.FC<ConditionalEdgeProps> = ({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style = {},
  markerEnd,
  data,
  selected,
  onConditionUpdate,
}) => {
  const [isEditorOpen, setIsEditorOpen] = useState(false);

  // borderRadius 0 = right-angle corners, matching the design-handoff
  // `step` contract the default edges follow via `type: 'step'`.
  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX, sourceY, sourcePosition,
    targetX, targetY, targetPosition,
    borderRadius: 0,
  });

  const hasCondition = !!data?.condition;
  const displayLabel = data?.label || (hasCondition && data?.condition ? formatConditionLabel(data.condition) : null);

  const handleLabelClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    setIsEditorOpen(true);
  }, []);

  const handleEditorClose = useCallback(() => setIsEditorOpen(false), []);

  const handleConditionSave = useCallback(
    (condition: ConditionalEdgeData['condition'], label: string | undefined) => {
      if (onConditionUpdate) onConditionUpdate(id, condition, label);
    },
    [id, onConditionUpdate]
  );

  return (
    <>
      {/* BaseEdge stroke is the React Flow API contract — must be inline.
          Values are theme tokens only (base.css --edge-* block): a
          condition-carrying edge highlights in the accent; without a
          condition every property stays undefined so the resting rule
          from canvasAnimations.ts applies. */}
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          ...style,
          stroke: hasCondition ? 'var(--accent)' : undefined,
          strokeWidth: hasCondition ? 'var(--edge-stroke-width-done)' : undefined,
          strokeDasharray: hasCondition ? 'var(--edge-dash)' : undefined,
        }}
      />

      <EdgeLabelRenderer>
        {/* Position via inline left/top — coords are computed by React Flow. */}
        {displayLabel ? (
          <div
            style={{ left: labelX, top: labelY }}
            onClick={handleLabelClick}
            title={`Click to edit condition: ${displayLabel}`}
            className={cn(
              'pointer-events-auto absolute -translate-x-1/2 -translate-y-1/2 max-w-[150px] cursor-pointer truncate rounded-sm border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap transition-all',
              hasCondition
                ? 'border-accent bg-accent/20 text-accent'
                : 'border-border bg-card text-muted-foreground',
              selected && 'shadow-[0_0_6px_var(--node-agent)]'
            )}
          >
            <span className="mr-1 inline-flex">
              <Maximize2 className="h-2.5 w-2.5" />
            </span>
            {displayLabel}
          </div>
        ) : (
          <div
            style={{ left: labelX, top: labelY }}
            onClick={handleLabelClick}
            title="Click to add condition"
            className={cn(
              'pointer-events-auto absolute flex h-5 w-5 -translate-x-1/2 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full border border-dashed border-border bg-card text-sm text-muted-foreground transition-opacity',
              selected ? 'opacity-100' : 'opacity-0'
            )}
          >
            +
          </div>
        )}
      </EdgeLabelRenderer>

      <EdgeConditionEditor
        isOpen={isEditorOpen}
        onClose={handleEditorClose}
        condition={data?.condition}
        label={data?.label}
        onSave={handleConditionSave}
      />
    </>
  );
};

export default memo(ConditionalEdge);
