/**
 * Centralized API Configuration
 * Single source of truth for all backend service URLs
 */

interface ApiConfig {
  readonly PYTHON_BASE_URL: string;
}

/**
 * Get API configuration from environment variables with fallback defaults
 */
function getApiConfig(): ApiConfig {
  const viteEnv = (import.meta as any).env || {};

  return {
    // Same origin everywhere: in production uvicorn serves the SPA and
    // the API on one port; in dev the Vite server proxies the backend
    // prefixes (/api, /ws, /webhook, /health, /mcp) — see
    // client/vite.config.js. VITE_PYTHON_SERVICE_URL remains as an
    // explicit escape hatch for pointing at a remote backend.
    PYTHON_BASE_URL: viteEnv.VITE_PYTHON_SERVICE_URL || '',
  };
}

/**
 * API Configuration singleton
 * Import this in services instead of hardcoding URLs
 *
 * @example
 * import { API_CONFIG } from '../config/api';
 * fetch(`${API_CONFIG.PYTHON_BASE_URL}/api/workflow/execute-node`);
 */
export const API_CONFIG = getApiConfig();

/**
 * Helper to build API endpoint URLs
 */
export const buildApiUrl = (path: string, baseUrl: string = API_CONFIG.PYTHON_BASE_URL): string => {
  // Ensure path starts with /
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  // Remove trailing slash from baseUrl
  const normalizedBase = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
  return `${normalizedBase}${normalizedPath}`;
};
