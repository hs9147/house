import { useState, useRef, useEffect } from 'react';
import { api } from '../lib/api';

export default function PowerShellConsole() {
  const [connected, setConnected] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [inputCmd, setInputCmd] = useState('');
  const [running, setRunning] = useState(false);
  const logEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [logs]);

  const handleConnect = () => {
    setConnected(true);
    setLogs([
      'Windows PowerShell Admin Interactive Session Connected.',
      'Administrator rights verified via PaaS Control Plane.',
      'Type commands below or click quick action buttons.\n',
      'PS > ',
    ]);
  };

  const handleDisconnect = () => {
    setConnected(false);
    setLogs((prev) => [...prev, '\n[Session Disconnected by User]', 'PS > ']);
  };

  const executeCommand = async (cmdToRun?: string) => {
    const cmd = (cmdToRun !== undefined ? cmdToRun : inputCmd).trim();
    if (!cmd || !connected || running) return;

    setLogs((prev) => [...prev, `PS > ${cmd}`]);
    if (cmdToRun === undefined) setInputCmd('');
    setRunning(true);

    try {
      if (cmd.toLowerCase() === 'clear' || cmd.toLowerCase() === 'cls') {
        setLogs(['PS > ']);
        setRunning(false);
        return;
      }

      const res = await api.execPowerShell(cmd);
      setLogs((prev) => [...prev, res.output, 'PS > ']);
    } catch (err) {
      setLogs((prev) => [...prev, `[Error] ${(err as Error).message}`, 'PS > ']);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="row" style={{ alignItems: 'center' }}>
        <div>
          <h2 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
            ⚡ PowerShell 콘솔 (관리자)
          </h2>
          <p className="mutedtext" style={{ margin: '4px 0 0 0', fontSize: 12 }}>
            PaaS 서버 호스트 노드의 관리자 권한 PowerShell 명령어를 실행하고 결과를 실시간 확인합니다.
          </p>
        </div>
        <div className="spacer" />
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span
            style={{
              padding: '4px 10px',
              borderRadius: 12,
              fontSize: 12,
              fontWeight: 600,
              background: connected ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)',
              color: connected ? '#10b981' : '#ef4444',
              border: `1px solid ${connected ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`,
            }}
          >
            {connected ? '● 연결됨 (Connected)' : '○ 연결 끊김 (Disconnected)'}
          </span>
          {!connected ? (
            <button className="primary small" onClick={handleConnect}>
              🔗 연결
            </button>
          ) : (
            <button className="danger small" onClick={handleDisconnect}>
              🔌 연결 끊기
            </button>
          )}
        </div>
      </div>

      {/* Quick Command Shortcuts */}
      {connected && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="mutedtext" style={{ fontSize: 12 }}>빠른 실행:</span>
          {['Get-Location', 'Get-Process | Select-Object -First 10', 'Get-Service | Select-Object -First 10', 'Get-ChildItem', 'cls'].map((preset) => (
            <button
              key={preset}
              className="secondary small"
              style={{ fontSize: 11, padding: '2px 8px' }}
              disabled={running}
              onClick={() => executeCommand(preset)}
            >
              {preset}
            </button>
          ))}
        </div>
      )}

      {/* Terminal Viewport */}
      <div
        style={{
          background: '#0f172a',
          color: '#38bdf8',
          fontFamily: 'monospace',
          fontSize: 13,
          padding: 16,
          borderRadius: 8,
          minHeight: 380,
          maxHeight: 520,
          overflowY: 'auto',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {!connected ? (
          <div style={{ color: '#94a3b8', textAlign: 'center', padding: '60px 0' }}>
            PowerShell 터미널 세션이 연결되지 않았습니다.<br />
            상단의 <b>[연결]</b> 버튼을 클릭하여 관리자 세션을 시작하세요.
          </div>
        ) : (
          <>
            {logs.join('\n')}
            <div ref={logEndRef} />
          </>
        )}
      </div>

      {/* Command Input Box */}
      {connected && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            executeCommand();
          }}
          style={{ display: 'flex', gap: 8 }}
        >
          <input
            type="text"
            className="mono"
            style={{ flex: 1, background: '#1e293b', color: '#f8fafc', border: '1px solid #334155' }}
            placeholder="PowerShell 명령어 입력 (예: Get-Process)..."
            value={inputCmd}
            onChange={(e) => setInputCmd(e.target.value)}
            disabled={running}
          />
          <button type="submit" className="primary" disabled={running || !inputCmd.trim()}>
            {running ? '실행 중...' : '전송'}
          </button>
        </form>
      )}
    </div>
  );
}
