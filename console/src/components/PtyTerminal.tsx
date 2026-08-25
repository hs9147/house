import { useEffect, useRef, useState } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { openTerminalSocket } from '../lib/api';

/**
 * 서버의 셸에 PTY로 붙는 진짜 터미널.
 *
 * 예전 "콘솔" 탭은 명령 한 줄을 보내고 끝날 때까지 기다렸다가 출력을 통째로 받는
 * 방식이라 되묻는 명령·Ctrl+C·긴 작업의 진행 상황을 다룰 수 없었다. 여기서는 키 입력과
 * 화면 출력을 바이트로 그대로 주고받는다.
 *
 * 프로토콜(app/api/system.py): 보낼 때는 JSON({type:'input'|'resize'}), 받을 때는 터미널
 * 출력 그대로. 키 입력에는 어떤 바이트든 올 수 있어 구분자를 둘 자리가 없으므로 보내는
 * 쪽만 감싼다.
 */
export default function PtyTerminal() {
  const holder = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'연결 중' | '연결됨' | '끊김'>('연결 중');

  useEffect(() => {
    if (!holder.current) return;

    const term = new Terminal({
      fontFamily: 'Consolas, D2Coding, "Courier New", monospace',
      fontSize: 13,
      cursorBlink: true,
      // 되돌아볼 줄 수. 빌드 로그를 그대로 흘려보는 자리라 넉넉히 잡는다.
      scrollback: 5000,
      theme: { background: '#11151c', foreground: '#d6deeb' },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(holder.current);
    fit.fit();

    const socket = openTerminalSocket();
    const sendResize = () => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      }
    };

    socket.onopen = () => {
      setStatus('연결됨');
      sendResize(); // 셸이 창 크기를 모르면 줄바꿈이 어긋난다
      term.focus();
    };
    socket.onmessage = (e) => term.write(e.data as string);
    socket.onclose = () => {
      setStatus('끊김');
      term.write('\r\n\x1b[33m[세션이 끝났습니다 — 다시 열려면 새로고침하세요]\x1b[0m\r\n');
    };

    const input = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'input', data }));
      }
    });

    // 창 크기가 바뀌면 다시 맞춰 셸에 알린다. 탭 전환으로 화면이 접혔다 펴질 때도
    // 크기가 달라지므로 window resize만으로는 놓친다.
    const observer = new ResizeObserver(() => {
      try {
        fit.fit();
        sendResize();
      } catch {
        // 화면에서 떨어져 나간 직후엔 크기를 잴 수 없다 — 다음 관측에서 맞는다
      }
    });
    observer.observe(holder.current);

    return () => {
      observer.disconnect();
      input.dispose();
      socket.close();
      term.dispose();
    };
  }, []);

  return (
    <div>
      <p className="mutedtext" style={{ fontSize: 12, marginBottom: 8 }}>
        서버 셸에 PTY로 직접 붙습니다 — 되묻는 명령·Ctrl+C·긴 작업의 실시간 출력이
        그대로 동작합니다. 상태: <b>{status}</b>
      </p>
      <div
        ref={holder}
        style={{ height: '60vh', background: '#11151c', padding: 8, borderRadius: 6 }}
      />
    </div>
  );
}
