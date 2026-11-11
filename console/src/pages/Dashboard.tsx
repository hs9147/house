import { useState } from 'react';
import Async from '../components/Async';
import Modal from '../components/Modal';
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
  const [tempOpen, setTempOpen] = useState(false);
  const [storageOpen, setStorageOpen] = useState(false);

  return (
    <>
      <div className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>시스템 상태</h2>
          <div className="spacer" />
          <button className="secondary small" onClick={() => setStorageOpen(true)}>내부 저장소</button>{' '}
          <button className="secondary small" onClick={() => setTempOpen(true)}>임시 작업</button>{' '}
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
              {s.backend && <BackendInstance b={s.backend} />}
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
      {tempOpen && <TempJobsModal onClose={() => setTempOpen(false)} />}
      {storageOpen && <InternalStorageModal onClose={() => setStorageOpen(false)} />}
    </>
  );
}

/** 내부 저장소(PAAS_STORAGE_ROOT) — 플랫폼이 실제로 쓰는 파일(.infosource)과 그 밖의 맨 위 항목. 옮길 때 무엇을
 *  가져가야 하는지, 문서 폴더와 겹치는지를 본다. */
function InternalStorageModal({ onClose }: { onClose: () => void }) {
  const state = useApi(() => api.internalStorage());
  return (
    <Modal title="내부 저장소 (PAAS_STORAGE_ROOT)" onClose={onClose} wide>
      <Async state={state}>
        {(u) => (
          <>
            <p style={{ marginTop: 0 }}>
              <span className="mono">{u.root}</span>{!u.exists && <span className="status bad" style={{ marginLeft: 6 }}>폴더 없음</span>}
            </p>
            <p className="mutedtext" style={{ fontSize: 12 }}>
              플랫폼이 파일을 쓰는 자리입니다(정보 업데이트의 받은 파일·캡처·실패 기록·조건 칸 동작·목록 누적본 —
              <span className="mono"> .infosource</span>). 문서 저장소가 아니며 검색·색인 대상도 아닙니다. 옮길 때는 GPAX를 멈추고
              <span className="mono"> .infosource</span>를 새 위치로 옮긴 뒤 다시 시작합니다.
            </p>
            {u.overlaps.length > 0 && (
              <div className="panel" style={{ borderColor: '#f59e0b' }}>
                <strong style={{ color: '#f59e0b' }}>
                  ⚠️ 문서 저장소 {u.overlaps.length}개와 겹칩니다
                </strong>
                <div className="mutedtext" style={{ fontSize: 12, margin: '4px 0' }}>
                  겹치면 이 아래에 새 저장소를 등록할 수 없고, internal 창구로 그 폴더의 파일을 고칠 수 있게 됩니다.
                  문서 폴더와 겹치지 않는 전용 폴더로 옮기세요.
                </div>
                <div style={{ fontSize: 12 }}>
                  {u.overlaps.map((o) => (
                    <span key={o.store} style={{ marginRight: 10 }}>
                      <b>{o.store}</b> <span className="mono mutedtext">{o.root}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
            <h4 style={{ margin: '12px 0 4px' }}>
              플랫폼이 쓰는 파일 — {u.platform_files.toLocaleString()}개 · {fmtBytes(u.platform_bytes)}
            </h4>
            {u.groups.length === 0 ? <p className="mutedtext">없습니다.</p> : (
              <table style={{ fontSize: 12 }}>
                <thead><tr><th>출처</th><th>용도</th><th>파일</th><th>크기</th><th>최근 수정</th></tr></thead>
                <tbody>
                  {u.groups.map((g) => (
                    <tr key={`${g.purpose}-${g.source_id}`}>
                      <td>{g.source || '-'}</td>
                      <td>
                        <details>
                          <summary style={{ cursor: 'pointer' }}>{g.purpose}</summary>
                          <ul className="mono" style={{ fontSize: 11, margin: '4px 0', paddingLeft: 16 }}>
                            {g.files.map((f) => (
                              <li key={f.path} style={{ wordBreak: 'break-all' }}>
                                {f.path} <span className="mutedtext">· {fmtBytes(f.size)} · {fmtDate(f.modified)}</span>
                              </li>
                            ))}
                            {g.count > g.files.length && <li className="mutedtext">… 외 {g.count - g.files.length}개</li>}
                          </ul>
                        </details>
                      </td>
                      <td>{g.count.toLocaleString()}</td>
                      <td>{fmtBytes(g.bytes)}</td>
                      <td>{g.modified ? fmtDate(g.modified) : '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <h4 style={{ margin: '12px 0 4px' }}>그 밖의 항목 — 플랫폼이 쓰지 않습니다</h4>
            <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
              맨 위의 폴더·파일 이름만 보입니다(폴더 크기는 재지 않습니다). 예전에 internal 창구로 올린 것이거나, 이 아래에 둔 다른 폴더입니다.
            </p>
            {u.others.length === 0 ? <p className="mutedtext">없습니다.</p> : (
              <table style={{ fontSize: 12 }}>
                <thead><tr><th>이름</th><th>종류</th><th>문서 저장소</th><th>크기</th></tr></thead>
                <tbody>
                  {u.others.map((o) => (
                    <tr key={o.name}>
                      <td className="mono">{o.name}</td>
                      <td>{o.trash ? '휴지통' : o.dir ? '폴더' : '파일'}</td>
                      <td>{o.store ? <span className="status warn">{o.store}</span> : '-'}</td>
                      <td>{o.size !== null ? fmtBytes(o.size) : '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {u.others_truncated && <p className="mutedtext" style={{ fontSize: 12 }}>앞의 200개만 보입니다.</p>}
          </>
        )}
      </Async>
    </Modal>
  );
}

/** 임시 작업 — 코드에 둔 운영 스크립트의 현황(마지막 실행·결과·활성)과 이력(한 일이 있을 때만). 활성/비활성·지금 실행. */
function TempJobsModal({ onClose }: { onClose: () => void }) {
  const state = useApi(() => api.tempJobs());
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError('');
    try {
      await fn();
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };
  return (
    <Modal title="임시 작업" onClose={onClose} wide>
      <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
        한동안만 돌리는 운영 스크립트입니다(코드에 있습니다 — 다 쓰면 코드에서 지웁니다). 활성이면 일이 생길 때 바로
        돌고 스케줄러가 주기마다 놓친 것을 줍습니다 — 비활성이면 둘 다 멈춥니다(「지금 실행」은 그래도 됩니다).
        이력은 실제로 무언가를 처리했을 때만 남습니다.
      </p>
      {error && <p className="error">{error}</p>}
      <Async state={state}>
        {(s) => (s.tasks.length === 0 ? <p className="mutedtext">임시 작업이 없습니다.</p> : (
          <>
            {s.tasks.map((t) => (
              <div key={t.id} style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 10, marginBottom: 10 }}>
                <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                  <b>{t.title}</b>
                  {t.job && (t.job.enabled ? <span className="status ok">활성</span> : <span className="status dim">비활성</span>)}
                  {t.job?.last_status === 'failed' && <span className="status bad">실패 {t.job.consecutive_failures}회</span>}
                  <div className="spacer" />
                  {t.job && (
                    <button className="small secondary" disabled={!!busy}
                      title={t.job.enabled ? '바로 실행·주기 실행을 모두 멈춥니다' : '바로 실행·주기 실행을 다시 켭니다'}
                      onClick={() => void act(t.id, () => api.toggleScheduledJob(t.job!.id))}>
                      {t.job.enabled ? '비활성으로' : '활성으로'}
                    </button>
                  )}
                  <button className="small" disabled={!!busy} onClick={() => void act(t.id, () => api.runTempJob(t.id))}>
                    {busy === t.id ? '실행 중…' : '지금 실행'}
                  </button>
                </div>
                <div className="mutedtext" style={{ fontSize: 12, margin: '4px 0' }}>{t.description}</div>
                <div className="mutedtext" style={{ fontSize: 12 }}>
                  {t.events.length > 0 && <>바로 도는 때: <span className="mono">{t.events.join(', ')}</span> · </>}
                  주기 {t.job ? fmtInterval(t.job.interval_seconds) : '—'}
                  {' '}· 마지막 실행 {t.job?.last_run_at ? fmtDate(t.job.last_run_at) : '아직 없음'}
                  {t.job?.last_detail && <> — <span className="mono">{fmtDetail(t.job.last_detail)}</span></>}
                </div>
                <h4 style={{ fontSize: 13, margin: '8px 0 4px' }}>이력 ({t.history.length})</h4>
                {t.history.length === 0 ? (
                  <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>아직 처리한 것이 없습니다.</p>
                ) : (
                  <table style={{ fontSize: 12 }}>
                    <thead><tr><th>시각</th><th>계기</th><th>결과</th></tr></thead>
                    <tbody>
                      {t.history.map((h, i) => (
                        <tr key={i}>
                          <td style={{ whiteSpace: 'nowrap' }}>{fmtDate(h.at)}</td>
                          <td className="mono">{h.actor}</td>
                          <td className="mono" style={{ wordBreak: 'break-all' }}>{fmtDetail(h.detail)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            ))}
          </>
        ))}
      </Async>
    </Modal>
  );
}

function fmtUptime(sec: number): string {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}일 ${h}시간` : h ? `${h}시간 ${m}분` : `${m}분`;
}

/** 백엔드 인스턴스 — 하나로 버티는지 보는 숫자. 요청 스레드가 상한에 가까우면 긴 LLM 대화가 다른 요청을 밀어낸다. */
function BackendInstance({ b }: { b: NonNullable<StatusSnapshot['backend']> }) {
  const t = b.request_threads;
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 13, marginBottom: 4 }}>
        <b>백엔드 인스턴스</b> {b.instances}개
        <span className="mutedtext" style={{ fontSize: 12 }}>
          {' '}— 정기 작업·진행 상태가 프로세스 안에 있어 하나로 운영합니다(늘리려면 그것들을 DB로 옮기는 작업이 먼저)
        </span>
      </div>
      {t && <Gauge label="요청 스레드" percent={(t.busy / t.limit) * 100} detail={`${t.busy} / ${t.limit}`} />}
      <p className="mutedtext" style={{ fontSize: 12, margin: '4px 0' }}>
        답을 만드는 LLM 대화 {b.llm_turns}개 · 뒤에서 도는 내용 분석 {b.background.analysis}개·제안 작업 {b.background.tasks}개
        {' '}· 스케줄러 {b.scheduler ? '동작 중' : '멈춤'}
        {b.memory !== undefined && <> · 메모리 {fmtBytes(b.memory)}</>}
        {b.threads !== undefined && <> · 스레드 {b.threads}개</>}
        {b.uptime_seconds !== undefined && <> · 가동 {fmtUptime(b.uptime_seconds)}</>}
        {' '}· PID <span className="mono">{b.pid}</span>
      </p>
    </div>
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
