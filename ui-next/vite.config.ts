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

/* HTTPS for the dev server, opt-in. The Excel add-in's pages must be served
   over HTTPS -- Office refuses a plain-HTTP task pane, localhost included -- so
   `VITE_DEV_HTTPS_CERT` / `VITE_DEV_HTTPS_KEY` name a certificate and key the
   developer created and trusted themselves (`ui-next/excel-addin/README.md`).
   Unset, the dev server stays on plain HTTP exactly as before, which is what
   the in-app browser preview expects. The files are read through a non-literal
   dynamic import for the same reason `runtimeEnvironment` is narrowly typed:
   this browser-only project does not carry `@types/node`. */
async function devHttps(): Promise<{ cert: string; key: string } | undefined> {
  const certPath = runtimeEnvironment?.VITE_DEV_HTTPS_CERT;
  const keyPath = runtimeEnvironment?.VITE_DEV_HTTPS_KEY;
  if (!certPath || !keyPath) return undefined;
  const specifier = "node:fs/promises";
  const fs = (await import(/* @vite-ignore */ specifier)) as {
    readFile(path: string, encoding: "utf8"): Promise<string>;
  };
  return { cert: await fs.readFile(certPath, "utf8"), key: await fs.readFile(keyPath, "utf8") };
}

// The API runs as a modular monolith on :8000. In dev we proxy rather than turn
// on CORS server-side, so the browser sees one origin and cookie/OIDC behaviour
// matches production, where nginx serves the SPA and the API from one host.
export default defineConfig(async () => ({
  plugins: [react()],
  server: {
    https: await devHttps(),
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
      // The MCP transport is mounted at `/mcp`, outside `/v1`. The Agent
      // gateway screen tells an engineer the endpoint is
      // `<this origin>/mcp`; without this proxy that statement would be true
      // in every deployment except the one they are looking at.
      "/mcp": {
        target:
          runtimeEnvironment?.VITE_API_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      // The shell, plus the two pages the Excel add-in loads inside Office.
      input: {
        main: "index.html",
        excelAddin: "excel-addin.html",
        excelAddinAuth: "excel-addin-auth.html",
      },
    },
  },
}));
