import { buildApiUrl } from '../config/api';
import type { AudioRef } from '../components/output/AudioPreview';

/**
 * Mirrors `MEDIA_MAX_UPLOAD_BYTES` in `server/services/media/limits.py`.
 * The server is the authority and returns 413 regardless; checking here
 * only buys a better message before 25 MB goes over the wire.
 */
export const MEDIA_MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export class UploadError extends Error {}

/**
 * POST a file into the workflow's workspace and return the stored reference.
 *
 * This exists because the alternative — base64 inside the node's parameters —
 * is actively harmful at audio sizes. That path JSON-stringifies the blob onto
 * the WebSocket, persists it in the `node_parameters` row, re-broadcasts it to
 * every connected client, and then dies at Temporal's 2 MiB payload limit,
 * burning three retries on the way. A ~1.5 MB clip is already ~2 MB of base64.
 *
 * The reference that comes back is ~400 bytes.
 */
export async function uploadToWorkspace(
  file: File,
  workflowId: string
): Promise<AudioRef> {
  if (!workflowId) {
    throw new UploadError('Save the workflow before uploading a file to it.');
  }
  if (file.size > MEDIA_MAX_UPLOAD_BYTES) {
    throw new UploadError(
      `${file.name} is ${(file.size / (1024 * 1024)).toFixed(1)} MB; the limit is ` +
        `${MEDIA_MAX_UPLOAD_BYTES / (1024 * 1024)} MB.`
    );
  }

  const body = new FormData();
  body.append('file', file, file.name);

  // No explicit Content-Type: the browser must set it so the multipart
  // boundary is generated. Setting it by hand produces a body the server
  // cannot parse.
  const response = await fetch(buildApiUrl(`/api/workspace/${workflowId}/uploads`), {
    method: 'POST',
    body,
    credentials: 'include',
  });

  if (!response.ok) {
    const detail = await response
      .json()
      .then((payload) => payload?.detail)
      .catch(() => null);
    throw new UploadError(detail || `Upload failed (${response.status}).`);
  }

  return response.json();
}
