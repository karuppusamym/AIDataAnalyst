import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `@types/node` is intentionally not part of this browser-only TypeScript
// project. Vite's config does run in Node, so read its environment through a
// narrow runtime type rather than widening the application's type surface.
const runtimeEnvironment = (
  globalThis as typeof globalThis & {
    process?: { env?: Record<string, string | undefined> };
  }
).process?.env;

// The API runs as a modular monolith on :8000. In dev we proxy rather than turn
// on CORS server-side, so the browser sees one origin and cookie/OIDC behaviour
// matches production, where nginx serves the SPA and the API from one host.
export default defineConfig({
  plugins: [react()],
  server: {
    // `VITE_API_PROXY_TARGET` lets the same configuration work on the host
    // (`localhost`) and inside the Docker development network (`api`).
    // Bind explicitly so Docker can publish the dev server, and use polling
    // when requested because Windows bind mounts do not reliably emit file
    // system events into Linux containers.
    host: true,
    port: 5174,
    strictPort: true,
    watch:
      runtimeEnvironment?.CHOKIDAR_USEPOLLING === "true"
        ? { usePolling: true }
        : undefined,
    proxy: {
      "/v1": {
        target:
          runtimeEnvironment?.VITE_API_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
