import { useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { fmtBytes } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { ApiKeyIssued } from '../lib/types';

function Gauge({ label, percent, detail }: { label: string; percent: number; detail?: string }) {
  const cls = percent >= 90 ? 'bad' : percent >= 70 ? 'warn' : '';
  return (
    <div style={{ marginBottom: 14 }}>
      <div className="row" style={{ marginBottom: 4 }}>
        <span>{label}</span>
        <div className="spacer" />
        <span className="mono mutedtext">
          {percent.toFixed(0)}%{detail ? ` · ${detail}` : ''}
        </span>
      </div>
      <div className="gauge">
        <div className={cls} style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
    </div>
  );
}

export default function Dashboard() {
  const state = useApi(() => api.status());

  return (
    <>
      <div className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>시스템 상태</h2>
          <div className="spacer" />
          <button className="secondary small" onClick={state.reload}>
            새로고침
          </button>
        </div>
        <Async state={state}>
          {(s) => (
            <>
              {s.host_os && (
                <p className="mutedtext" style={{ marginTop: 0 }}>
                  운영환경: <span className="status info">{s.host_os}</span>{' '}
                  {s.docker_hint} · GPU {s.gpu_supported ? '지원' : '미지원'}
                </p>
              )}
              {s.system && <p className="mutedtext">{s.system}</p>}
              {s.cpu_percent !== undefined && <Gauge label="CPU" percent={s.cpu_percent} />}
              {s.memory && (
                <Gauge
                  label="메모리"
                  percent={s.memory.percent}
                  detail={`${fmtBytes(s.memory.used)} / ${fmtBytes(s.memory.total)}`}
                />
              )}
              {s.disk && (
                <Gauge
                  label="디스크"
                  percent={s.disk.percent}
                  detail={`${fmtBytes(s.disk.used)} / ${fmtBytes(s.disk.total)}`}
                />
              )}
              {s.gpus.length > 0 ? (
                <table style={{ marginTop: 8 }}>
                  <thead>
                    <tr>
                      <th>GPU</th>
                      <th>이름</th>
                      <th>VRAM</th>
                      <th>사용률</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.gpus.map((g) => (
                      <tr key={g.index}>
                        <td className="mono">#{g.index}</td>
                        <td>{g.name}</td>
                        <td style={{ minWidth: 220 }}>
                          <Gauge
                            label=""
                            percent={(g.vram_used / g.vram_total) * 100}
                            detail={`${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}`}
                          />
                        </td>
                        <td className="mono">{g.util_percent}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="mutedtext">GPU 없음 (또는 NVML 미설치)</p>
              )}
            </>
          )}
        </Async>
      </div>
      <KeyIssuePanel />
    </>
  );
}

function KeyIssuePanel() {
  const [name, setName] = useState('');
  const [admin, setAdmin] = useState(false);
  const [issued, setIssued] = useState<ApiKeyIssued | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    setIssued(null);
    try {
      setIssued(await api.issueKey(name.trim(), admin));
      setName('');
      setAdmin(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>API 키 발급</h2>
      <form className="row" onSubmit={submit}>
        <input
          placeholder="키 이름 (예: ci-bot)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          style={{ width: 220 }}
        />
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />
          admin 권한
        </label>
        <button type="submit" disabled={busy}>
          {busy ? '발급 중...' : '발급'}
        </button>
      </form>
      {issued && (
        <p style={{ color: 'var(--yellow)', fontSize: 13 }}>
          지금만 표시됩니다 — 안전한 곳에 보관하세요:{' '}
          <span className="mono" style={{ userSelect: 'all' }}>{issued.key}</span>
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
