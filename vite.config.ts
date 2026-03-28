import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    outDir: "web/dist",
    emptyDirBeforeWrite: true,
  },
  server: {
    port: 1420,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:17890", changeOrigin: true },
    },
  },
});
