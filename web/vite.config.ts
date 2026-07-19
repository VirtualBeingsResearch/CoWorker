import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

function coworkerLogPlugin(): Plugin {
  return {
    name: 'coworker-log',
    configureServer(server) {
      server.middlewares.use('/__coworker_log', (req, res) => {
        if (req.method !== 'POST') {
          res.statusCode = 405;
          res.end();
          return;
        }
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', () => {
          try {
            const evt = JSON.parse(body || '{}');
            server.config.logger.info(`[coworker-chat:${evt.type || 'info'}] ${evt.time || ''} ${evt.message || ''}`);
          } catch {
            server.config.logger.info(`[coworker-chat:info] ${body}`);
          }
          res.statusCode = 204;
          res.end();
        });
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const apiTarget = (env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
  const wsTarget = (env.VITE_WS_BASE_URL || apiTarget.replace(/^http/, 'ws')).replace(/\/$/, '');

  return {
    plugins: [react(), coworkerLogPlugin()],
    build: {
      // Ship the management UI with the Python package so source, wheel, and
      // Docker installs all expose the same first-run experience.
      outDir: '../src/coworker/web',
      emptyOutDir: true,
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/logs': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/status': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/profile': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/messages': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/sse': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/switch_model': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/app': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/ws': {
          target: wsTarget,
          ws: true,
          changeOrigin: true,
        },
      },
    },
  };
});
