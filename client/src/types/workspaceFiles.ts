/**
 * Wire types for the workspace file explorer (the `gallery` node).
 *
 * Declarations only. Every decision these describe is made server-side:
 *   - `WorkspaceEntry` <- `server/nodes/filesystem/gallery/_service.py::_to_row`
 *   - `WorkspaceFileRef` <- `server/services/media/refs.py::FileRef`
 *
 * In particular the client does not build a `FileRef` (the row arrives with
 * a finished one in `ref`), does not decide what is previewable (`preview`
 * comes from the same function that sets the route's Content-Disposition),
 * and does not translate a search term into a glob. Each of those would be
 * a second copy of a server rule, and copies drift.
 */

/** What the workspace route will let a browser render in place. */
export type PreviewKind = 'image' | 'audio' | 'video' | 'pdf' | 'none';

/** One entry in a directory listing. `path` is always workspace-relative. */
export interface WorkspaceEntry {
  name: string;
  /**
   * POSIX, workspace-relative, no leading slash ('audio/greeting.wav').
   * Never OS-native: a leading slash reads as absolute on POSIX but not on
   * Windows, so a native path would resolve on a Windows dev box and fail
   * in production.
   */
  path: string;
  is_dir: boolean;
  /** 0 for directories, and for files whose stat() failed. */
  size_bytes: number;
  /** ISO 8601, or null when stat() failed. Advisory — not a cache key. */
  modified_at: string | null;
  /** null for directories. */
  mime_type: string | null;
  /** Path-only (no scheme/host); null for directories. Prefix via buildApiUrl. */
  url: string | null;
  /** Server's verdict on inline rendering. 'none' for directories. */
  preview: PreviewKind;
  /** Finished `FileRef`, ready to drop into a parameter. null for directories. */
  ref: WorkspaceFileRef | null;
}

/** Every `kind` the backend's `FileKind` literal allows. */
export const FILE_REF_KINDS = ['file', 'audio', 'image', 'video', 'document'] as const;
export type FileRefKind = (typeof FILE_REF_KINDS)[number];

/** A serialized `FileRef`. `AudioRef` is this plus probed audio fields. */
export interface WorkspaceFileRef {
  kind: FileRefKind;
  path: string;
  workflow_id?: string | null;
  filename: string;
  mime_type?: string;
  size_bytes?: number;
  modified_at?: string | null;
  sha256?: string | null;
  url?: string | null;
}

const KIND_SET: ReadonlySet<string> = new Set(FILE_REF_KINDS);

/**
 * Structural check for "this parameter value is a file reference".
 *
 * Deliberately accepts every `FileKind`, not just 'audio': the gallery emits
 * `kind: "file"` even for a .wav, because `kind: "audio"` asserts the
 * container was probed by `inspect_audio` and a fabricated duration would
 * mis-bill a per-second provider downstream.
 */
export const isWorkspaceFileRef = (value: unknown): value is WorkspaceFileRef =>
  !!value &&
  typeof value === 'object' &&
  KIND_SET.has((value as WorkspaceFileRef).kind) &&
  typeof (value as WorkspaceFileRef).path === 'string' &&
  typeof (value as WorkspaceFileRef).filename === 'string';

/**
 * Drag discriminator.
 *
 * Deliberately NOT the existing 'nodeOutput' — that handler replaces the
 * target value unconditionally, so reusing it would mean dropping a file
 * into a half-written prompt destroys the prompt.
 */
export const WORKSPACE_FILE_DRAG_TYPE = 'workspaceFile';

export interface WorkspaceFileDragPayload {
  type: typeof WORKSPACE_FILE_DRAG_TYPE;
  /** For text params — appended as plain text. */
  path: string;
  /** For `file` params — assigned whole. */
  ref: WorkspaceFileRef;
}

/**
 * Response to `list_workspace_files`.
 *
 * The directory and glob branches return *different* key sets: only the
 * directory branch carries `parent` / `workspace_exists` / `path_exists`,
 * and only the glob branch carries `pattern`. Hence the wide optionality.
 */
export interface ListWorkspaceFilesResponse {
  success: boolean;
  error?: string;
  workflow_id?: string;
  path?: string;
  parent?: string | null;
  /** Breadcrumb trail for `path`, root excluded. Directory branch only. */
  crumbs?: Array<{ name: string; path: string }>;
  pattern?: string;
  entries?: WorkspaceEntry[];
  /** Equals `entries.length` — the *capped* count, never the untruncated total. */
  count?: number;
  truncated?: boolean;
  workspace_exists?: boolean;
  path_exists?: boolean;
}
