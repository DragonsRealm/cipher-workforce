/// <reference types="vite/client" />
/// <reference types="google.maps" />
/// <reference types="node" />
// `google.maps` namespace is exposed via @types/google.maps. tsc 5.x
// auto-loads all @types/* packages, but tsgo (TypeScript 7 native
// preview) does not pick the namespace up reliably under pnpm's
// symlinked node_modules layout — the explicit reference here is the
// canonical fix per https://developers.google.com/maps/documentation/javascript/using-typescript
// and works for both compilers.
//
// `node` is here for the same tsgo reason, not because browser code may
// use Node APIs. Node-only tests under src/ (icons/index.test.ts reads
// server/nodes/**/meta.json off disk) need `node:fs` / `node:path` /
// `__dirname`; without this the root TS7 gate fails on them while
// client's own tsc 5.x passes, because 5.x auto-loads @types/node.

interface ImportMetaEnv {
  readonly VITE_ANDROID_RELAY_URL: string;
  readonly VITE_CLIENT_PORT: string;
  readonly VITE_PYTHON_BACKEND_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Allow importing SVG files as raw strings
declare module '*.svg?raw' {
  const content: string;
  export default content;
}
