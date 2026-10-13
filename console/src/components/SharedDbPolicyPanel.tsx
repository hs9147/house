import { useState } from 'react';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { OrgDataPolicyRow } from '../lib/types';
import Async from './Async';

/**
 * 공용 DB 공유 단위 — 부서마다 "어느 부서와 함께 쓰나"(관리자, services/shareddb).
 *
 * 기본은 자기 부서다. 다른 부서(상위 부서)를 고르면 그 부서와 같은 단위가 되어, 그 단위의 과제끼리
 * 서로의 운영 표를 읽는다. 단위 밖은 그 스키마에 들어가지도 못한다(표 단위로 준 것만 예외).
 * 「분리」는 공유 단위에만 켤 수 있고, 켜면 그 단위만 자기 DB를 쓴다 — 다른 부서에 표 이름도 안 보이는
 * 대신 다른 단위와 묶어 쓸 수 없다. 자료가 생긴 부서의 DB가 바뀌는 설정은 서버가 거부한다.
 */
export default function SharedDbPolicyPanel() {
  const state = useApi(() => api.sharedDbPolicies());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (state.data && !state.data.configured) return null;  // 공용 DB를 쓰지 않는 설치본

  const save = async (row: OrgDataPolicyRow, change: Partial<Pick<OrgDataPolicyRow, 'share_with_org_id' | 'isolated'>>) => {
    setBusy(true);
    setError('');
    try {
      const next = { share_with_org_id: row.share_with_org_id, isolated: row.isolated, ...change };
      if (next.share_with_org_id !== null) next.isolated = false;
      await api.setSharedDbPolicy(row.organization_id, next);
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>공용 DB 공유 단위</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        같은 공유 단위의 과제는 서로의 운영 표를 읽습니다. 기본은 자기 부서이고, 다른 부서를 고르면 그 부서와
        함께 씁니다(데이터는 옮기지 않고 권한만 바뀝니다). 「분리」한 단위는 자기 DB를 써서 다른 부서에 표 이름도
        보이지 않습니다 — 자료가 생긴 뒤에는 분리를 켜고 끌 수 없습니다.
      </p>
      <Async state={state}>
        {(s) => (
          <table>
            <thead>
              <tr><th>부서</th><th>함께 쓰는 부서</th><th>분리</th><th>DB</th><th>스키마</th></tr>
            </thead>
            <tbody>
              {s.policies.map((row) => (
                <tr key={row.organization_id}>
                  <td>{row.name}</td>
                  <td>
                    <select value={row.share_with_org_id ?? ''} disabled={busy}
                      onChange={(e) => void save(row, {
                        share_with_org_id: e.target.value ? Number(e.target.value) : null,
                      })}>
                      <option value="">자기 부서</option>
                      {s.policies.filter((o) => o.organization_id !== row.organization_id).map((o) => (
                        <option key={o.organization_id} value={o.organization_id}>{o.name}</option>
                      ))}
                    </select>
                    {row.share_with_org_id !== null && row.unit_id !== row.share_with_org_id && (
                      <span className="mutedtext" style={{ fontSize: 11 }}> → {row.unit_name}</span>
                    )}
                  </td>
                  <td>
                    <input type="checkbox" checked={row.isolated}
                      disabled={busy || row.share_with_org_id !== null}
                      title={row.share_with_org_id !== null ? '다른 부서와 함께 쓰는 부서는 분리할 수 없습니다' : ''}
                      onChange={(e) => {
                        if (window.confirm(e.target.checked
                          ? `${row.name}(과 함께 쓰는 부서)만 따로 DB를 쓰게 합니다. 다른 부서의 표와 묶어 쓸 수 없게 됩니다.`
                          : `${row.name}의 분리를 풉니다. 기본 DB를 함께 쓰게 됩니다.`)) {
                          void save(row, { isolated: e.target.checked });
                        }
                      }} />
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>{row.db_name}</td>
                  <td className="mono">{row.schemas}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
