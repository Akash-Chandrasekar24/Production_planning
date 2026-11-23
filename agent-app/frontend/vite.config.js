import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 127.0.0.1, not "localhost": localhost can resolve to IPv6 (::1) while the API listens on IPv4 only
    proxy: { "/api": "http://127.0.0.1:8000" },
    // polling: the default file watcher stopped seeing edits in this folder on Windows
    watch: { usePolling: true, interval: 300 },
  },
});
