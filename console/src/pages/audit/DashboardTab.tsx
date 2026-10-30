import { useState } from 'react';
import Async from '../../components/Async';
import { api } from '../../lib/api';
import { fmtDate } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import { BarList, StackedBars, StatTile, VIZ, fmt } from '../../lib/viz';

/** 비용 — 작은 값은 센트 아래까지(호출 몇 번이면 몇 센트다). */
const usd = (v: number) => `$${v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(4)}`;

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

// 입력 구간의 이름(services/telemetry.composition).
const PART: Record<string, string> = {
  system: '지시문',
  tools: '도구 정의',
  history: '대화 이력·첨부',
  tool_call: '이전 왕복의 도구 호출',
  tool_result: '도구 결과',
  image: '이미지',
};

// "구간:이름" → 사람이 읽는 이름(api/telemetry._composition의 composition_detail).
function detailLabel(key: string): string {
  const [part, ...rest] = key.split(':');
  return `${PART[part] ?? part} · ${rest.join(':')}`;
}

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
                        sub={`입력 ${fmt(t.totals.prompt_tokens)} · 출력 ${fmt(t.totals.completion_tokens)}`
                          + (t.cached_tokens ? ` · 캐시 ${fmt(t.cached_tokens)}` : '')} />
              {/* 비용은 서버가 직접 단가를 얻은 프로바이더(지금은 Azure 공개 가격)의 호출만 — 나머지는 셈에서 빠진다. */}
              {t.totals.cost_usd !== null && (
                <StatTile label="비용" value={usd(t.totals.cost_usd)}
                          sub={t.totals.priced_calls < t.totals.calls
                            ? `단가를 아는 호출 ${fmt(t.totals.priced_calls)}/${fmt(t.totals.calls)}건만`
                            : 'Azure 공개 가격 기준'} />
              )}
            </div>
            {t.prices.length > 0 && (
              <p className="mutedtext" style={{ fontSize: 11, margin: '-8px 0 12px' }}>
                단가({t.prices[0].unit}, {t.prices[0].source}, {t.prices[0].tier}):{' '}
                {t.prices.map((p) => `${p.provider} 입력 $${p.input} · 캐시 입력 $${p.cached_input} · 출력 $${p.output}`
                  + ` (${p.effective}~)`).join(' / ')}
                {' '}— 배포 유형이 Global Standard가 아니면(Data Zone·Flex 등) 실제 청구와 다를 수 있습니다.
                단가를 직접 얻을 수 없는 프로바이더(Bedrock·사내 LLM 등)의 호출은 비용에 넣지 않습니다.
              </p>
            )}
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
              hint: `실패 ${fmt(m.failed)} · 평균 ${fmt(m.avg_ms)}ms · 토큰 ${fmt(m.tokens)}`
                + (m.cost_usd !== null ? ` · ${usd(m.cost_usd)}` : ''),
            }))} />

            <h3>기능별</h3>
            {/* 요청 경로로 묶는다(숫자 id는 :id로) — "어느 화면이 토큰을 태우나"에 답하는 표다. */}
            <BarList rows={t.by_route.map((r) => ({
              label: r.key,
              value: r.calls,
              hint: `실패 ${fmt(r.failed)} · 평균 ${fmt(r.avg_ms)}ms · 토큰 ${fmt(r.tokens)}`
                + (r.cost_usd !== null ? ` · ${usd(r.cost_usd)}` : ''),
            }))} />

            <h3>입력 구성 <span className="mutedtext" style={{ fontSize: 12 }}>(추정)</span></h3>
            {/* 프로바이더는 입력을 합계로만 준다 — 구간마다 크기를 재 그 비율로 나눈 값이다.
                무엇이 입력을 차지하는지 알아야 줄일 곳(도구 결과·도구 정의)을 고른다. */}
            {t.composition.length === 0 ? (
              <p className="mutedtext">입력 구성 기록이 아직 없습니다 — 이 기능을 올린 뒤의 호출부터 쌓입니다.</p>
            ) : (
              <>
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  합계는 프로바이더가 센 입력 토큰이고, 구간별 몫은 크기 비율로 나눈 추정입니다. 도구 왕복마다
                  앞의 지시문·도구 정의·도구 결과가 다시 실리므로 왕복이 많은 턴일수록 커집니다.
                </p>
                <BarList unit="토큰" rows={t.composition.map((c) => ({
                  label: PART[c.key] ?? c.key,
                  value: c.tokens,
                  hint: pct(c.tokens, t.composition.reduce((n, x) => n + x.tokens, 0)),
                }))} />
              </>
            )}
            {t.composition_detail.length > 0 && (
              <>
                <h3>입력의 세부</h3>
                {/* 사용자가 친 글은 「대화 · 이번 요청」뿐이다 — 나머지는 시스템이 그 요청에 맞춰
                    붙인 맥락(업무·에이전트 목록·부서 워크플로)과 찾아 읽은 자료다. */}
                <BarList unit="토큰" rows={t.composition_detail.map((c) => ({
                  label: detailLabel(c.key), value: c.tokens,
                }))} />
              </>
            )}
            {t.tool_results.length > 0 && (
              <>
                <h3>출처별 도구 결과</h3>
                <BarList unit="토큰" rows={t.tool_results.map((r) => ({ label: r.key, value: r.tokens }))} />
              </>
            )}

            {t.output.length > 0 && (
              <>
                <h3>출력 구성 <span className="mutedtext" style={{ fontSize: 12 }}>(추정)</span></h3>
                {/* 중간 왕복의 출력은 도구 인자라 짧고, 마지막의 결과 화면(show_report) 본문이 길다. */}
                <BarList unit="토큰" rows={t.output.map((o) => ({ label: o.key, value: o.tokens }))} />
                {t.truncated_calls > 0 && (
                  <p className="mutedtext" style={{ fontSize: 12 }}>
                    출력 상한에 걸려 잘린 호출 {fmt(t.truncated_calls)}건 — 그 출력은 원래 더 길었을 값이라
                    아래 비교에서 실제보다 작게 잡힙니다.
                  </p>
                )}
              </>
            )}

            {t.outcomes.length > 0 && (
              <>
                <h3>결과 종류별 출력</h3>
                {/* 같은 결과 종류 안에서, 읽어 들인 자료가 많을수록 출력이 길어지는지를 본다.
                    종류를 섞으면 "보고서는 원래 길다"가 "자료를 많이 읽으면 길어진다"로 보인다.
                    상관이지 인과가 아니다. */}
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  칸은 출력 토큰 중앙값(턴 수)입니다. 열은 그 턴이 읽어 들인 도구 결과의 크기 — 같은 종류
                  안에서의 비교이고, 인과가 아니라 상관입니다.
                </p>
                <table>
                  <thead>
                    <tr>
                      <th>결과</th>
                      <th>턴</th>
                      <th>입력 중앙값</th>
                      <th>출력 중앙값</th>
                      {t.outcomes[0].bins.map((b) => <th key={b.label}>도구 결과 {b.label}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {t.outcomes.map((o) => (
                      <tr key={o.outcome}>
                        <td>{o.outcome}</td>
                        <td>{fmt(o.turns)}</td>
                        <td>{fmt(o.median_input)}</td>
                        <td>{fmt(o.median_output)}</td>
                        {o.bins.map((b) => (
                          <td key={b.label} className={b.turns ? '' : 'mutedtext'}>
                            {b.turns ? `${fmt(b.median_output)} (${b.turns})` : '-'}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            {t.citations.length > 0 && (
              <>
                <h3>읽은 자료의 인용</h3>
                {/* 읽은 문서의 경로·이름이 결과에 나왔는가 — 읽고 쓰지 않은 분량은 입력만 불린 것이다.
                    줄일 곳을 고르는 가장 직접적인 단서다. */}
                <table>
                  <thead>
                    <tr>
                      <th>출처</th>
                      <th>읽은 문서</th>
                      <th>결과에 쓰임</th>
                      <th>쓰이지 않은 분량</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.citations.map((c) => (
                      <tr key={c.source}>
                        <td>{c.source}</td>
                        <td>{fmt(c.reads)}</td>
                        <td>{fmt(c.cited)} ({pct(c.cited, c.reads)})</td>
                        <td>{fmt(c.uncited_chars)}자 ({pct(c.uncited_chars, c.chars)})</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            {t.turns.length > 0 && (
              <>
                <h3>최근 턴</h3>
                {/* 한 번의 요청이 도구 왕복 몇 번으로 끝났고, 왕복마다 입력이 어떻게 불어났으며,
                    그 끝에 출력이 얼마였는지 — 입력이 출력으로 이어지는 모양을 보는 자리다. */}
                <table>
                  <thead>
                    <tr>
                      <th>시각</th>
                      <th>기능</th>
                      <th>결과</th>
                      <th>왕복</th>
                      <th>입력</th>
                      <th>출력</th>
                      <th>왕복별 입력</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.turns.map((u, i) => (
                      <tr key={i}>
                        <td className="mono">{fmtDate(u.at)}</td>
                        <td className="mono" style={{ fontSize: 11 }}>{u.route || '(요청 밖)'}</td>
                        <td>{u.outcome || '-'}</td>
                        <td>{u.rounds}{u.tool_calls ? ` · 도구 ${u.tool_calls}` : ''}</td>
                        <td>{fmt(u.prompt_tokens)}{u.cached_tokens ? ` (캐시 ${fmt(u.cached_tokens)})` : ''}</td>
                        <td>{fmt(u.completion_tokens)}</td>
                        <td className="mono" style={{ fontSize: 11 }}>{u.round_inputs.map(fmt).join(' → ')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

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
