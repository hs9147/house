import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// 백엔드 라우터는 모두 /paas 아래 마운트된다(app/main.py의 PAAS_PREFIX와 동일해야 함).
const API_PREFIXES = ['/paas'];

export default defineConfig({
  plugins: [react()],
  base: '/console/',
  server: {
    proxy: Object.fromEntries(
      API_PREFIXES.map((p) => [p, { target: 'http://localhost:7000', changeOrigin: true }]),
    ),
  },
  test: {
    environment: 'node',
  },
});
