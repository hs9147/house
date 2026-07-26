import { useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';

export default function Audit() {
  const [limit, setLimit] = useState(100);
  const state = useApi(() => api.audit(limit), [limit]);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>감사 로그</h2>
        <div className="spacer" />
        <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          <option value={100}>최근 100건</option>
          <option value={250}>최근 250건</option>
          <option value={500}>최근 500건</option>
        </select>
        <button className="secondary small" onClick={state.reload}>
          새로고침
        </button>
      </div>
      <Async state={state} empty="기록이 없습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>시각</th>
                <th>주체</th>
                <th>행위</th>
                <th>대상</th>
                <th>상세</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td className="mono">{fmtDate(r.at)}</td>
                  <td>{r.actor}</td>
                  <td className="mono">{r.action}</td>
                  <td>{r.target}</td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {r.detail ? JSON.stringify(r.detail) : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
    </div>
  );
}
