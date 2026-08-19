import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchWorkspaceText } from '../useWorkspaceText';

const encoder = new TextEncoder();

/** Minimal streamed-body double: yields the given chunks, tracks cancel. */
function streamedResponse(chunks: string[], status = 200) {
  let index = 0;
  const cancel = vi.fn(async () => {});
  return {
    response: {
      ok: status < 400,
      status,
      body: {
        getReader: () => ({
          read: async () =>
            index < chunks.length
              ? { done: false, value: encoder.encode(chunks[index++]) }
              : { done: true, value: undefined },
          cancel,
        }),
      },
    } as unknown as Response,
    cancel,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchWorkspaceText', () => {
  it('reads the full body when under the cap', async () => {
    const { response } = streamedResponse(['hello ', 'world']);
    vi.stubGlobal('fetch', vi.fn(async () => response));

    const result = await fetchWorkspaceText('/api/workspace/wf/files/a.md');
    expect(result).toEqual({ text: 'hello world', truncated: false });
  });

  it('stops reading at the cap, cancels the reader, and flags truncation', async () => {
    const { response, cancel } = streamedResponse(['abcde', 'fghij', 'never-read']);
    vi.stubGlobal('fetch', vi.fn(async () => response));

    const result = await fetchWorkspaceText('/api/workspace/wf/files/big.log', 8);
    expect(result.truncated).toBe(true);
    expect(result.text).toBe('abcdefgh');
    expect(cancel).toHaveBeenCalled();
  });

  it('throws with the status on a non-ok response (401 must not render blank)', async () => {
    const { response } = streamedResponse([], 401);
    vi.stubGlobal('fetch', vi.fn(async () => response));

    await expect(fetchWorkspaceText('/api/workspace/wf/files/x.md')).rejects.toThrow('401');
  });

  it('falls back to text() when the environment has no streamed body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        body: null,
        text: async () => 'x'.repeat(10),
      })),
    );

    const result = await fetchWorkspaceText('/api/workspace/wf/files/x.md', 5);
    expect(result).toEqual({ text: 'xxxxx', truncated: true });
  });

  it('sends the session cookie and routes through buildApiUrl', async () => {
    const fetchMock = vi.fn(async () => streamedResponse(['ok']).response);
    vi.stubGlobal('fetch', fetchMock);

    await fetchWorkspaceText('/api/workspace/wf/files/a.md');
    expect(fetchMock).toHaveBeenCalledWith('/api/workspace/wf/files/a.md', {
      credentials: 'include',
    });
  });
});
