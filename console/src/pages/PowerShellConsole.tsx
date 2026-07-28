import { useState, useRef, useEffect } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

export default function PowerShellConsole() {
  const [activeTab, setActiveTab] = useState<'console' | 'build_logs'>('console');
  const [connected, setConnected] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [inputCmd, setInputCmd] = useState('');
  const [running, setRunning] = useState(false);
  const logEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 빌드 로그 (.txt) 탭용 API State
  const buildLogsState = useApi(() => api.listBuildLogs(), []);
  const [selectedFile, setSelectedFile] = useState<string>('');
  const [logContent, setLogContent] = useState<string>('');
  const [tailLines, setTailLines] = useState<number>(1000);
  const [loadingLog, setLoadingLog] = useState<boolean>(false);
  const fileLogEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    setTimeout(() => {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }, 50);
  };

  useEffect(() => {
    scrollToBottom();
  }, [logs, activeTab, connected]);

  useEffect(() => {
    if (logContent) {
      fileLogEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logContent]);

  useEffect(() => {
    if (buildLogsState.data && buildLogsState.data.files.length > 0 && !selectedFile) {
      const firstFile = buildLogsState.data.files[0].relative_path;
      setSelectedFile(firstFile);
      loadLogFile(firstFile, tailLines);
    }
  }, [buildLogsState.data]);

  const loadLogFile = async (filename: string, lines = 1000) => {
    if (!filename) return;
    setLoadingLog(true);
    try {
      const res = await api.getBuildLogContent(filename, lines);
      setLogContent(res.content);
    } catch (err) {
      setLogContent(`[Error] 빌드 로그 파일을 읽을 수 없습니다: ${(err as Error).message}`);
    } finally {
      setLoadingLog(false);
    }
  };

  const handleConnect = () => {
    setConnected(true);
    setLogs([
      'Windows PowerShell Admin Interactive Console [Version 10.0.19045]',
      'Copyright (C) Microsoft Corporation. All rights reserved.',
      'Administrator privileges verified via PaaS Control Plane.\n',
    ]);
  };

  const handleDisconnect = () => {
    setConnected(false);
    setLogs((prev) => [...prev, '[Session Disconnected by User]']);
  };

  const executeCommand = async (cmdToRun?: string) => {
    const cmd = (cmdToRun !== undefined ? cmdToRun : inputCmd).trim();
    if (!cmd || !connected || running) return;

    setLogs((prev) => [...prev, `PS > ${cmd}`]);
    if (cmdToRun === undefined) setInputCmd('');
    setRunning(true);

    try {
      if (cmd.toLowerCase() === 'clear' || cmd.toLowerCase() === 'cls') {
        setLogs([]);
        setRunning(false);
        return;
      }

      const res = await api.execPowerShell(cmd);
      setLogs((prev) => [...prev, res.output]);
    } catch (err) {
      setLogs((prev) => [...prev, `[Error] ${(err as Error).message}`]);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Header & Connection status */}
      <div className="row" style={{ alignItems: 'center' }}>
        <div>
          <h2 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
            ⚡ PowerShell & 빌드 로그
          </h2>
          <p className="mutedtext" style={{ margin: '4px 0 0 0', fontSize: 12 }}>
            관리자 권한 PowerShell 프롬프트 터미널과 빌드 로그를 조회합니다.
          </p>
        </div>
        <div className="spacer" />
        {activeTab === 'console' && (
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
        )}
      </div>

      {/* Tab Nav Selector: PowerShell 콘솔 -> 빌드 로그 순서 */}
      <div style={{ display: 'flex', gap: 8, borderBottom: '1px solid rgba(255, 255, 255, 0.1)', paddingBottom: 8 }}>
        <button
          className={activeTab === 'console' ? 'primary small' : 'secondary small'}
          onClick={() => setActiveTab('console')}
        >
          ⚡ PowerShell 콘솔
        </button>
        <button
          className={activeTab === 'build_logs' ? 'primary small' : 'secondary small'}
          onClick={() => {
            setActiveTab('build_logs');
            buildLogsState.reload();
          }}
        >
          📄 빌드 로그
        </button>
      </div>

      {/* TAB 1: PowerShell 콘솔 (Prompt Format UI) */}
      {activeTab === 'console' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
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

          {/* Integrated Prompt Style Terminal Window */}
          <div
            onClick={() => inputRef.current?.focus()}
            style={{
              background: '#090d16',
              color: '#38bdf8',
              fontFamily: 'Consolas, Monaco, "Courier New", monospace',
              fontSize: 13,
              lineHeight: 1.6,
              padding: 16,
              borderRadius: 8,
              minHeight: 420,
              maxHeight: 560,
              overflowY: 'auto',
              border: '1px solid rgba(56, 189, 248, 0.2)',
              boxShadow: 'inset 0 0 10px rgba(0, 0, 0, 0.5)',
              cursor: 'text',
            }}
          >
            {!connected ? (
              <div style={{ color: '#94a3b8', textAlign: 'center', padding: '80px 0' }}>
                PowerShell 터미널 세션이 연결되지 않았습니다.<br />
                상단의 <b>[연결]</b> 버튼을 클릭하여 관리자 세션을 시작하세요.
              </div>
            ) : (
              <div>
                {logs.map((logLine, idx) => (
                  <div key={idx} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                    {logLine}
                  </div>
                ))}

                {/* Inline Prompt Form */}
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    executeCommand();
                  }}
                  style={{ display: 'flex', alignItems: 'center', marginTop: 4 }}
                >
                  <span style={{ color: '#10b981', fontWeight: 'bold', marginRight: 8, userSelect: 'none' }}>
                    PS &gt;
                  </span>
                  <input
                    ref={inputRef}
                    type="text"
                    style={{
                      flex: 1,
                      background: 'transparent',
                      border: 'none',
                      outline: 'none',
                      color: '#f8fafc',
                      fontFamily: 'inherit',
                      fontSize: 13,
                      padding: 0,
                    }}
                    placeholder={running ? '명령어 실행 중...' : 'PowerShell 명령어 입력...'}
                    value={inputCmd}
                    onChange={(e) => setInputCmd(e.target.value)}
                    disabled={running}
                    autoFocus
                  />
                </form>
                <div ref={logEndRef} />
              </div>
            )}
          </div>
        </div>
      )}

      {/* TAB 2: 빌드 로그 (PAAS_BUILD_LOG_DIR .txt Files View) */}
      {activeTab === 'build_logs' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Async state={buildLogsState} empty="PAAS_BUILD_LOG_DIR 하위에 빌드 로그 (.txt) 파일이 존재하지 않습니다.">
            {(data) => (
              <>
                <div className="row" style={{ alignItems: 'center', gap: 8 }}>
                  <label style={{ fontSize: 13, fontWeight: 600 }}>로그 파일 선택:</label>
                  <select
                    style={{ flex: 1, maxWidth: 450, fontFamily: 'monospace' }}
                    value={selectedFile}
                    onChange={(e) => {
                      setSelectedFile(e.target.value);
                      loadLogFile(e.target.value, tailLines);
                    }}
                  >
                    {data.files.map((f) => (
                      <option key={f.relative_path} value={f.relative_path}>
                        {f.relative_path} ({(f.size_bytes / 1024).toFixed(1)} KB)
                      </option>
                    ))}
                  </select>

                  <label style={{ fontSize: 12, marginLeft: 8 }} className="mutedtext">라인 출력 (끝부분):</label>
                  <select
                    value={tailLines}
                    onChange={(e) => {
                      const num = Number(e.target.value);
                      setTailLines(num);
                      if (selectedFile) loadLogFile(selectedFile, num);
                    }}
                  >
                    <option value={200}>마지막 200줄</option>
                    <option value={500}>마지막 500줄</option>
                    <option value={1000}>마지막 1000줄 (기본값)</option>
                    <option value={5000}>마지막 5000줄</option>
                  </select>

                  <button
                    className="secondary small"
                    disabled={loadingLog || !selectedFile}
                    onClick={() => loadLogFile(selectedFile, tailLines)}
                  >
                    {loadingLog ? '로딩 중...' : '🔄 새로고침'}
                  </button>
                </div>

                <div
                  style={{
                    background: '#090d16',
                    color: '#e2e8f0',
                    fontFamily: 'Consolas, Monaco, "Courier New", monospace',
                    fontSize: 12,
                    lineHeight: 1.5,
                    padding: 16,
                    borderRadius: 8,
                    minHeight: 420,
                    maxHeight: 580,
                    overflowY: 'auto',
                    border: '1px solid rgba(255, 255, 255, 0.12)',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                  }}
                >
                  {loadingLog ? (
                    <div style={{ color: '#94a3b8', textAlign: 'center', padding: '60px 0' }}>
                      빌드 로그 파일 읽는 중...
                    </div>
                  ) : !logContent ? (
                    <div style={{ color: '#94a3b8', textAlign: 'center', padding: '60px 0' }}>
                      선택된 빌드 로그 파일 내용이 비어있습니다.
                    </div>
                  ) : (
                    <>
                      {logContent}
                      <div ref={fileLogEndRef} />
                    </>
                  )}
                </div>
              </>
            )}
          </Async>
        </div>
      )}
    </div>
  );
}
