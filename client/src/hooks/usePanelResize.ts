/**
 * Drag-resize hook for docked panels.
 *
 * Extracted verbatim from ConsolePanel (its private hook since the hybrid
 * layout) so the Canvas dock and any future docked pane share one
 * implementation: document-level mousemove/mouseup listeners for the drag
 * lifetime, with body cursor + userSelect managed so the drag never selects
 * text or flickers the cursor across child elements.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export function usePanelResize(opts: {
  axis: 'y' | 'x';
  cursor: 'ns-resize' | 'ew-resize';
  onMove: (deltaPx: number, startValue: number) => void;
  getStartValue: () => number;
}) {
  const [isResizing, setIsResizing] = useState(false);
  const startCoordRef = useRef(0);
  const startValueRef = useRef(0);
  const { axis, cursor, onMove, getStartValue } = opts;

  const start = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    startCoordRef.current = axis === 'y' ? e.clientY : e.clientX;
    startValueRef.current = getStartValue();
    setIsResizing(true);
  }, [axis, getStartValue]);

  useEffect(() => {
    if (!isResizing) return;
    const handleMove = (e: MouseEvent) => {
      const cur = axis === 'y' ? e.clientY : e.clientX;
      onMove(cur - startCoordRef.current, startValueRef.current);
    };
    const handleUp = () => setIsResizing(false);
    document.addEventListener('mousemove', handleMove);
    document.addEventListener('mouseup', handleUp);
    document.body.style.cursor = cursor;
    document.body.style.userSelect = 'none';
    return () => {
      document.removeEventListener('mousemove', handleMove);
      document.removeEventListener('mouseup', handleUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResizing, axis, cursor, onMove]);

  return { start, isResizing };
}
