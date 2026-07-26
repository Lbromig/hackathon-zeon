import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";

// 127.0.0.1, not localhost: uvicorn binds IPv4 only by default, while Node resolves
// "localhost" to ::1 first — so a localhost target makes every proxied request fail
// with "Failed to fetch" against a backend that is plainly running.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // REST + websocket to the FastAPI backend
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
