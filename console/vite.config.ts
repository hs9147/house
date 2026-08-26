import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// 백엔드 라우터는 모두 /paas 아래 마운트된다(app/main.py의 PAAS_PREFIX와 동일해야 함).
const API_PREFIXES = ['/paas'];

export default defineConfig({
  plugins: [react()],
  base: '/console/',
  server: {
    allowedHosts: true,
    // ws: true가 없으면 프록시가 WebSocket 업그레이드를 넘기지 않는다 — HTTP는 전부
    // 정상인데 터미널 소켓만 **응답 없이 매달린다**(404도 403도 아니다). 개발 서버에서
    // 터미널 탭이 영원히 "연결 중"이던 원인이다.
    proxy: Object.fromEntries(
      API_PREFIXES.map((p) => [
        p,
        { target: 'http://localhost:7000', changeOrigin: true, ws: true },
      ]),
    ),
  },
  test: {
    environment: 'node',
  },
});
