import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "/",
  server: {
    port: 5174,
    proxy: {
      "/v1": "http://127.0.0.1:8780",
      "/api": "http://127.0.0.1:8780",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
