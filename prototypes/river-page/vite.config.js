import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Data is shared with the PR #54 landing prototype rather than copied (about 9 MB of validation-year JSON).
export default defineConfig({
  // Absolute, because pages live at /site/<id> and relative asset and data URLs would resolve under it.
  base: '/',
  publicDir: '../landing/public',
  plugins: [react(), tailwindcss()],
  server: { port: 5174 },
});
