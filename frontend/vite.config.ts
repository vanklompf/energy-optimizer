import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend URL to proxy /api and /healthz to during development. In Docker this is
// the app service (http://app:8320); on the host it defaults to localhost.
const apiProxy = process.env.VITE_API_PROXY ?? "http://localhost:8320";

// Build output goes into the Python package so FastAPI serves it directly.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/energy_optimizer/web/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": apiProxy,
      "/healthz": apiProxy,
    },
  },
});
