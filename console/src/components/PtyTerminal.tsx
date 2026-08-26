import { useEffect, useRef, useState } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { api, openTerminalSocket } from '../lib/api';

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

// 핸드셰이크가 이만큼 지나도 안 끝나면 뭔가 붙들고 있는 것이다. 브라우저는 그동안
// 아무 말도 하지 않고 CONNECTING에 머물기 때문에, 말해 주지 않으면 "연결 중"만 남는다.
const HANDSHAKE_TIMEOUT_MS = 8000;

export default function PtyTerminal() {
  const holder = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'연결 중' | '연결됨' | '끊김'>('연결 중');
  // 소켓이 왜 안 열렸는지 브라우저는 알려주지 않는다(닫힘 코드 1006뿐). 서버에 같은
  // 것을 REST로 물어 "서버가 준비됐는지"와 "길이 막혔는지"를 가른다.
  const [diagnosis, setDiagnosis] = useState('');

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

    // 정리된 뒤에도 이 소켓의 이벤트는 뒤늦게 도착한다. 그대로 두면 **우리가 직접 버린
    // 소켓의 닫힘**이 화면을 "끊김"으로 확정하고 서버 진단까지 띄운다 — 정작 뒤이어 열린
    // 소켓은 멀쩡하거나 아직 시도 중인데도. 버린 소켓은 아무 말도 하지 않게 한다.
    let disposed = false;

    let everOpened = false;
    socket.onopen = () => {
      if (disposed) return;
      everOpened = true;
      setStatus('연결됨');
      setDiagnosis(''); // 늦게라도 열렸으면 앞서 띄운 지연 경고는 더 이상 사실이 아니다
      sendResize(); // 셸이 창 크기를 모르면 줄바꿈이 어긋난다
      term.focus();
    };
    socket.onmessage = (e) => {
      if (disposed) return;
      term.write(e.data as string);
    };
    socket.onclose = (e) => {
      if (disposed) return;
      setStatus('끊김');
      if (everOpened) {
        term.write('\r\n\x1b[33m[세션이 끝났습니다 — 다시 열려면 새로고침하세요]\x1b[0m\r\n');
        return;
      }
      // 한 번도 열리지 못했다 = 핸드셰이크 단계에서 막혔다. 원인을 서버에 물어본다.
      term.write(`\r\n\x1b[31m[연결하지 못했습니다 — 닫힘 코드 ${e.code}]\x1b[0m\r\n`);
      api
        .terminalPreflight()
        .then((r) => {
          setDiagnosis(r.ok ? r.hint : `${r.error} ${r.hint}`);
          term.write(
            `\x1b[33m서버 점검: 셸 ${r.shell} / 백엔드 ${r.backend} → ${r.ok ? 'OK' : r.error}\x1b[0m\r\n` +
              `\x1b[33m${r.hint}\x1b[0m\r\n`,
          );
        })
        .catch((err) => {
          setDiagnosis((err as Error).message);
          term.write(`\x1b[31m서버 점검도 실패: ${(err as Error).message}\x1b[0m\r\n`);
        });
    };

    // 핸드셰이크가 끝나지 않고 매달려 있는 경우 — 서버가 거절하면 403·404가 즉시 오고,
    // 열리면 곧바로 열린다. 둘 다 아니면 중간에서 업그레이드를 넘기지 않고 붙들고 있는
    // 것이다. 닫지는 않는다: 늦게라도 열리면 그대로 쓰면 된다.
    const stalled = window.setTimeout(() => {
      if (disposed || socket.readyState !== WebSocket.CONNECTING) return;
      const hint =
        '핸드셰이크가 응답이 없습니다 — 서버가 거절하면 즉시 403/404가 옵니다.' +
        ' 중간(IIS/ARR 등)에서 WebSocket 업그레이드를 넘기지 않을 때의 모양입니다.';
      setDiagnosis(hint);
      term.write(`\r\n\x1b[33m[${hint}]\x1b[0m\r\n`);
    }, HANDSHAKE_TIMEOUT_MS);

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
      disposed = true;
      window.clearTimeout(stalled);
      observer.disconnect();
      input.dispose();
      // 아직 핸드셰이크 중인 소켓을 그냥 닫으면 브라우저가 그것을 **연결 실패**로 기록한다
      // ("WebSocket is closed before the connection is established"). 서버가 거절한 것과
      // 구분되지 않는 문구라, 원인을 서버에서 찾게 만든다. 열린 다음에 닫는다.
      if (socket.readyState === WebSocket.CONNECTING) {
        socket.onopen = () => socket.close();
      } else {
        socket.close();
      }
      term.dispose();
    };
  }, []);

  return (
    <div>
      <p className="mutedtext" style={{ fontSize: 12, marginBottom: 8 }}>
        서버 셸에 PTY로 직접 붙습니다 — 되묻는 명령·Ctrl+C·긴 작업의 실시간 출력이
        그대로 동작합니다. 상태: <b>{status}</b>
      </p>
      {diagnosis && (
        <p className="error" style={{ fontSize: 12, marginBottom: 8 }}>
          {diagnosis}
        </p>
      )}
      <div
        ref={holder}
        style={{ height: '60vh', background: '#11151c', padding: 8, borderRadius: 6 }}
      />
    </div>
  );
}
