import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// `cr serve` listens on 8000 by default. Override with CR_API when that port is
// already claimed by something else on the machine (IIS takes it on Windows).
const API = process.env.CR_API || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": API,
      "/auth": API,
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
