import { useState, useRef, useEffect } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';

export default function PowerShellConsole() {
  const [activeTab, setActiveTab] = useState<'console' | 'build_logs' | 'audit_logs'>('build_logs');
  const [connected, setConnected] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [inputCmd, setInputCmd] = useState('');
  const [running, setRunning] = useState(false);
  const logEndRef = useRef<HTMLDivElement>(null);

  // 빌드 로그 (.txt) 탭용 API State
  const buildLogsState = useApi(() => api.listBuildLogs(), []);
  const [selectedFile, setSelectedFile] = useState<string>('');
  const [logContent, setLogContent] = useState<string>('');
  const [tailLines, setTailLines] = useState<number>(1000);
  const [loadingLog, setLoadingLog] = useState<boolean>(false);
  const fileLogEndRef = useRef<HTMLDivElement>(null);

  // 작업 로그 탭용 API hook
  const [auditLimit, setAuditLimit] = useState(100);
  const auditState = useApi(() => api.audit(auditLimit), [auditLimit]);
  const [searchFilter, setSearchFilter] = useState('');

  const scrollToBottom = () => {
    setTimeout(() => {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }, 50);
  };

  useEffect(() => {
    scrollToBottom();
  }, [logs, activeTab, connected]);

  // 빌드 로그 로드 후 자동 스크롤 (파일 끝 기본값 보기)
  useEffect(() => {
    if (logContent) {
      fileLogEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logContent]);

  // 빌드 로그 파일 목록 변경 시 첫 번째 파일 자동 선택
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
      setLogContent(`[Error] 로그 파일을 읽을 수 없습니다: ${(err as Error).message}`);
    } finally {
      setLoadingLog(false);
    }
  };

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
      auditState.reload();
    } catch (err) {
      setLogs((prev) => [...prev, `[Error] ${(err as Error).message}`, 'PS > ']);
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
            ⚡ PowerShell & 빌드 로그 (관리자)
          </h2>
          <p className="mutedtext" style={{ margin: '4px 0 0 0', fontSize: 12 }}>
            PAAS_BUILD_LOG_DIR (.txt) 빌드 로그 및 관리자 권한 PowerShell을 조회/실행합니다.
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

      {/* Tab Nav Selector */}
      <div style={{ display: 'flex', gap: 8, borderBottom: '1px solid rgba(255, 255, 255, 0.1)', paddingBottom: 8 }}>
        <button
          className={activeTab === 'build_logs' ? 'primary small' : 'secondary small'}
          onClick={() => {
            setActiveTab('build_logs');
            buildLogsState.reload();
          }}
        >
          📄 빌드 로그 파일 (PAAS_BUILD_LOG_DIR)
        </button>
        <button
          className={activeTab === 'console' ? 'primary small' : 'secondary small'}
          onClick={() => setActiveTab('console')}
        >
          ⚡ 대화형 콘솔
        </button>
        <button
          className={activeTab === 'audit_logs' ? 'primary small' : 'secondary small'}
          onClick={() => {
            setActiveTab('audit_logs');
            auditState.reload();
          }}
        >
          📋 작업 로그 확인
        </button>
      </div>

      {/* TAB 1: PAAS_BUILD_LOG_DIR (.txt) Files View */}
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
                    fontFamily: 'monospace',
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

      {/* TAB 2: Console View */}
      {activeTab === 'console' && (
        <>
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
        </>
      )}

      {/* TAB 3: Audit Logs View */}
      {activeTab === 'audit_logs' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="row" style={{ alignItems: 'center', gap: 8 }}>
            <input
              type="text"
              placeholder="명령어/주체 키워드 필터..."
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              style={{ maxWidth: 300 }}
            />
            <div className="spacer" />
            <select value={auditLimit} onChange={(e) => setAuditLimit(Number(e.target.value))}>
              <option value={100}>최근 100건</option>
              <option value={250}>최근 250건</option>
              <option value={500}>최근 500건</option>
            </select>
            <button className="secondary small" onClick={auditState.reload}>
              새로고침
            </button>
          </div>

          <Async state={auditState} empty="기록된 작업 로그가 없습니다.">
            {(rows) => {
              const filtered = rows.filter(
                (r) =>
                  !searchFilter.trim() ||
                  r.actor.toLowerCase().includes(searchFilter.toLowerCase()) ||
                  r.action.toLowerCase().includes(searchFilter.toLowerCase()) ||
                  r.target.toLowerCase().includes(searchFilter.toLowerCase())
              );
              return (
                <table>
                  <thead>
                    <tr>
                      <th>시각</th>
                      <th>주체</th>
                      <th>작업 행위</th>
                      <th>명령어 / 대상</th>
                      <th>상세</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((r, i) => (
                      <tr key={i}>
                        <td className="mono">{fmtDate(r.at)}</td>
                        <td>{r.actor}</td>
                        <td>
                          <span
                            style={{
                              fontSize: 11,
                              padding: '2px 6px',
                              borderRadius: 4,
                              background: r.action.startsWith('powershell') ? 'rgba(56, 189, 248, 0.15)' : 'rgba(255, 255, 255, 0.05)',
                              color: r.action.startsWith('powershell') ? '#38bdf8' : 'inherit',
                            }}
                          >
                            {r.action}
                          </span>
                        </td>
                        <td className="mono" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {r.target}
                        </td>
                        <td className="mono" style={{ fontSize: 11 }}>
                          {JSON.stringify(r.detail)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              );
            }}
          </Async>
        </div>
      )}
    </div>
  );
}
