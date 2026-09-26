import { defineConfig } from 'vite';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));

export const PAGES = [
  'river-pulse',
  'watershed-3d',
  'forecast-fan',
  'storm-explorer',
  'river-year',
  'raindrop-journey',
  'flood-wave',
  'river-pulse-v2',
  'watershed-3d-v2',
];

export default defineConfig({
  base: './',
  // MapLibre v6 runs its worker as an ES module (see raindrop-journey/main.js).
  worker: { format: 'es' },
  build: {
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      input: {
        main: resolve(root, 'index.html'),
        ...Object.fromEntries(PAGES.map((p) => [p, resolve(root, p, 'index.html')])),
      },
    },
  },
});
