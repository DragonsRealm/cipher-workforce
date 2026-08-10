import React, { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import JsonView from '@uiw/react-json-view';
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Download,
  GitFork,
  Loader2,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { toast } from 'sonner';

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useWebSocket } from '@/contexts/WebSocketContext';

const PAGE_SIZE = 50;

export interface AgentContextEventView {
  sequence: number;
  event_type: string;
  operation_id?: string;
  provider?: string;
  payload_ref?: string;
  payload_hash?: string;
  message_wire_v2?: unknown;
  payload?: unknown;
}

interface ContextCheckpointView {
  provider?: string;
  strategy?: string;
  covers_through_sequence?: number;
  active_token_count?: number;
  source_revision?: number;
  source_hash?: string;
}

interface ContextSnapshot {
  thread_id?: string;
  epoch?: number;
  revision?: number;
  provider?: string;
  fidelity?: string;
  resumable?: boolean;
  active_token_count?: number;
  context_window?: number;
  pressure_ratio?: number;
  provider_binding_status?: string;
  checkpoints?: ContextCheckpointView[];
  events?: AgentContextEventView[];
  active_replay?: unknown;
  next_cursor?: string | null;
}

interface ContextResponse extends Partial<ContextSnapshot> {
  success?: boolean;
  context?: ContextSnapshot;
  error?: string;
}

interface ContextPanelProps {
  nodeId: string;
  workflowId?: string;
}

const contextQueryKey = (
  workflowId: string | undefined,
  nodeId: string,
  cursor: string | null,
  view: string,
) => ['agentContext', workflowId ?? '', nodeId, cursor ?? '', view] as const;

function responseSnapshot(response: ContextResponse | undefined): ContextSnapshot {
  if (!response) return {};
  return response.context ?? response;
}

function exportDownload(response: unknown, nodeId: string): void {
  if (!response || typeof window === 'undefined') return;
  const envelope = response as {
    filename?: string;
    content?: string;
    export?: unknown;
  };
  const body =
    typeof envelope.content === 'string'
      ? envelope.content
      : JSON.stringify(envelope.export ?? response, null, 2);
  const blob = new Blob([body], { type: 'application/json' });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = envelope.filename || `context-${nodeId}.json`;
  anchor.click();
  URL.revokeObjectURL(href);
}

/** Authorized, query-backed Context inspector.
 *
 * Raw journal/replay payloads are fetched only while this panel is open.
 * It never reads transcript data from workflow params, node status, or
 * websocket broadcasts.
 */
const ContextPanel: React.FC<ContextPanelProps> = ({ nodeId, workflowId }) => {
  const { sendRequest } = useWebSocket();
  const queryClient = useQueryClient();
  const [view, setView] = useState<'journal' | 'active'>('journal');
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);

  const queryKey = contextQueryKey(workflowId, nodeId, cursor, view);
  const contextQuery = useQuery<ContextResponse, Error>({
    queryKey,
    queryFn: () =>
      sendRequest<ContextResponse>('get_agent_context', {
        workflow_id: workflowId,
        context_node_id: nodeId,
        cursor,
        limit: PAGE_SIZE,
        view,
      }),
    enabled: !!workflowId && !!nodeId,
  });
  const snapshot = responseSnapshot(contextQuery.data);

  const invalidateContext = async () => {
    await queryClient.invalidateQueries({
      queryKey: ['agentContext', workflowId ?? '', nodeId],
    });
  };
  const clearMutation = useMutation({
    mutationFn: () =>
      sendRequest('clear_agent_context', {
        workflow_id: workflowId,
        context_node_id: nodeId,
      }),
    onSuccess: async () => {
      setCursor(null);
      setCursorHistory([]);
      await invalidateContext();
      toast.success('Context epoch cleared');
    },
    onError: () => toast.error('Failed to clear Context'),
  });
  const forkMutation = useMutation({
    mutationFn: () =>
      // No `provider`: the backend derives the binding from the thread.
      // Echoing it from this snapshot would let a stale render fork the
      // epoch onto a provider the thread has already moved off.
      sendRequest('fork_agent_context', {
        workflow_id: workflowId,
        context_node_id: nodeId,
      }),
    onSuccess: async () => {
      await invalidateContext();
      toast.success('Provider context forked');
    },
    onError: () => toast.error('Failed to fork Context'),
  });
  const exportMutation = useMutation({
    mutationFn: () =>
      sendRequest('export_agent_context', {
        workflow_id: workflowId,
        context_node_id: nodeId,
        thread_id: snapshot.thread_id,
        epoch: snapshot.epoch,
      }),
    onSuccess: (response) => {
      exportDownload(response, nodeId);
      toast.success('Context export prepared');
    },
    onError: () => toast.error('Failed to export Context'),
  });

  // The server owns the pressure rule (it divides by the context window it
  // resolved, and sends null when it has none). This only scales the ratio for
  // <Progress>; re-deriving it from active_token_count / context_window here
  // would be a second copy of that rule, free to drift.
  const pressurePercent = useMemo<number | null>(
    () =>
      typeof snapshot.pressure_ratio === 'number'
        ? Math.max(0, Math.min(100, snapshot.pressure_ratio * 100))
        : null,
    [snapshot.pressure_ratio],
  );

  if (!workflowId) {
    return (
      <div className="p-6">
        <Alert variant="info">
          <ShieldCheck />
          <AlertTitle>Context is workflow-scoped</AlertTitle>
          <AlertDescription>Save the workflow before inspecting its Context.</AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-foreground">Agent Context</h3>
          <p className="text-xs text-muted-foreground">
            Exact observable journal and active replay for this Context node.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void contextQuery.refetch()}
            disabled={contextQuery.isFetching}
          >
            <RefreshCw className={contextQuery.isFetching ? 'animate-spin' : ''} />
            Refresh
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => forkMutation.mutate()}
            disabled={forkMutation.isPending}
          >
            <GitFork />
            Fork
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => exportMutation.mutate()}
            disabled={exportMutation.isPending}
          >
            <Download />
            Export
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => clearMutation.mutate()}
            disabled={clearMutation.isPending}
          >
            <Archive />
            Clear epoch
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="rounded-md border border-border bg-card p-3">
          <div className="text-xs text-muted-foreground">Thread / epoch</div>
          <div className="truncate text-sm font-medium">
            {snapshot.thread_id || '—'} / {snapshot.epoch ?? '—'}
          </div>
          <div className="text-xs text-muted-foreground">
            Revision {snapshot.revision ?? '—'}
          </div>
        </div>
        <div className="rounded-md border border-border bg-card p-3">
          <div className="text-xs text-muted-foreground">Replay fidelity</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {/* Success is claimed only when the server actually reported both
                a fidelity and resumability. Defaulting an absent verdict to
                green asserted a guarantee the server never made. */}
            <Badge
              variant={
                !snapshot.fidelity || snapshot.resumable !== true ? 'warning' : 'success'
              }
            >
              {snapshot.fidelity || 'unknown'}
            </Badge>
            {snapshot.resumable === false && <Badge variant="outline">non-resumable</Badge>}
          </div>
        </div>
        <div className="rounded-md border border-border bg-card p-3">
          <div className="text-xs text-muted-foreground">Provider</div>
          <div className="text-sm font-medium">{snapshot.provider || '—'}</div>
          <div className="text-xs text-muted-foreground">
            {/* "unbound" is a real server verdict, so it must not double as
                the placeholder for "the server said nothing". */}
            Binding {snapshot.provider_binding_status || '—'}
          </div>
        </div>
        <div className="rounded-md border border-border bg-card p-3">
          <div className="text-xs text-muted-foreground">Active pressure</div>
          <div className="mb-2 text-sm font-medium">
            {snapshot.active_token_count ?? 0}
            {snapshot.context_window ? ` / ${snapshot.context_window}` : ' tokens'}
          </div>
          {pressurePercent === null ? (
            <div className="text-xs text-muted-foreground">
              Set a context window to track pressure
            </div>
          ) : (
            <Progress value={pressurePercent} />
          )}
        </div>
      </div>

      {(snapshot.checkpoints?.length ?? 0) > 0 && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="mb-2 text-sm font-medium">Checkpoints</div>
          <div className="flex flex-col gap-2">
            {snapshot.checkpoints!.map((checkpoint, index) => (
              <div
                key={`${checkpoint.source_hash || checkpoint.covers_through_sequence || index}`}
                className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
              >
                <Badge variant="outline">{checkpoint.strategy || 'portable'}</Badge>
                <span>{checkpoint.provider || 'unknown provider'}</span>
                <span>through #{checkpoint.covers_through_sequence ?? '—'}</span>
                <span>{checkpoint.active_token_count ?? 0} active tokens</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <Tabs
        value={view}
        onValueChange={(next) => {
          setView(next as 'journal' | 'active');
          setCursor(null);
          setCursorHistory([]);
        }}
        className="min-h-0 flex-1"
      >
        <TabsList>
          <TabsTrigger value="journal">Raw journal</TabsTrigger>
          <TabsTrigger value="active">Active replay</TabsTrigger>
        </TabsList>
        <TabsContent value="journal" className="mt-2">
          {contextQuery.isLoading ? (
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          ) : contextQuery.error ? (
            <Alert variant="destructive">
              <AlertTitle>Context unavailable</AlertTitle>
              <AlertDescription>{contextQuery.error.message}</AlertDescription>
            </Alert>
          ) : (snapshot.events?.length ?? 0) === 0 ? (
            <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
              No committed Context events yet.
            </div>
          ) : (
            // Radix Accordion rather than <details>: it ships the
            // aria-expanded / keyboard semantics, and `type="multiple"`
            // keeps rows independently expandable.
            <Accordion type="multiple" className="flex flex-col gap-2">
              {snapshot.events!.map((event) => {
                const key = `${event.sequence}:${event.operation_id || event.event_type}`;
                return (
                  <AccordionItem
                    key={key}
                    value={key}
                    className="rounded-md border border-border bg-card p-3"
                  >
                    <AccordionTrigger className="gap-2 py-0 text-sm no-underline hover:no-underline">
                      <span className="font-mono text-xs text-muted-foreground">
                        #{event.sequence}
                      </span>
                      <span className="font-medium">{event.event_type}</span>
                      {event.provider && (
                        <Badge variant="outline">{event.provider}</Badge>
                      )}
                    </AccordionTrigger>
                    <AccordionContent className="pt-3 pb-0">
                      <JsonView
                        value={
                          (event.message_wire_v2 ??
                            event.payload ?? {
                              payload_ref: event.payload_ref,
                              payload_hash: event.payload_hash,
                              operation_id: event.operation_id,
                            }) as object
                        }
                        collapsed={2}
                        displayDataTypes={false}
                      />
                    </AccordionContent>
                  </AccordionItem>
                );
              })}
            </Accordion>
          )}
        </TabsContent>
        <TabsContent value="active" className="mt-2">
          {contextQuery.isLoading ? (
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          ) : (
            snapshot.active_replay ? (
              <div className="rounded-md border border-border bg-card p-3">
                <JsonView
                  value={snapshot.active_replay as object}
                  collapsed={3}
                  displayDataTypes={false}
                />
              </div>
            ) : (
              <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
                No active replay is available.
              </div>
            )
          )}
        </TabsContent>
      </Tabs>

      {view === 'journal' && (
        <div className="flex items-center justify-between">
          <Button
            variant="outline"
            size="sm"
            disabled={cursorHistory.length === 0}
            onClick={() => {
              setCursorHistory((history) => {
                const next = [...history];
                setCursor(next.pop() ?? null);
                return next;
              });
            }}
          >
            <ChevronLeft />
            Previous
          </Button>
          <span className="text-xs text-muted-foreground">Up to {PAGE_SIZE} events per page</span>
          <Button
            variant="outline"
            size="sm"
            disabled={!snapshot.next_cursor}
            onClick={() => {
              if (!snapshot.next_cursor) return;
              setCursorHistory((history) => [...history, cursor]);
              setCursor(snapshot.next_cursor);
            }}
          >
            Next
            <ChevronRight />
          </Button>
        </div>
      )}
    </div>
  );
};

export default ContextPanel;
