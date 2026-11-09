import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Async from '../../components/Async';
import { api } from '../../lib/api';
import { fmtDate } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import type { LlmTurnDetail } from '../../lib/types';
import { fmt } from '../../lib/viz';

/**
 * 작업 로그 · 추적 — 턴 하나(도구 왕복을 묶은 것)의 호출 흐름.
 *
 * 대시보드가 분포라면 여기는 사건 하나다: 「아쉬움」을 받은 답, 실패한 턴, 토큰을 많이 쓴 턴을 골라
 * 왕복마다 무엇이 입력을 차지했고(지시문·도구 정의·대화·도구 결과), 어느 도구를 불렀고, 어디서 끊기거나
 * 실패했는지를 본다. **본문은 없다**(수치와 이름뿐) — 대화 내용은 그 세션의 참여자만 본다.
 */
const WINDOWS: [number, string][] = [[1, '최근 1일'], [7, '최근 7일'], [30, '최근 30일'], [90, '최근 90일']];

// 실패 신호(services/telemetry.signal) — 응답을 받은 뒤에 안 실패
export const SIGNAL: Record<string, string> = {
  json_unreadable: 'JSON으로 읽지 못함',
  invented_urls: '지어낸 주소를 버림',
  plan_rejected: '동작을 고르지 못함',
  plan_mismatch: '고른 동작이 화면과 다름',
};
const signalLabel = (s: string) => SIGNAL[s] ?? s;

const PART: Record<string, string> = {
  system: '지시문', tools: '도구 정의', history: '대화 이력·첨부', tool_call: '이전 왕복의 도구 호출',
  tool_result: '도구 결과', image: '이미지',
};

export default function TracesTab() {
  const [params, setParams] = useSearchParams();
  const [days, setDays] = useState(7);
  const [label, setLabel] = useState('');
  const [model, setModel] = useState('');
  const [outcome, setOutcome] = useState('');
  const [signal, setSignal] = useState('');
  const [failed, setFailed] = useState(false);
  const turn = params.get('turn') ?? '';
  const state = useApi(() => api.telemetryTurns({ days, label, model, outcome, signal, failed }),
    [days, label, model, outcome, signal, failed]);
  const open = (id: string) => setParams(id ? { turn: id } : {});

  return (
    <>
      {turn && <TurnPanel id={turn} onClose={() => open('')} />}
      <div className="panel">
        <div className="row" style={{ marginBottom: 12, gap: 6, flexWrap: 'wrap' }}>
          <h2 style={{ margin: 0 }}>턴 추적</h2>
          <div className="spacer" />
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {WINDOWS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          {state.data && (
            <>
              <Pick value={label} onChange={setLabel} all="모든 기능" options={state.data.options.labels} />
              <Pick value={model} onChange={setModel} all="모든 모델" options={state.data.options.models} />
              <Pick value={outcome} onChange={setOutcome} all="모든 결과" options={state.data.options.outcomes} />
              <Pick value={signal} onChange={setSignal} all="모든 신호" options={state.data.options.signals}
                label={signalLabel} />
            </>
          )}
          <label style={{ fontSize: 12 }}>
            <input type="checkbox" checked={failed} onChange={(e) => setFailed(e.target.checked)} /> 실패·잘림만
          </label>
          <button className="secondary small" onClick={state.reload}>새로고침</button>
        </div>
        <Async state={state}>
          {(t) => (t.turns.length === 0 ? <p className="mutedtext">조건에 맞는 턴이 없습니다.</p> : (
            <>
              <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
                {fmt(t.total)}턴{t.total > t.turns.length && ` 중 최근 ${fmt(t.turns.length)}턴`}
                {t.truncated && ' · 기간 안의 호출이 많아 최근 것만 셌습니다'} — 줄을 누르면 왕복마다 펼쳐 봅니다.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>시각</th><th>기능</th><th>모델</th><th>결과</th><th>왕복</th><th>시간</th>
                    <th>입력</th><th>출력</th><th>문제</th>
                  </tr>
                </thead>
                <tbody>
                  {t.turns.map((u) => (
                    <tr key={u.turn_id} onClick={() => open(u.turn_id)}
                      style={{ cursor: 'pointer', background: u.turn_id === turn ? 'var(--hover, #f3f4f6)' : undefined }}>
                      <td className="mono">{fmtDate(u.at)}</td>
                      <td className="mono" style={{ fontSize: 11 }}>{u.label}</td>
                      <td style={{ fontSize: 12 }}>{u.models.join(', ')}</td>
                      <td>{u.outcome || '-'}</td>
                      <td>{u.rounds}{u.tool_calls ? ` · 도구 ${u.tool_calls}` : ''}</td>
                      <td>{fmt(u.ms)}ms</td>
                      <td>{fmt(u.prompt_tokens)}{u.cached_tokens ? ` (캐시 ${fmt(u.cached_tokens)})` : ''}</td>
                      <td>{fmt(u.completion_tokens)}</td>
                      <td style={{ fontSize: 12 }}>
                        {u.failed > 0 && <span className="status bad">실패 {u.failed}</span>}{' '}
                        {u.truncated && <span className="status warn">잘림</span>}{' '}
                        {u.signals.map((s) => <span key={s} className="status warn" style={{ marginRight: 4 }}>{signalLabel(s)}</span>)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ))}
        </Async>
      </div>
    </>
  );
}

function Pick({ value, onChange, all, options, label = (s) => s }: {
  value: string; onChange: (v: string) => void; all: string; options: string[]; label?: (s: string) => string;
}) {
  if (options.length === 0) return null;
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} style={{ maxWidth: 220 }}>
      <option value="">{all}</option>
      {options.map((o) => <option key={o} value={o}>{label(o)}</option>)}
    </select>
  );
}

/** 턴 하나 — 왕복마다 입력 구성·불린 도구·종료 사유·오류·신호, 턴의 결과와 읽은 자료, 그 답에 매긴 평가. */
function TurnPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const state = useApi(() => api.telemetryTurn(id), [id]);
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>턴 <span className="mono" style={{ fontSize: 12 }}>{id}</span></h3>
        <div className="spacer" />
        <button className="secondary small" onClick={onClose}>닫기</button>
      </div>
      <Async state={state}>{(d) => <TurnBody d={d} />}</Async>
    </div>
  );
}

function TurnBody({ d }: { d: LlmTurnDetail }) {
  const total = (k: 'ms' | 'prompt_tokens' | 'completion_tokens') => d.rounds.reduce((n, r) => n + (r[k] || 0), 0);
  return (
    <>
      <p style={{ fontSize: 12, marginTop: 0 }}>
        <span className="mono">{d.label}</span> · 왕복 {d.rounds.length} · {fmt(total('ms'))}ms · 입력{' '}
        {fmt(total('prompt_tokens'))} · 출력 {fmt(total('completion_tokens'))}
        {d.outcome && <> · 결과 <b>{d.outcome}</b></>}
        {d.trace_id && <> · 트레이스 <span className="mono">{d.trace_id}</span></>}
      </p>
      {d.ratings.length > 0 && (
        <p style={{ fontSize: 12 }}>
          평가:{' '}
          {d.ratings.map((r, i) => (
            <span key={i} className={`status ${r.score > 0 ? 'ok' : 'bad'}`} style={{ marginRight: 6 }}>
              {r.score > 0 ? '좋음' : '아쉬움'}{r.reason ? ` — ${r.reason}` : ''}
            </span>
          ))}
        </p>
      )}
      <table style={{ fontSize: 12 }}>
        <thead>
          <tr>
            <th>왕복</th><th>모델·판</th><th>시간</th><th>입력(구성, 추정)</th><th>출력</th><th>끝</th>
          </tr>
        </thead>
        <tbody>
          {d.rounds.map((r) => (
            <tr key={r.id}>
              <td>{r.round + 1}</td>
              <td>
                {r.model}
                {r.prompt && <div className="mono mutedtext" style={{ fontSize: 10 }}>{r.prompt}</div>}
              </td>
              <td>{fmt(r.ms)}ms</td>
              <td>
                <b>{fmt(r.prompt_tokens)}</b>{r.cached_tokens ? ` (캐시 ${fmt(r.cached_tokens)})` : ''}
                <div className="mutedtext" style={{ fontSize: 11 }}>
                  {r.parts.filter((p) => p.tokens > 0).sort((a, b) => b.tokens - a.tokens).slice(0, 6)
                    .map((p) => `${PART[p.part] ?? p.part}${p.name ? ` · ${p.name}` : ''} ${fmt(p.tokens)}`).join(' / ')}
                </div>
              </td>
              <td>
                <b>{fmt(r.completion_tokens)}</b>
                <div className="mutedtext" style={{ fontSize: 11 }}>
                  {r.output_parts.filter((p) => p.tokens > 0)
                    .map((p) => `${p.part === 'text' ? '글 답' : p.name || '도구 인자'} ${fmt(p.tokens)}`).join(' / ')}
                  {r.tool_calls > 0 && ` · 도구 ${r.tool_calls}번`}
                </div>
              </td>
              <td>
                {!r.ok && <span className="status bad">실패</span>}{' '}
                {['length', 'max_tokens'].includes((r.finish_reason || '').toLowerCase())
                  ? <span className="status warn">잘림</span> : <span className="mutedtext">{r.finish_reason || '-'}</span>}
                {r.signals.map((s) => <div key={s}><span className="status warn">{signalLabel(s)}</span></div>)}
                {r.error && <div className="mono" style={{ fontSize: 10, wordBreak: 'break-all' }}>{r.error}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {d.reads.length > 0 && (
        <>
          <h4 style={{ fontSize: 13 }}>읽은 자료</h4>
          <table style={{ fontSize: 12 }}>
            <thead><tr><th>출처</th><th>읽음</th><th>결과에 인용</th><th>글자</th><th>인용 안 된 글자</th></tr></thead>
            <tbody>
              {d.reads.map((r) => (
                <tr key={r.source}>
                  <td>{r.source}</td><td>{fmt(r.reads)}</td><td>{fmt(r.cited)}</td>
                  <td>{fmt(r.chars)}</td><td>{fmt(r.uncited_chars)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
