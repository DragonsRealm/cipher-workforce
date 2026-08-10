import { useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { buildApiUrl } from '../../config/api';
import type { AudioRef } from '../../types/workspaceFiles';

// `AudioRef` / `isAudioRef` live in types/workspaceFiles.ts alongside the
// `WorkspaceFileRef` they narrow. Keeping the value export here made this
// file export both a component and a non-component, which breaks Fast
// Refresh (react-refresh/only-export-components).

const formatDuration = (seconds?: number | null): string | null => {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
};

const formatSize = (bytes?: number): string | null => {
  if (!bytes) return null;
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(0)} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

function AudioClip({ audio }: { audio: AudioRef }) {
  const [failed, setFailed] = useState(false);

  // `url` is path-only so a remote backend can be prefixed; fall back to
  // composing it when an older ref predates the field.
  const src = buildApiUrl(
    audio.url || `/api/workspace/${audio.workflow_id ?? ''}/files/${audio.path}`
  );

  const meta = [formatDuration(audio.duration_seconds), formatSize(audio.size_bytes), audio.format]
    .filter(Boolean)
    .join(' · ');

  return (
    <div className="rounded-md border border-border bg-card p-3">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <span className="truncate font-mono text-xs text-foreground">{audio.filename}</span>
        {meta && <span className="shrink-0 text-xs text-muted-foreground">{meta}</span>}
      </div>

      {failed ? (
        // A 401 surfaces to <audio> as a silent `error` event, so an
        // unauthenticated session would otherwise show a dead player with
        // no explanation.
        <div className="flex items-center gap-2 text-xs text-destructive">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          <span>Could not load this clip. It may have been cleaned up, or the session expired.</span>
        </div>
      ) : (
        <audio
          controls
          preload="metadata"
          src={src}
          onError={() => setFailed(true)}
          className="w-full"
        />
      )}
    </div>
  );
}

/**
 * Renders one or more generated clips.
 *
 * Several clips are not one stream: providers that split long input return
 * standalone files, each with its own container header, so they are shown
 * as separate players rather than concatenated.
 */
export default function AudioPreview({ clips }: { clips: AudioRef[] }) {
  if (!clips.length) return null;

  return (
    <div className="flex flex-col gap-2">
      {clips.length > 1 && (
        <span className="text-xs text-muted-foreground">
          {clips.length} clips — separate files, not parts of one stream.
        </span>
      )}
      {clips.map((clip, index) => (
        <AudioClip key={clip.sha256 || clip.path || index} audio={clip} />
      ))}
    </div>
  );
}
