/**
 * Node.js Code Execution Server
 * Thin HTTP layer - all parameters from environment or requests
 */

import express, { Request, Response, NextFunction } from 'express';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// All configuration from environment variables
const PORT = parseInt(process.env.NODEJS_EXECUTOR_PORT ?? '', 10);
if (!PORT) {
  // Spawned by nodes/code/_runtime.py, which always injects the port
  // (canonical default lives in .env.template). No hardcoded fallback.
  throw new Error('NODEJS_EXECUTOR_PORT is not set - defaults live in .env.template');
}
const HOST = process.env.NODEJS_EXECUTOR_HOST ?? 'localhost';
const BODY_LIMIT = process.env.NODEJS_EXECUTOR_BODY_LIMIT ?? '10mb';
const USER_PACKAGES_DIR = process.env.NODEJS_USER_PACKAGES_DIR ?? path.join(__dirname, '..', 'user-packages');

const app = express();
app.use(express.json({ limit: BODY_LIMIT }));

interface ExecuteRequest {
  code: string;
  language?: 'javascript' | 'typescript';
  input_data?: Record<string, unknown>;
  timeout?: number;
}

// Health check
app.get('/health', (_req: Request, res: Response) => {
  res.json({
    status: 'healthy',
    service: 'nodejs-executor',
    node_version: process.version,
  });
});

// Execute code - all parameters from request body
app.post('/execute', (req: Request, res: Response) => {
  const { code, input_data = {}, timeout = 30000 } = req.body as ExecuteRequest;

  if (!code || typeof code !== 'string') {
    res.status(400).json({ success: false, error: 'Missing or invalid "code" field' });
    return;
  }

  const startTime = Date.now();
  const consoleOutput: string[] = [];

  const capturedConsole = {
    log: (...args: unknown[]) => consoleOutput.push(args.map(String).join(' ')),
    error: (...args: unknown[]) => consoleOutput.push(`[ERROR] ${args.map(String).join(' ')}`),
    warn: (...args: unknown[]) => consoleOutput.push(`[WARN] ${args.map(String).join(' ')}`),
    info: (...args: unknown[]) => consoleOutput.push(args.map(String).join(' ')),
  };

  const sandbox = {
    console: capturedConsole,
    input_data,
    output: undefined as unknown,
    JSON, Math, Date, Array, Object, String, Number, Boolean, RegExp, Map, Set, Promise,
    setTimeout, setInterval, clearTimeout, clearInterval,
  };

  try {
    const context = vm.createContext(sandbox);
    // This service IS the sandboxed JS executor for the pythonExecutor /
    // javascriptExecutor workflow nodes. Node's `vm` is not a security
    // boundary (per https://nodejs.org/api/vm.html#vmcreatecontextcontextobject-options);
    // the deployment context is: server binds to localhost only (line 16,
    // default 'localhost') and is invoked exclusively by the same-machine
    // Python backend via NodeJSClient. Public network exposure is the
    // operator's responsibility.
    // codeql[js/code-injection]
    vm.runInContext(code, context, { timeout, filename: 'user-code.js' });

    res.json({
      success: true,
      output: sandbox.output,
      console_output: consoleOutput.join('\n'),
      execution_time_ms: Date.now() - startTime,
    });
  } catch (error) {
    res.json({
      success: false,
      error: error instanceof Error ? error.message : String(error),
      console_output: consoleOutput.join('\n'),
      execution_time_ms: Date.now() - startTime,
    });
  }
});

// Install packages - package list from request.
// Localhost-only service (see server.listen at the bottom); same trust
// boundary as the /execute sandbox. No request-rate limiting because the
// only caller is the same-machine Python backend.
// codeql[js/missing-rate-limiting]
app.post('/packages/install', (req: Request, res: Response) => {
  const { packages } = req.body as { packages: string[] };

  if (!packages || !Array.isArray(packages) || packages.length === 0) {
    res.status(400).json({ success: false, error: 'Missing or invalid "packages" array' });
    return;
  }

  const validPattern = /^(@[\w-]+\/)?[\w-]+(@[\w.-]+)?$/;
  const invalid = packages.filter(p => !validPattern.test(p));
  if (invalid.length > 0) {
    res.status(400).json({ success: false, error: `Invalid package names: ${invalid.join(', ')}` });
    return;
  }

  try {
    // execFileSync (argv array, no shell) instead of execSync with template
    // string. The regex above already validates names, but going through
    // execFileSync removes the shell from the path entirely as
    // defense-in-depth.
    execFileSync('npm', ['install', ...packages], { cwd: USER_PACKAGES_DIR, timeout: 60000 });
    res.json({ success: true, message: `Installed: ${packages.join(', ')}` });
  } catch (error) {
    res.status(500).json({ success: false, error: error instanceof Error ? error.message : String(error) });
  }
});

// List packages — same localhost-only trust boundary as above.
// codeql[js/missing-rate-limiting]
app.get('/packages', (_req: Request, res: Response) => {
  try {
    const output = execFileSync('npm', ['list', '--json', '--depth=0'], { cwd: USER_PACKAGES_DIR, encoding: 'utf-8' });
    res.json({ success: true, packages: JSON.parse(output).dependencies || {} });
  } catch {
    res.json({ success: true, packages: {} });
  }
});

app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
  console.error('Server error:', err);
  res.status(500).json({ success: false, error: 'Internal server error' });
});

app.listen(PORT, HOST, () => {
  console.log(`Node.js Executor running on http://${HOST}:${PORT}`);
});
