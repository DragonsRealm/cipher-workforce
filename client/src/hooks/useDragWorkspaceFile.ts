import { useCallback } from 'react';

import {
  WORKSPACE_FILE_DRAG_TYPE,
  type WorkspaceEntry,
  type WorkspaceFileDragPayload,
} from '@/types/workspaceFiles';

interface DragWorkspaceFileHookReturn {
  handleFileDragStart: (event: React.DragEvent, entry: WorkspaceEntry) => void;
}

/**
 * Produces the drag payload for a workspace file.
 *
 * Mirrors `useDragVariable`: both MIME types are set — `application/json`
 * carries the structured payload, `text/plain` carries a human-readable
 * fallback so the path still lands when dropped on a plain <textarea> or an
 * editor outside this app.
 *
 * The reference is not assembled here — the row arrives with a finished one
 * from the server, which owns the `FileRef` model. Directories have none, so
 * a directory is simply not draggable (and appending a bare folder path into
 * a prompt was never what was meant anyway).
 */
export function useDragWorkspaceFile(): DragWorkspaceFileHookReturn {
  const handleFileDragStart = useCallback((event: React.DragEvent, entry: WorkspaceEntry) => {
    if (entry.is_dir || !entry.ref) {
      event.preventDefault();
      return;
    }

    const payload: WorkspaceFileDragPayload = {
      type: WORKSPACE_FILE_DRAG_TYPE,
      path: entry.path,
      ref: entry.ref,
    };

    event.dataTransfer.setData('application/json', JSON.stringify(payload));
    event.dataTransfer.setData('text/plain', entry.path);
    event.dataTransfer.effectAllowed = 'copy';
  }, []);

  return { handleFileDragStart };
}

export default useDragWorkspaceFile;
