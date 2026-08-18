import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import JsonView from '@uiw/react-json-view';
import { Loader2, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { toast } from 'sonner';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useWebSocket } from '@/contexts/WebSocketContext';

interface ConversationMessage {
  role?: string;
  content?: unknown;
  tool_calls?: unknown[];
  tool_call_id?: string;
  name?: string;
}

interface ConversationMeta {
  workflow_id: string;
  generation: number;
  agent_node_id: string;
  message_count: number;
  updated_at?: string | null;
}

interface ContextSnapshot {
  conversations?: ConversationMeta[];
  generation?: number | null;
  agent_node_id?: string | null;
  updated_at?: string | null;
  message_count?: number;
  messages?: ConversationMessage[];
}

interface ContextResponse {
  success?: boolean;
  context?: ContextSnapshot;
  error?: string;
}

interface ContextPanelProps {
  nodeId: string;
  workflowId?: string;
}

const conversationValue = (meta: {
  generation?: number | null;
  agent_node_id?: string | null;
}) => `${meta.generation ?? ''}:${meta.agent_node_id ?? ''}`;

const contextQueryKey = (
  workflowId: string | undefined,
  nodeId: string,
  selected: string,
) => ['agentContext', workflowId ?? '', nodeId, selected] as const;

/** Authorized, query-backed viewer for the plain conversation store.
 *
 * One conversation per (workflow, generation, agent node); this panel lists
 * the stored conversations and renders the selected transcript. It never
 * reads transcript data from workflow params, node status, or websocket
 * broadcasts — `context.updated` broadcasts only trigger a refetch through
 * the authorized `get_agent_context` handler.
 */
const ContextPanel: React.FC<ContextPanelProps> = ({ nodeId, workflowId }) => {
  const { sendRequest } = useWebSocket();
  const queryClient = useQueryClient();
  // '' = server default (the newest stored conversation).
  const [selected, setSelected] = useState<string>('');
  const [selectedGeneration, selectedAgent] = selected
    ? selected.split(/:(.*)/s, 2)
    : ['', ''];

  const queryKey = contextQueryKey(workflowId, nodeId, selected);
  const contextQuery = useQuery<ContextResponse, Error>({
    queryKey,
    queryFn: () =>
      sendRequest<ContextResponse>('get_agent_context', {
        workflow_id: workflowId,
        context_node_id: nodeId,
        ...(selectedGeneration ? { generation: Number(selectedGeneration) } : {}),
        ...(selectedAgent ? { agent_node_id: selectedAgent } : {}),
      }),
    enabled: !!workflowId && !!nodeId,
  });
  const snapshot = contextQuery.data?.context ?? {};
  const conversations = snapshot.conversations ?? [];
  const messages = snapshot.messages ?? [];

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
        ...(snapshot.generation != null ? { generation: snapshot.generation } : {}),
        ...(snapshot.agent_node_id ? { agent_node_id: snapshot.agent_node_id } : {}),
      }),
    onSuccess: async () => {
      setSelected('');
      await invalidateContext();
      toast.success('Conversation cleared');
    },
    onError: () => toast.error('Failed to clear conversation'),
  });

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
          <h3 className="text-base font-semibold text-foreground">Agent Conversation</h3>
          <p className="text-xs text-muted-foreground">
            Stored conversation for each connected agent, one per workflow generation.
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
            variant="destructive"
            size="sm"
            onClick={() => clearMutation.mutate()}
            disabled={clearMutation.isPending || messages.length === 0}
          >
            <Trash2 />
            Clear
          </Button>
        </div>
      </div>

      {conversations.length > 1 && (
        <Select
          value={selected || conversationValue(snapshot)}
          onValueChange={setSelected}
        >
          <SelectTrigger className="w-full max-w-md">
            <SelectValue placeholder="Select a conversation" />
          </SelectTrigger>
          <SelectContent>
            {conversations.map((meta) => (
              <SelectItem key={conversationValue(meta)} value={conversationValue(meta)}>
                Generation {meta.generation} · {meta.agent_node_id} ·{' '}
                {meta.message_count} message{meta.message_count === 1 ? '' : 's'}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      {snapshot.agent_node_id && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="outline">Generation {snapshot.generation ?? '—'}</Badge>
          <Badge variant="outline">{snapshot.agent_node_id}</Badge>
          <span>
            {snapshot.message_count ?? 0} message
            {(snapshot.message_count ?? 0) === 1 ? '' : 's'}
          </span>
          {snapshot.updated_at && <span>updated {snapshot.updated_at}</span>}
        </div>
      )}

      {contextQuery.isLoading ? (
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      ) : contextQuery.error ? (
        <Alert variant="destructive">
          <AlertTitle>Context unavailable</AlertTitle>
          <AlertDescription>{contextQuery.error.message}</AlertDescription>
        </Alert>
      ) : messages.length === 0 ? (
        <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          No stored conversation yet.
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {messages.map((message, index) => (
            <div
              key={index}
              className="rounded-md border border-border bg-card p-3"
            >
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <Badge variant="outline">{message.role || 'message'}</Badge>
                {message.name && (
                  <span className="font-mono text-xs text-muted-foreground">
                    {message.name}
                  </span>
                )}
              </div>
              {typeof message.content === 'string' && message.content ? (
                <div className="whitespace-pre-wrap text-sm text-foreground">
                  {message.content}
                </div>
              ) : message.content != null ? (
                <JsonView
                  value={message.content as object}
                  collapsed={2}
                  displayDataTypes={false}
                />
              ) : null}
              {(message.tool_calls?.length ?? 0) > 0 && (
                <div className="mt-2">
                  <div className="mb-1 text-xs text-muted-foreground">Tool calls</div>
                  <JsonView
                    value={message.tool_calls as object}
                    collapsed={1}
                    displayDataTypes={false}
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ContextPanel;
