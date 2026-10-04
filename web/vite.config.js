import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import mockApi from './dev/mock-api.js';

// `npm run dev` serves dev/fixtures in place of the backend. FLOWCAST_API=<origin> proxies /api and /data to a
// real backend instead. Builds are plain static files with content-hashed assets (assets/[name]-[hash].js);
// scripts/deploy.sh sets --base for preview deploys.
const backend = process.env.FLOWCAST_API;

export default defineConfig({
  plugins: [react(), tailwindcss(), !backend && mockApi()],
  server: {
    port: 5174,
    // Never drift onto a neighbor's port (5175 is the site-selection prototype's).
    strictPort: true,
    proxy: backend ? { '/api': { target: backend, changeOrigin: true }, '/data': { target: backend, changeOrigin: true } } : undefined,
  },
});
