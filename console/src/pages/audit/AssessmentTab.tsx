import { useState } from 'react';
import Async from '../../components/Async';
import { api } from '../../lib/api';
import { fmtDate } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import { StackedBars, StatTile, VIZ, fmt } from '../../lib/viz';

/**
 * 작업 로그 · 평가 — 사람이 매긴 답변 평가(answer_ratings).
 *
 * **모델이 자기 답을 채점하면 측정이 아니라 자기 보고다.** 그래서 점수는 쓰는 사람이
 * 대화 화면에서 누른 좋음·아쉬움뿐이고, 이 탭은 그것을 모델별로 모아 보여 준다.
 *
 * 비율만으로 줄 세우지 않는다 — 2건 중 2건 좋음인 모델이 1위가 되면, 적은 표본으로
 * 모델을 바꾸는 결정을 하게 된다. 그래서 막대의 길이는 **건수**고, 좋음 비율은 그 옆에
 * 글자로 붙인다. 누가 매겼는지는 서버가 내려보내지 않는다(사람 수만) — 이름이 보이면
 * 솔직한 평가가 줄고, 그러면 측정 자체가 망가진다.
 */
const WINDOWS: [number, string][] = [[7, '최근 7일'], [30, '최근 30일'], [90, '최근 90일']];

export default function AssessmentTab() {
  const [days, setDays] = useState(30);
  const state = useApi(() => api.qualityTelemetry(days), [days]);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>답변 평가</h2>
        <div className="spacer" />
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {WINDOWS.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <button className="secondary small" onClick={state.reload}>새로고침</button>
      </div>
      <Async state={state}>
        {(q) => (q.totals.rated === 0 ? (
          <p className="mutedtext">
            이 기간에 매겨진 평가가 없습니다 — 스마트워크 대화의 답변 아래
            「좋음 · 아쉬움」을 누르면 여기 쌓입니다.
          </p>
        ) : (
          <>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
              <StatTile label="평가" value={fmt(q.totals.rated)} sub={`${q.days}일`} />
              <StatTile label="좋음" value={pct(q.totals.good, q.totals.rated)}
                        sub={`${fmt(q.totals.good)}건`} />
              <StatTile label="아쉬움" value={pct(q.totals.poor, q.totals.rated)}
                        sub={`${fmt(q.totals.poor)}건`} />
              <StatTile label="평가한 사람" value={fmt(q.totals.raters)}
                        sub="이름은 남기지 않습니다" />
            </div>

            <h3>모델별</h3>
            {/* 막대 길이는 건수다 — 표본이 적은 모델이 비율로 1위가 되지 않게. */}
            <StackedBars
              rows={q.by_model.map((m) => ({
                label: m.key,
                parts: [
                  { key: '좋음', value: m.good, color: VIZ.status.good },
                  { key: '아쉬움', value: m.poor, color: VIZ.status.critical },
                ],
              }))}
              legend={[
                { key: 'good', label: '좋음', color: VIZ.status.good, mark: '■' },
                { key: 'poor', label: '아쉬움', color: VIZ.status.critical, mark: '■' },
              ]}
            />
            <table style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>모델</th>
                  <th>평가</th>
                  <th>좋음</th>
                </tr>
              </thead>
              <tbody>
                {q.by_model.map((m) => (
                  <tr key={m.key}>
                    <td>{m.key}</td>
                    <td>{fmt(m.rated)}건</td>
                    <td>
                      {pct(m.good, m.rated)}
                      {/* 표본이 적으면 비율을 믿지 말라고 그 자리에 적는다. */}
                      {m.rated < 10 && <span className="mutedtext"> (표본 적음)</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {q.recent_poor.length > 0 && (
              <>
                <h3>아쉬움의 사유</h3>
                {/* 고칠 거리는 여기 있다 — 좋음에는 보통 아무 말도 안 적는다. */}
                <table>
                  <thead>
                    <tr>
                      <th>시각</th>
                      <th>모델</th>
                      <th>사유</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.recent_poor.map((r, i) => (
                      <tr key={i}>
                        <td className="mono">{fmtDate(r.at)}</td>
                        <td>{r.model || '-'}</td>
                        <td>{r.reason}</td>
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
  return `${Math.round((part / whole) * 100)}%`;
}
