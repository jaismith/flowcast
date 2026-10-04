import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// The serving backend (PR #64). The page reads it same-origin, as it will behind CloudFront, so locally the dev
// server proxies it; CloudFront sends no CORS headers.
const SERVING = process.env.FLOWCAST_ORIGIN ?? 'https://d2plrkhnzsjv1y.cloudfront.net';
const proxy = Object.fromEntries(['/data/v1', '/api'].map((path) => [path, { target: SERVING, changeOrigin: true }]));

// Data is shared with the PR #54 landing prototype rather than copied (about 9 MB of validation-year JSON).
export default defineConfig({
  // Absolute, because pages live at /site/<id> and relative asset and data URLs would resolve under it.
  base: '/',
  publicDir: '../landing/public',
  plugins: [react(), tailwindcss()],
  server: { port: 5174, proxy },
  preview: { proxy },
});
