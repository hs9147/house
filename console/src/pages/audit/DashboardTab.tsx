import { useState } from 'react';
import Async from '../../components/Async';
import { api } from '../../lib/api';
import { fmtDate } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import { BarList, StackedBars, StatTile, VIZ, fmt } from '../../lib/viz';

/**
 * 작업 로그 · 대시보드 — 모델 호출의 숫자(llm_calls).
 *
 * **셈은 서버가 한다**(api/telemetry.py). 화면은 받은 집계를 그리기만 한다 — 같은 질문에
 * 화면마다 다른 답이 나오지 않게 하려는 것이고, 창이 넓어져도 느려지지 않는다.
 *
 * 무엇을 먼저 보여 줄지: 첫 줄은 "지금 괜찮은가"에 답하는 네 숫자다(호출 수·실패·p50·p95).
 * 평균 지연은 쓰지 않는다 — 긴 꼬리 하나가 평균을 끌고 가고, 사람이 느끼는 것은 분포다.
 * 그 다음이 "어디가 문제인가"(모델별·경로별)이고, 마지막이 "무엇을 고치나"(실패 사유)다.
 */
const WINDOWS: [number, string][] = [[1, '최근 1일'], [7, '최근 7일'], [30, '최근 30일']];

export default function DashboardTab() {
  const [days, setDays] = useState(7);
  const state = useApi(() => api.llmTelemetry(days), [days]);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>LLM 호출</h2>
        <div className="spacer" />
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {WINDOWS.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <button className="secondary small" onClick={state.reload}>새로고침</button>
      </div>
      <Async state={state}>
        {(t) => (t.totals.calls === 0 ? (
          <p className="mutedtext">
            이 기간에 기록된 모델 호출이 없습니다 — 대화나 기획을 한 번 돌리면 쌓입니다.
          </p>
        ) : (
          <>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
              <StatTile label="호출" value={fmt(t.totals.calls)}
                        sub={`도구 왕복 포함 · ${t.days}일`} />
              <StatTile label="실패" value={fmt(t.totals.failed)}
                        sub={`${pct(t.totals.failed, t.totals.calls)} 실패율`} />
              <StatTile label="지연 p50" value={`${fmt(t.totals.p50_ms)}ms`}
                        sub={`p95 ${fmt(t.totals.p95_ms)}ms`} />
              <StatTile label="토큰" value={fmt(t.totals.prompt_tokens + t.totals.completion_tokens)}
                        sub={`입력 ${fmt(t.totals.prompt_tokens)} · 출력 ${fmt(t.totals.completion_tokens)}`} />
            </div>
            {t.truncated && (
              <p className="mutedtext">
                행이 너무 많아 최근 구간만 셌습니다 — 기간을 좁혀 보세요.
              </p>
            )}

            <h3>일별</h3>
            <StackedBars
              rows={t.daily.map((d) => ({
                label: d.key.slice(5),  // MM-DD — 연도는 기간 선택이 이미 말한다
                parts: [
                  { key: '성공', value: d.calls - d.failed, color: VIZ.series },
                  { key: '실패', value: d.failed, color: VIZ.status.critical },
                ],
              }))}
              legend={[
                { key: 'ok', label: '성공', color: VIZ.series, mark: '■' },
                { key: 'failed', label: '실패', color: VIZ.status.critical, mark: '■' },
              ]}
            />

            <h3>모델별</h3>
            <BarList rows={t.by_model.map((m) => ({
              label: m.key,
              value: m.calls,
              hint: `실패 ${fmt(m.failed)} · 평균 ${fmt(m.avg_ms)}ms · 토큰 ${fmt(m.tokens)}`,
            }))} />

            <h3>기능별</h3>
            {/* 요청 경로로 묶는다(숫자 id는 :id로) — "어느 화면이 토큰을 태우나"에 답하는 표다. */}
            <BarList rows={t.by_route.map((r) => ({
              label: r.key,
              value: r.calls,
              hint: `실패 ${fmt(r.failed)} · 평균 ${fmt(r.avg_ms)}ms · 토큰 ${fmt(r.tokens)}`,
            }))} />

            {t.failures.length > 0 && (
              <>
                <h3>최근 실패</h3>
                {/* 실패율만 보면 무엇을 고칠지 알 수 없다 — 사유를 그 자리에 둔다. */}
                <table>
                  <thead>
                    <tr>
                      <th>시각</th>
                      <th>모델</th>
                      <th>경로</th>
                      <th>사유</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.failures.map((f, i) => (
                      <tr key={i}>
                        <td className="mono">{fmtDate(f.at)}</td>
                        <td>{f.model}</td>
                        <td className="mono" style={{ fontSize: 11 }}>
                          {f.route || '-'}
                          {f.path && f.path !== 'chat.completions' && ` (${f.path})`}
                        </td>
                        <td className="mono" style={{ fontSize: 11 }}>{f.error || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        ))}
      </Async>
    </div>
  );
}

function pct(part: number, whole: number): string {
  if (whole <= 0) return '0%';
  const ratio = (part / whole) * 100;
  // 작은 실패율을 0%로 뭉개지 않는다 — 1000건에 3건은 "없다"와 다르다.
  return `${ratio < 10 ? ratio.toFixed(1) : Math.round(ratio)}%`;
}
