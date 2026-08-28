import { useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { fmtBytes, fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { ApiKeyIssued, ScheduledJobRow } from '../lib/types';

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
      <SchedulerPanel />
      <KeyIssuePanel />
    </>
  );
}

function fmtInterval(sec: number): string {
  if (sec % 3600 === 0) return `${sec / 3600}시간`;
  if (sec % 60 === 0) return `${sec / 60}분`;
  return `${sec}초`;
}

/** 결과 상세를 한 줄로. 0과 null은 뺀다 — "added=0 updated=0"은 읽을 것이 없다. */
function fmtDetail(detail: Record<string, unknown> | null): string {
  if (!detail) return '';
  const text = Object.entries(detail)
    .filter(([, v]) => v !== null && v !== 0 && v !== '')
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(' ');
  return text.length > 140 ? `${text.slice(0, 140)}…` : text;
}

function JobStatusBadge({ job }: { job: ScheduledJobRow }) {
  if (!job.enabled) return <span className="status dim">꺼짐</span>;
  if (job.last_status === 'failed') {
    return <span className="status bad">실패 {job.consecutive_failures}회</span>;
  }
  if (job.last_run_at === null) return <span className="status dim">대기</span>;
  if (job.last_status === 'skipped') return <span className="status dim">변경 없음</span>;
  return <span className="status ok">정상</span>;
}

function SchedulerPanel() {
  const state = useApi(() => api.schedulerSnapshot());
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState('');

  const act = async (id: number, fn: (jobId: number) => Promise<unknown>) => {
    setBusy(id);
    setError('');
    try {
      await fn(id);
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(0);
    }
  };

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>주기 갱신</h2>
        <div className="spacer" />
        <button className="secondary small" onClick={state.reload}>
          새로고침
        </button>
      </div>
      <Async state={state}>
        {(s) => (
          <>
            <p className="mutedtext" style={{ marginTop: 0 }}>
              스케줄러{' '}
              <span className={`status ${s.running ? 'ok' : 'bad'}`}>
                {s.running ? '동작 중' : '멈춤'}
              </span>{' '}
              · {s.tick_seconds}초마다 확인 · 실패 {s.failing}건 · 미실행 {s.never_run}건
            </p>
            <table>
              <thead>
                <tr>
                  <th>작업</th>
                  <th>주기</th>
                  <th>마지막 실행</th>
                  <th>결과</th>
                  <th>동작</th>
                </tr>
              </thead>
              <tbody>
                {s.jobs.map((j) => (
                  <tr key={j.id}>
                    <td className="mono">{j.name}</td>
                    <td className="mutedtext">{fmtInterval(j.interval_seconds)}</td>
                    <td>
                      {fmtDate(j.last_run_at)}
                      {j.last_run_at && j.overdue && j.enabled && (
                        <span className="status warn" style={{ marginLeft: 6 }}>
                          밀림
                        </span>
                      )}
                    </td>
                    <td>
                      <JobStatusBadge job={j} />
                      {j.last_ms !== null && (
                        <span className="mutedtext" style={{ marginLeft: 6 }}>
                          {j.last_ms}ms
                        </span>
                      )}
                      {fmtDetail(j.last_detail) && (
                        <div className="mono mutedtext">{fmtDetail(j.last_detail)}</div>
                      )}
                    </td>
                    <td>
                      <div className="row" style={{ gap: 6 }}>
                        <button
                          className="secondary small"
                          disabled={busy === j.id || !j.enabled}
                          onClick={() => act(j.id, api.runScheduledJob)}
                        >
                          {busy === j.id ? '실행 중...' : '지금 실행'}
                        </button>
                        <button
                          className="secondary small"
                          disabled={busy === j.id}
                          onClick={() => act(j.id, api.toggleScheduledJob)}
                        >
                          {j.enabled ? '끄기' : '켜기'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {s.jobs.length === 0 && <p className="mutedtext">갱신할 대상이 없습니다.</p>}
            {error && <p className="error">{error}</p>}
          </>
        )}
      </Async>
    </div>
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
