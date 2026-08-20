import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the gateway (port 8000), so the frontend
// only ever talks to ONE origin — just like it will in production behind nginx.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
