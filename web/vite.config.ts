import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8777",
        changeOrigin: true,
        ws: true
      }
    }
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        chat: `${root}chat.html`,
        knowledge: `${root}knowledge.html`,
        settings: `${root}settings.html`,
        sessions: `${root}index.html`,
        tasks: `${root}tasks.html`,
        login: `${root}login.html`
      }
    }
  }
});
