import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/user-api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      // All /api/* requests → FastAPI backend on localhost:8082
      // The /api prefix is stripped before forwarding.
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          // SAP UI5 WebComponents — largest chunk, loaded once and cached
          "ui5-core":    ["@ui5/webcomponents-react"],
          // Recharts only needed on Dashboard
          "recharts":    ["recharts"],
          // React ecosystem
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          // State / data-fetching
          "query":       ["@tanstack/react-query", "zustand"],
        },
      },
    },
    // Warn if any single chunk exceeds 600 KB after splitting
    chunkSizeWarningLimit: 600,
  },
});
