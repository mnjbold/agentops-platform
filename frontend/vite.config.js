import { defineConfig } from 'vite';

// agentops Vite config.
// - dist/ output for production nginx deployment
// - dev server on 0.0.0.0:5173 (Coolify / docker friendly)
// - injects __API_BASE_PRIMARY__ / __API_BASE_FALLBACK__ globals so the api.js
//   module can target the right backend without an extra build step
// - source maps for prod debugging

export default defineConfig(({ mode }) => ({
  root: '.',
  publicDir: 'public',
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: false,
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
    target: 'es2020',
    cssCodeSplit: true,
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          if (id.includes('node_modules')) {
            return 'vendor';
          }
        },
      },
    },
  },
  define: {
    __API_BASE_PRIMARY__: JSON.stringify(process.env.VITE_API_BASE_PRIMARY || 'https://bkjr-api.getbijou.xyz'),
    __API_BASE_FALLBACK__: JSON.stringify(process.env.VITE_API_BASE_FALLBACK || 'https://bk-jr-api.aixlabs.fun'),
  },
}));
