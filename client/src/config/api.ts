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

  // In production (non-localhost), use relative URLs (same origin)
  const isProduction = typeof window !== 'undefined' &&
    !window.location.hostname.includes('localhost') &&
    !window.location.hostname.includes('127.0.0.1');

  return {
    // Python FastAPI backend (port 5678 in dev, same origin in prod)
    PYTHON_BASE_URL: viteEnv.VITE_PYTHON_SERVICE_URL || (isProduction ? '' : 'http://localhost:5678'),
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
