import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs as a modular monolith on :8000. In dev we proxy rather than turn
// on CORS server-side, so the browser sees one origin and cookie/OIDC behaviour
// matches production, where nginx serves the SPA and the API from one host.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: { "/v1": { target: "http://localhost:8000", changeOrigin: true } },
  },
  build: { outDir: "dist", sourcemap: true },
});
