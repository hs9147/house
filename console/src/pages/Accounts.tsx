import { useState } from 'react';
import Async from '../components/Async';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

export default function Accounts() {
  const accounts = useApi(() => api.listAccounts());
  const orgs = useApi(() => api.listOrgs());
  const [activeTab, setActiveTab] = useState<'list' | 'pending'>('list');
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState('');

  const act = async (id: number, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError('');
    try {
      await fn();
      accounts.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(0);
    }
  };

  const updateOrg = (accountId: number, orgIdStr: string) => {
    const orgId = orgIdStr ? Number(orgIdStr) : null;
    return act(accountId, () => api.updateAccountOrganization(accountId, orgId));
  };

  const deleteAccount = (id: number, email: string) => {
    if (!confirm(`정말로 계정 '${email}'을(를) 삭제하시겠습니까?\n해당 계정에 발급된 세션 및 권한이 즉시 폐기됩니다.`)) return;
    return act(id, () => api.rejectAccount(id));
  };

  const rejectAccountRequest = (id: number, email: string) => {
    if (!confirm(`'${email}' 계정의 승인 요청을 거절하고 삭제하시겠습니까?`)) return;
    return act(id, () => api.rejectAccount(id));
  };

  const orgList = orgs.data ?? [];

  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h2 style={{ margin: 0 }}>👤 계정 관리 (Account Management)</h2>
        <p className="mutedtext" style={{ fontSize: 12, marginTop: 4 }}>
          계정 목록 및 승인 대기 요청을 관리하고 계정별 소속 조직을 지정합니다.
        </p>
      </div>

      {error && (
        <div style={{ padding: 10, borderRadius: 6, background: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', border: '1px solid rgba(239, 68, 68, 0.3)', fontSize: 13 }}>
          ⚠️ {error}
        </div>
      )}

      <Async state={accounts} empty="등록된 계정이 없습니다.">
        {(rows) => {
          const pendingRows = rows.filter((a) => !a.is_approved);

          return (
            <>
              {/* Tab navigation */}
              <div style={{ display: 'flex', gap: 8, borderBottom: '1px solid rgba(255, 255, 255, 0.1)', paddingBottom: 8 }}>
                <button
                  className={activeTab === 'list' ? 'primary small' : 'secondary small'}
                  onClick={() => setActiveTab('list')}
                >
                  👥 계정 목록 조회 ({rows.length})
                </button>
                <button
                  className={activeTab === 'pending' ? 'primary small' : 'secondary small'}
                  onClick={() => setActiveTab('pending')}
                  style={{ position: 'relative' }}
                >
                  ✅ 계정 승인 요청 {pendingRows.length > 0 && (
                    <span style={{
                      marginLeft: 6,
                      padding: '1px 6px',
                      borderRadius: 10,
                      fontSize: 11,
                      background: '#ef4444',
                      color: '#fff',
                    }}>
                      {pendingRows.length}
                    </span>
                  )}
                </button>
              </div>

              {/* Tab 1: 계정 목록 조회 */}
              {activeTab === 'list' && (
                <div>
                  <table style={{ width: '100%', marginTop: 8 }}>
                    <thead>
                      <tr>
                        <th style={{ textAlign: 'left' }}>이메일</th>
                        <th style={{ textAlign: 'left' }}>이름</th>
                        <th style={{ textAlign: 'left' }}>소속 조직 설정</th>
                        <th style={{ textAlign: 'left' }}>역할 / 상태</th>
                        <th style={{ textAlign: 'right' }}>관리 Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.length === 0 ? (
                        <tr>
                          <td colSpan={5} className="mutedtext" style={{ textAlign: 'center', padding: 24 }}>
                            등록된 계정이 존재하지 않습니다.
                          </td>
                        </tr>
                      ) : (
                        rows.map((a) => (
                          <tr key={a.id}>
                            <td className="mono">{a.email}</td>
                            <td>{a.name}</td>
                            <td>
                              <select
                                style={{ fontSize: 12, padding: '4px 6px' }}
                                value={a.organization_id ?? ''}
                                disabled={busy === a.id}
                                onChange={(e) => updateOrg(a.id, e.target.value)}
                              >
                                <option value="">🏢 미지정 (전역)</option>
                                {orgList.map((o) => (
                                  <option key={o.id} value={o.id}>
                                    🏢 {o.name}
                                  </option>
                                ))}
                              </select>
                            </td>
                            <td>
                              <StatusPill value={a.is_approved ? (a.is_admin ? 'admin' : '승인됨') : '승인 대기'} />
                            </td>
                            <td style={{ textAlign: 'right' }}>
                              {!a.is_approved && (
                                <button
                                  className="small"
                                  disabled={busy === a.id}
                                  onClick={() => act(a.id, () => api.approveAccount(a.id))}
                                  style={{ marginRight: 6 }}
                                >
                                  ✅ 승인
                                </button>
                              )}
                              <button
                                className="danger small"
                                disabled={busy === a.id}
                                onClick={() => deleteAccount(a.id, a.email)}
                              >
                                🗑️ 계정 삭제
                              </button>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Tab 2: 계정 승인 요청 */}
              {activeTab === 'pending' && (
                <div>
                  <table style={{ width: '100%', marginTop: 8 }}>
                    <thead>
                      <tr>
                        <th style={{ textAlign: 'left' }}>이메일</th>
                        <th style={{ textAlign: 'left' }}>이름</th>
                        <th style={{ textAlign: 'left' }}>소속 조직 설정</th>
                        <th style={{ textAlign: 'left' }}>상태</th>
                        <th style={{ textAlign: 'right' }}>승인 처리</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pendingRows.length === 0 ? (
                        <tr>
                          <td colSpan={5} className="mutedtext" style={{ textAlign: 'center', padding: 24 }}>
                            🎉 승인 대기 중인 계정 요청이 없습니다.
                          </td>
                        </tr>
                      ) : (
                        pendingRows.map((a) => (
                          <tr key={a.id}>
                            <td className="mono">{a.email}</td>
                            <td>{a.name}</td>
                            <td>
                              <select
                                style={{ fontSize: 12, padding: '4px 6px' }}
                                value={a.organization_id ?? ''}
                                disabled={busy === a.id}
                                onChange={(e) => updateOrg(a.id, e.target.value)}
                              >
                                <option value="">🏢 미지정 (전역)</option>
                                {orgList.map((o) => (
                                  <option key={o.id} value={o.id}>
                                    🏢 {o.name}
                                  </option>
                                ))}
                              </select>
                            </td>
                            <td>
                              <StatusPill value="승인 대기" />
                            </td>
                            <td style={{ textAlign: 'right' }}>
                              <button
                                className="primary small"
                                disabled={busy === a.id}
                                onClick={() => act(a.id, () => api.approveAccount(a.id))}
                                style={{ marginRight: 6 }}
                              >
                                ✅ 승인
                              </button>
                              <button
                                className="danger small"
                                disabled={busy === a.id}
                                onClick={() => rejectAccountRequest(a.id, a.email)}
                              >
                                ❌ 거절 (삭제)
                              </button>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          );
        }}
      </Async>
    </div>
  );
}
