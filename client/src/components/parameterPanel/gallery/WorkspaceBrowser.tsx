import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  ChevronRight,
  FolderOpen,
  Home,
  LayoutGrid,
  List,
  RefreshCw,
  Search,
} from 'lucide-react';
import { toast } from 'sonner';

import { useWebSocket } from '@/contexts/WebSocketContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { buildApiUrl } from '@/config/api';
import type { ListWorkspaceFilesResponse, WorkspaceEntry } from '@/types/workspaceFiles';

import FileGlyph from './FileGlyph';
import { formatBytes, formatModified, glyphToneFor } from './fileIcons';

/**
 * The workspace browsing engine — listing, breadcrumbs, search, grid/list.
 *
 * Extracted so the gallery panel and the "Choose from workspace" picker are
 * one implementation rather than two. They differ only in what wraps it: the
 * panel stores `path` on its node and allows dragging; the picker keeps `path`
 * in local state and offers no drag surface at all (a non-drag alternative
 * that reintroduced a drag would defeat its purpose).
 *
 * `path` is controlled by the parent for exactly that reason.
 */
interface Props {
  workflowId?: string;
  path: string;
  onPathChange: (next: string) => void;
  /** Directory → navigate, file → whatever the host wants (preview or select). */
  onActivate: (entry: WorkspaceEntry) => void;
  /** Rows carry an HTML5 drag payload. Off inside the picker. */
  draggable?: boolean;
  onDragStart?: (event: React.DragEvent, entry: WorkspaceEntry) => void;
  /** Optional per-row checkbox (the panel's "pin to node output"). */
  selectionSet?: ReadonlySet<string>;
  onTogglePin?: (entry: WorkspaceEntry) => void;
  /** Highlighted row, for the picker's single-select. */
  activePath?: string | null;
  /** Bumping this forces a refetch — the panel uses it after an upload. */
  refreshToken?: number;
}

const POLL_INTERVAL_MS = 5000;
const SEARCH_DEBOUNCE_MS = 250;

const WorkspaceBrowser: React.FC<Props> = ({
  workflowId,
  path,
  onPathChange,
  onActivate,
  draggable = false,
  onDragStart,
  selectionSet,
  onTogglePin,
  activePath = null,
  refreshToken = 0,
}) => {
  const { sendRequest } = useWebSocket();

  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [crumbs, setCrumbs] = useState<Array<{ name: string; path: string }>>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [workspaceExists, setWorkspaceExists] = useState(true);
  const [pathExists, setPathExists] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<'grid' | 'list'>('grid');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');

  const searching = query.trim().length > 0;

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  const refresh = useCallback(async (quiet = false) => {
    if (!workflowId) return;
    if (!quiet) setLoading(true);
    try {
      const term = query.trim();
      // The raw term goes over the wire; the server decides what searching
      // for it means and which directories that spans.
      const response = await sendRequest<ListWorkspaceFilesResponse>('list_workspace_files', {
        workflow_id: workflowId,
        path,
        ...(term ? { search: term } : {}),
        limit: 500,
      });

      if (!response.success) throw new Error(response.error || 'Could not read this workspace');

      setEntries(response.entries ?? []);
      setCrumbs(response.crumbs ?? []);
      setParent(response.parent ?? null);
      setTruncated(response.truncated ?? false);
      // Absent on the glob branch, which never reports them — treat a search
      // as always "inside" a workspace so it can't flash the empty-state.
      setWorkspaceExists(response.workspace_exists ?? true);
      setPathExists(response.path_exists ?? true);
      setError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : 'Could not read this workspace';
      setError(message);
      if (!quiet) toast.error(message);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [path, query, sendRequest, workflowId]);

  useEffect(() => { void refresh(); }, [refresh, refreshToken]);

  useEffect(() => {
    const poll = window.setInterval(() => {
      // A background tab has nothing to show.
      if (document.visibilityState !== 'visible') return;
      void refresh(true);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(poll);
  }, [refresh]);

  const navigate = useCallback((next: string) => {
    setSearch('');
    setQuery('');
    onPathChange(next);
  }, [onPathChange]);

  const visible = useMemo(() => {
    // The backend already filtered a search; this only narrows a plain listing.
    if (searching) return entries;
    const term = search.trim().toLowerCase();
    return term ? entries.filter((entry) => entry.name.toLowerCase().includes(term)) : entries;
  }, [entries, search, searching]);

  const fileCount = visible.filter((entry) => !entry.is_dir).length;

  const emptyMessage = !workflowId
    ? 'Save this workflow to give it a workspace.'
    : !workspaceExists
      ? 'No workspace yet. Run a node that writes a file — a download, a recording, a report — and it will appear here.'
      : !pathExists
        ? 'This folder no longer exists.'
        : searching
          ? 'Nothing in this workspace matches that search.'
          : search.trim()
            ? 'Nothing here matches that filter.'
            : 'This folder is empty.';

  const rowProps = (entry: WorkspaceEntry) => ({
    entry,
    pinned: selectionSet?.has(entry.path) ?? false,
    showPin: !!onTogglePin,
    active: activePath === entry.path,
    showFullPath: searching,
    draggable: draggable && !entry.is_dir && !!entry.ref,
    onOpen: onActivate,
    onTogglePin,
    onDragStart,
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="mb-3 flex shrink-0 flex-wrap items-center gap-2">
        <nav aria-label="Workspace path" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0 px-2"
            onClick={() => navigate('')}
            aria-label="Workspace root"
          >
            <Home className="h-4 w-4" />
          </Button>
          {crumbs.map((crumb, index) => (
            <React.Fragment key={crumb.path}>
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-fg-faint" />
              <Button
                variant="ghost"
                size="sm"
                className={`shrink-0 px-2 ${index === crumbs.length - 1 ? 'text-fg-default' : 'text-fg-muted'}`}
                onClick={() => navigate(crumb.path)}
              >
                {crumb.name}
              </Button>
            </React.Fragment>
          ))}
          {searching && (
            <>
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-fg-faint" />
              <span className="shrink-0 px-2 text-sm text-fg-default">Search results</span>
            </>
          )}
        </nav>

        <div className="relative w-56 shrink-0">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-muted" />
          <Input
            className="pl-9"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search this workspace"
            aria-label="Search this workspace"
          />
        </div>

        <div className="flex shrink-0 items-center rounded border border-border-default">
          <Button
            variant={view === 'grid' ? 'secondary' : 'ghost'}
            size="sm"
            className="rounded-r-none"
            onClick={() => setView('grid')}
            aria-label="Grid view"
            aria-pressed={view === 'grid'}
          >
            <LayoutGrid className="h-4 w-4" />
          </Button>
          <Button
            variant={view === 'list' ? 'secondary' : 'ghost'}
            size="sm"
            className="rounded-l-none"
            onClick={() => setView('list')}
            aria-label="List view"
            aria-pressed={view === 'list'}
          >
            <List className="h-4 w-4" />
          </Button>
        </div>

        <Button
          variant="outline"
          size="sm"
          disabled={loading || !workflowId}
          onClick={() => void refresh()}
          aria-label="Refresh workspace files"
        >
          <RefreshCw className={loading ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
        </Button>
      </div>

      {error && (
        <div className="mb-3 flex shrink-0 items-center gap-2 rounded border border-destructive bg-bg-elevated px-3 py-2 text-xs text-destructive">
          <AlertTriangle className="h-4 w-4 shrink-0" /> {error}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto rounded border border-border-default bg-bg-elevated p-3">
        {visible.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 py-12 text-center">
            <FolderOpen className="h-10 w-10 text-fg-faint" />
            <p className="max-w-sm text-sm text-fg-muted">{emptyMessage}</p>
            {!pathExists && workspaceExists && parent !== null && (
              <Button variant="outline" size="sm" onClick={() => navigate(parent)}>
                Go up
              </Button>
            )}
          </div>
        ) : view === 'grid' ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(8.5rem,1fr))] gap-3">
            {visible.map((entry) => <GridTile key={entry.path} {...rowProps(entry)} />)}
          </div>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead className="text-left text-xs text-fg-muted">
              <tr>
                {['', 'Name', 'Size', 'Modified', 'Type'].map((label, index) => (
                  <th key={label || index} className="border-b border-border-default px-3 py-2 font-medium">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((entry) => <ListRow key={entry.path} {...rowProps(entry)} />)}
            </tbody>
          </table>
        )}
      </div>

      <footer className="mt-2 flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted">
        <span>
          {fileCount} {fileCount === 1 ? 'file' : 'files'}
          {visible.length !== fileCount ? ` · ${visible.length - fileCount} folders` : ''}
        </span>
        {truncated && (
          <span className="flex items-center gap-1 text-warning">
            <AlertTriangle className="h-3 w-3" /> Showing the first {visible.length} — narrow with search.
          </span>
        )}
      </footer>
    </div>
  );
};

interface RowProps {
  entry: WorkspaceEntry;
  pinned: boolean;
  showPin: boolean;
  active: boolean;
  showFullPath: boolean;
  draggable: boolean;
  onOpen: (entry: WorkspaceEntry) => void;
  onTogglePin?: (entry: WorkspaceEntry) => void;
  onDragStart?: (event: React.DragEvent, entry: WorkspaceEntry) => void;
}

/** Enter and Space activate, matching what a `role="button"` promises. */
const activationKeys = (onOpen: () => void) => (event: React.KeyboardEvent) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    onOpen();
  }
};

const GridTile: React.FC<RowProps> = ({
  entry, pinned, showPin, active, showFullPath, draggable, onOpen, onTogglePin, onDragStart,
}) => {
  const [thumbFailed, setThumbFailed] = useState(false);
  const showThumb = entry.preview === 'image' && !!entry.url && !thumbFailed;

  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={active}
      draggable={draggable}
      onDragStart={onDragStart ? (event) => onDragStart(event, entry) : undefined}
      onClick={() => onOpen(entry)}
      onKeyDown={activationKeys(() => onOpen(entry))}
      title={entry.path}
      className={`group relative flex cursor-pointer flex-col gap-2 rounded border bg-bg-panel p-2 text-left transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-focus ${
        active ? 'border-border-focus' : 'border-border-default hover:border-border-strong'
      }`}
    >
      <div className="flex aspect-square items-center justify-center overflow-hidden rounded bg-bg-app">
        {showThumb ? (
          <img
            src={buildApiUrl(entry.url as string)}
            alt={entry.name}
            loading="lazy"
            draggable={false}
            onError={() => setThumbFailed(true)}
            className="h-full w-full object-cover"
          />
        ) : (
          <FileGlyph entry={entry} className={`h-8 w-8 ${glyphToneFor(entry)}`} />
        )}
      </div>

      <div className="min-w-0">
        <div className="truncate text-xs font-medium text-fg-default">{entry.name}</div>
        <div className="truncate text-[11px] text-fg-muted">
          {showFullPath ? entry.path : entry.is_dir ? 'Folder' : formatBytes(entry.size_bytes)}
        </div>
      </div>

      {showPin && !entry.is_dir && onTogglePin && (
        <div
          className={pinned ? 'absolute right-2 top-2' : 'absolute right-2 top-2 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100'}
          onClick={(event) => event.stopPropagation()}
        >
          <Checkbox
            checked={pinned}
            onCheckedChange={() => onTogglePin(entry)}
            aria-label={`Pin ${entry.name} to this node's output`}
          />
        </div>
      )}
    </div>
  );
};

const ListRow: React.FC<RowProps> = ({
  entry, pinned, showPin, active, showFullPath, draggable, onOpen, onTogglePin, onDragStart,
}) => (
  // role/tabIndex/keydown so list view is operable without a pointer — the
  // grid tiles always were, and a view toggle must not change what is reachable.
  <tr
    role="button"
    tabIndex={0}
    aria-pressed={active}
    draggable={draggable}
    onDragStart={onDragStart ? (event) => onDragStart(event, entry) : undefined}
    onClick={() => onOpen(entry)}
    onKeyDown={activationKeys(() => onOpen(entry))}
    title={entry.path}
    className={`cursor-pointer border-b border-border-default hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-focus ${
      active ? 'bg-bg-active' : ''
    }`}
  >
    <td className="px-3 py-2" onClick={(event) => event.stopPropagation()}>
      {showPin && !entry.is_dir && onTogglePin && (
        <Checkbox
          checked={pinned}
          onCheckedChange={() => onTogglePin(entry)}
          aria-label={`Pin ${entry.name} to this node's output`}
        />
      )}
    </td>
    <td className="max-w-0 px-3 py-2">
      <div className="flex items-center gap-2">
        <FileGlyph entry={entry} className={`h-4 w-4 shrink-0 ${glyphToneFor(entry)}`} />
        <span className="truncate text-fg-default">{showFullPath ? entry.path : entry.name}</span>
      </div>
    </td>
    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-fg-muted">
      {formatBytes(entry.size_bytes, entry.is_dir)}
    </td>
    <td className="whitespace-nowrap px-3 py-2 text-xs text-fg-muted">
      {formatModified(entry.modified_at)}
    </td>
    <td className="max-w-[14rem] truncate px-3 py-2 text-xs text-fg-muted">
      {entry.is_dir ? 'Folder' : entry.mime_type || 'unknown'}
    </td>
  </tr>
);

export default WorkspaceBrowser;
