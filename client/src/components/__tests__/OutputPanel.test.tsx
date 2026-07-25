/**
 * Tests for the output renderer (components/output/OutputPanel) —
 * specifically the spec-driven Response branches.
 *
 * Locks in:
 *   - uiHints.outputMode === 'terminal' renders string output in a
 *     <pre> with REAL newlines preserved (never ReactMarkdown, which
 *     collapses whitespace — the "raw stdout" mangling bug)
 *   - without the hint, string output keeps the markdown path
 *   - terminal stdout that is wholly JSON routes to the JSON tree
 *   - an object/array `result` payload (server-side-parsed CLI JSON)
 *     surfaces in the Response section as a tree
 */

import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '../../test/providers';

// Heavy renderers mocked — the tests assert ROUTING, not markdown/json internals.
vi.mock('react-markdown', () => ({
  default: ({ children }: any) => <div data-testid="markdown">{children}</div>,
}));
vi.mock('remark-gfm', () => ({ default: () => null }));
vi.mock('remark-breaks', () => ({ default: () => null }));
vi.mock('@uiw/react-json-view', () => ({
  default: ({ value }: any) => <div data-testid="json-view">{JSON.stringify(value)}</div>,
}));

// Spec source — the backend-owned display hint under test.
const specState: { spec: any } = { spec: null };
vi.mock('@/lib/nodeSpec', () => ({
  useNodeSpec: () => specState.spec,
}));

import OutputPanel from '../output/OutputPanel';

const NODE = { id: 'gh-1', type: 'githubAction' } as any;

const makeResult = (outputs: Record<string, any>) =>
  [{ nodeId: 'gh-1', success: true, executionTime: 12, outputs }] as any;

const GH_TABLE = 'trohitg/opencowork\tpublic\t2026-07-06\ntrohitg/opencompany\tpublic\t2026-07-06';

describe('OutputPanel response routing', () => {
  it('renders terminal stdout in a <pre> with real newlines when uiHints.outputMode=terminal', () => {
    specState.spec = { uiHints: { outputMode: 'terminal' } };
    renderWithProviders(
      <OutputPanel
        results={makeResult({ operation: 'custom', success: true, stdout: GH_TABLE })}
        selectedNode={NODE}
      />,
    );
    const pre = screen.getByTestId('terminal-output');
    // Real newline + tab preserved — NOT whitespace-collapsed.
    expect(pre.textContent).toContain('trohitg/opencowork\tpublic');
    expect(pre.textContent).toContain('\ntrohitg/opencompany');
    expect(screen.queryByTestId('markdown')).toBeNull();
  });

  it('keeps the markdown path for string output when no outputMode hint is declared', () => {
    specState.spec = null; // e.g. spec not yet served / non-CLI node
    renderWithProviders(
      <OutputPanel
        results={makeResult({ operation: 'custom', success: true, stdout: GH_TABLE })}
        selectedNode={NODE}
      />,
    );
    expect(screen.getByTestId('markdown')).toBeTruthy();
    expect(screen.queryByTestId('terminal-output')).toBeNull();
  });

  it('routes terminal stdout that is wholly JSON to the tree viewer', () => {
    specState.spec = { uiHints: { outputMode: 'terminal' } };
    renderWithProviders(
      <OutputPanel
        results={makeResult({ operation: 'custom', success: true, stdout: '[{"login": "octocat"}]' })}
        selectedNode={NODE}
      />,
    );
    const views = screen.getAllByTestId('json-view');
    expect(views.some(v => v.textContent?.includes('octocat'))).toBe(true);
    expect(screen.queryByTestId('terminal-output')).toBeNull();
  });

  it('surfaces a parsed array `result` payload in the Response section', () => {
    specState.spec = { uiHints: { outputMode: 'terminal' } };
    renderWithProviders(
      <OutputPanel
        results={makeResult({ operation: 'pr_list', success: true, result: [{ number: 42 }] })}
        selectedNode={NODE}
      />,
    );
    const views = screen.getAllByTestId('json-view');
    expect(views.some(v => v.textContent?.includes('42'))).toBe(true);
  });
});

/**
 * Audio results.
 *
 * An AudioRef is an object, so without a dedicated branch it falls into
 * the JSON tree and the user gets a wall of metadata instead of something
 * they can play. Detection is structural (`kind === 'audio'`) rather than
 * hint-driven, so a ref reaching this panel from anywhere still renders.
 */
describe('OutputPanel — audio', () => {
  const SPEECH_NODE = { id: 'tts-1', type: 'textToSpeech' } as any;

  const audioRef = (overrides: Record<string, any> = {}) => ({
    kind: 'audio',
    path: 'audio/greeting-abc123.wav',
    workflow_id: 'wf-1',
    filename: 'greeting-abc123.wav',
    mime_type: 'audio/wav',
    format: 'wav',
    size_bytes: 24000,
    duration_seconds: 1.5,
    url: '/api/workspace/wf-1/files/audio/greeting-abc123.wav',
    ...overrides,
  });

  const makeSpeechResult = (outputs: Record<string, any>) =>
    [{ nodeId: 'tts-1', success: true, executionTime: 900, outputs }] as any;

  it('renders a player instead of dumping the reference as JSON', () => {
    specState.spec = { uiHints: { outputMode: 'audio' } };
    const { container } = renderWithProviders(
      <OutputPanel
        results={makeSpeechResult({ audio: audioRef(), files: [audioRef()], chunk_count: 1 })}
        selectedNode={SPEECH_NODE}
      />,
    );

    const player = container.querySelector('audio');
    expect(player).toBeTruthy();
    expect(player?.getAttribute('src')).toContain(
      '/api/workspace/wf-1/files/audio/greeting-abc123.wav',
    );
    expect(screen.getByText('greeting-abc123.wav')).toBeTruthy();
  });

  it('renders one player per clip when a provider split the input', () => {
    specState.spec = { uiHints: { outputMode: 'audio' } };
    const { container } = renderWithProviders(
      <OutputPanel
        results={makeSpeechResult({
          audio: audioRef({ path: 'audio/a.wav', filename: 'a.wav', sha256: 'aa' }),
          files: [
            audioRef({ path: 'audio/a.wav', filename: 'a.wav', sha256: 'aa' }),
            audioRef({ path: 'audio/b.wav', filename: 'b.wav', sha256: 'bb' }),
          ],
          chunk_count: 2,
        })}
        selectedNode={SPEECH_NODE}
      />,
    );

    expect(container.querySelectorAll('audio')).toHaveLength(2);
    // The clips are separate files, not parts of one stream — the UI has to
    // say so, because concatenating them produces unplayable audio.
    expect(screen.getByText(/separate files/i)).toBeTruthy();
  });

  it('does not hijack a non-audio result', () => {
    specState.spec = null;
    const { container } = renderWithProviders(
      <OutputPanel
        results={makeSpeechResult({ response: 'plain text' })}
        selectedNode={SPEECH_NODE}
      />,
    );

    expect(container.querySelector('audio')).toBeNull();
    expect(screen.getByTestId('markdown')).toBeTruthy();
  });
});
