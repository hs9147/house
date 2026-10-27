import { useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { fmtBytes, fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { ScheduledJobRow, StatusSnapshot, TopConsumer } from '../lib/types';

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

const KIND: Record<string, string> = { app: '앱', platform: '플랫폼', process: '프로세스' };

/** 주요 점유 항목 — 게이지 아래 한 줄씩. 막대는 CPU·메모리는 전체 대비, 폴더는 가장 큰 폴더 대비다. */
function TopList({ rows }: { rows: { key: string; kind: string; label: string; value: string; share: number; note?: string }[] }) {
  if (!rows.length) return null;
  return (
    <table style={{ fontSize: 12, margin: '-6px 0 14px', width: '100%' }}>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <td style={{ width: 64 }}><span className="status dim">{KIND[r.kind] ?? r.kind}</span></td>
            <td title={r.note}>{r.label}{r.note ? <span className="mutedtext"> · {r.note}</span> : null}</td>
            <td style={{ width: 160 }}>
              <div className="gauge" style={{ height: 6 }}><div style={{ width: `${Math.min(r.share, 100)}%` }} /></div>
            </td>
            <td className="mono" style={{ width: 110, textAlign: 'right' }}>{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const procNote = (c: TopConsumer) => (c.count > 1 ? `프로세스 ${c.count}개` : undefined);

function CpuTop({ s }: { s: StatusSnapshot }) {
  return <TopList rows={(s.top?.cpu ?? []).map((c) => ({
    key: c.kind + c.label, kind: c.kind, label: c.label, value: `${c.cpu.toFixed(1)}%`, share: c.cpu, note: procNote(c),
  }))} />;
}

function MemoryTop({ s }: { s: StatusSnapshot }) {
  const total = s.memory?.total || 1;
  return <TopList rows={(s.top?.memory ?? []).map((c) => ({
    key: c.kind + c.label, kind: c.kind, label: c.label, value: fmtBytes(c.memory),
    share: (c.memory / total) * 100, note: procNote(c),
  }))} />;
}

/** 디스크 — 드라이브마다 사용률, 그리고 플랫폼이 쌓는 폴더(앱 배포 폴더마다·로그·저장소·색인)의 크기. */
function DiskTop({ s }: { s: StatusSnapshot }) {
  // 위의 "디스크"(플랫폼이 깔린 드라이브)와 같은 드라이브는 다시 보이지 않는다.
  const disks = (s.top?.disks ?? []).filter((d) => !(d.total === s.disk?.total && d.used === s.disk?.used));
  const folders = s.top?.folders;
  // 폴더는 드라이브가 서로 다를 수 있어 가장 큰 폴더 대비로 막대를 그린다.
  const biggest = Math.max(1, ...(folders?.items ?? []).map((f) => f.bytes));
  return (
    <>
      {disks.map((d) => (
        <Gauge key={d.mount} label={`드라이브 ${d.mount}`} percent={d.percent}
          detail={`${fmtBytes(d.used)} / ${fmtBytes(d.total)}`} />
      ))}
      {folders && (
        <>
          <div className="mutedtext" style={{ fontSize: 12, margin: '-6px 0 8px' }}>
            플랫폼 폴더 크기
            {folders.measuring ? ' — 재는 중입니다. 잠시 뒤 새로고침하세요'
              : folders.measured_at ? ` — ${fmtDate(folders.measured_at)}에 잼(10분마다 다시)` : ''}
          </div>
          <TopList rows={folders.items.map((f) => ({
            key: f.path, kind: f.kind, label: f.label, share: (f.bytes / biggest) * 100,
            value: `${fmtBytes(f.bytes)}${f.complete ? '' : '+'}`,
            note: f.complete ? f.path : `${f.path} (항목이 많아 일부만 잼 — 실제는 더 크다)`,
          }))} />
        </>
      )}
    </>
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
              {/* 실제 구성을 그대로 말한다. 예전에는 OS만 보고 만든 문구라 런타임이
                  windows_service(Docker 미사용)여도 Docker 라이선스 안내가 뜨고, GPU가
                  없는 서버에서 "GPU 지원"이라 하면서 바로 아래 "GPU 없음"과 어긋났다. */}
              {s.host_os && (
                <p className="mutedtext" style={{ marginTop: 0 }}>
                  운영환경: <span className="status info">{s.host_os}</span>
                  {s.runtime_backend && <> · 런타임 <span className="mono">{s.runtime_backend}</span></>}
                  {s.proxy_backend && <> · 프록시 <span className="mono">{s.proxy_backend}</span></>}
                  {' '}· GPU 배정 {s.gpu_supported ? '가능' : '불가'}
                  {s.docker_hint && <> · {s.docker_hint}</>}
                </p>
              )}
              {s.system && <p className="mutedtext">{s.system}</p>}
              {s.cpu_percent !== undefined && <Gauge label="CPU" percent={s.cpu_percent} />}
              <CpuTop s={s} />
              {s.memory && (
                <Gauge
                  label="메모리"
                  percent={s.memory.percent}
                  detail={`${fmtBytes(s.memory.used)} / ${fmtBytes(s.memory.total)}`}
                />
              )}
              <MemoryTop s={s} />
              {s.disk && (
                <Gauge
                  label="디스크"
                  percent={s.disk.percent}
                  detail={`${fmtBytes(s.disk.used)} / ${fmtBytes(s.disk.total)}`}
                />
              )}
              <DiskTop s={s} />
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
