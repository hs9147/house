import { useState } from 'react';
import Async from '../components/Async';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

export default function Accounts() {
  const accounts = useApi(() => api.listAccounts());
  const orgs = useApi(() => api.listOrgs());
  const [filter, setFilter] = useState<'all' | 'pending' | 'approved'>('all');
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

  const addOrgBadge = (accountId: number, orgIdStr: string) => {
    if (!orgIdStr) return;
    return act(accountId, () => api.modifyAccountOrganization(accountId, Number(orgIdStr), 'add'));
  };

  const removeOrgBadge = (accountId: number, orgId: number, orgName: string) => {
    if (!confirm(`해당 계정에서 '🏢 ${orgName}' 조직 소속 권한을 삭제하시겠습니까?`)) return;
    return act(accountId, () => api.modifyAccountOrganization(accountId, orgId, 'remove'));
  };

  const deleteAccount = (id: number, email: string) => {
    if (!confirm(`정말로 계정 '${email}'을(를) 삭제하시겠습니까?\n발급된 로그인 세션 및 권한이 즉시 폐기됩니다.`)) return;
    return act(id, () => api.rejectAccount(id));
  };

  const orgList = orgs.data ?? [];

  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h2 style={{ margin: 0 }}>👤 계정 관리 (Account Management)</h2>
        <p className="mutedtext" style={{ fontSize: 12, marginTop: 4 }}>
          등록된 계정 목록을 관리하며 계정별 소속 조직 뱃지를 추가 및 삭제 관리합니다.
        </p>
      </div>

      {error && (
        <div style={{ padding: 10, borderRadius: 6, background: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', border: '1px solid rgba(239, 68, 68, 0.3)', fontSize: 13 }}>
          ⚠️ {error}
        </div>
      )}

      <Async state={accounts} empty="등록된 계정이 없습니다.">
        {(rows) => {
          const pendingCount = rows.filter((a) => !a.is_approved).length;
          const approvedCount = rows.filter((a) => a.is_approved).length;

          const filteredRows = rows.filter((a) => {
            if (filter === 'pending') return !a.is_approved;
            if (filter === 'approved') return a.is_approved;
            return true;
          });

          return (
            <>
              {/* 필터 버튼 그룹 */}
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <button
                  className={filter === 'all' ? 'primary small' : 'secondary small'}
                  onClick={() => setFilter('all')}
                >
                  👥 전체 계정 ({rows.length})
                </button>
                <button
                  className={filter === 'pending' ? 'primary small' : 'secondary small'}
                  onClick={() => setFilter('pending')}
                >
                  ⏳ 승인 대기 ({pendingCount})
                </button>
                <button
                  className={filter === 'approved' ? 'primary small' : 'secondary small'}
                  onClick={() => setFilter('approved')}
                >
                  ✅ 승인 완료 ({approvedCount})
                </button>
              </div>

              {/* 승인요청 포함 전체 계정 통합 목록 테이블 */}
              <table style={{ width: '100%', marginTop: 8 }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: 'left' }}>이메일</th>
                    <th style={{ textAlign: 'left' }}>이름</th>
                    <th style={{ textAlign: 'left' }}>소속 조직 뱃지 목록 (추가/삭제)</th>
                    <th style={{ textAlign: 'left' }}>승인 / 역할 상태</th>
                    <th style={{ textAlign: 'right' }}>관리 Action</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredRows.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="mutedtext" style={{ textAlign: 'center', padding: 24 }}>
                        조건에 일치하는 계정이 존재하지 않습니다.
                      </td>
                    </tr>
                  ) : (
                    filteredRows.map((a) => {
                      const userOrgs = a.organizations && a.organizations.length > 0
                        ? a.organizations
                        : (a.organization_id && a.organization_name ? [{ id: a.organization_id, name: a.organization_name }] : []);

                      const unassignedOrgs = orgList.filter((o) => !userOrgs.some((uo) => uo.id === o.id));

                      return (
                        <tr
                          key={a.id}
                          style={{
                            background: !a.is_approved ? 'rgba(239, 68, 68, 0.05)' : undefined,
                          }}
                        >
                          <td className="mono">
                            {a.email}
                            {!a.is_approved && (
                              <span style={{
                                marginLeft: 6,
                                fontSize: 10,
                                padding: '1px 5px',
                                borderRadius: 4,
                                background: 'rgba(245, 158, 11, 0.2)',
                                color: '#f59e0b',
                              }}>
                                신규 요청
                              </span>
                            )}
                          </td>
                          <td>{a.name}</td>
                          <td>
                            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                              {userOrgs.length === 0 ? (
                                <span className="mutedtext" style={{ fontSize: 11 }}>소속 미지정</span>
                              ) : (
                                userOrgs.map((o) => (
                                  <span
                                    key={o.id}
                                    style={{
                                      fontSize: 11,
                                      padding: '2px 8px',
                                      borderRadius: 12,
                                      background: 'rgba(56, 189, 248, 0.15)',
                                      color: '#38bdf8',
                                      border: '1px solid rgba(56, 189, 248, 0.3)',
                                      display: 'inline-flex',
                                      alignItems: 'center',
                                      gap: 4,
                                    }}
                                  >
                                    🏢 {o.name}
                                    <button
                                      type="button"
                                      disabled={busy === a.id}
                                      style={{
                                        border: 'none',
                                        background: 'none',
                                        color: '#ef4444',
                                        cursor: 'pointer',
                                        padding: '0 2px',
                                        fontSize: 10,
                                        fontWeight: 'bold',
                                        lineHeight: 1,
                                      }}
                                      title="소속 조직 뱃지 삭제"
                                      onClick={() => removeOrgBadge(a.id, o.id, o.name)}
                                    >
                                      ✕
                                    </button>
                                  </span>
                                ))
                              )}

                              {unassignedOrgs.length > 0 && (
                                <select
                                  style={{ fontSize: 11, padding: '2px 4px', borderRadius: 4 }}
                                  value=""
                                  disabled={busy === a.id}
                                  onChange={(e) => addOrgBadge(a.id, e.target.value)}
                                >
                                  <option value="">➕ 소속 조직 추가...</option>
                                  {unassignedOrgs.map((o) => (
                                    <option key={o.id} value={o.id}>
                                      🏢 {o.name}
                                    </option>
                                  ))}
                                </select>
                              )}
                            </div>
                          </td>
                          <td>
                            <StatusPill value={a.is_approved ? (a.is_admin ? 'admin' : '승인됨') : '승인 대기'} />
                          </td>
                          <td style={{ textAlign: 'right' }}>
                            {!a.is_approved && (
                              <button
                                className="primary small"
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
                      );
                    })
                  )}
                </tbody>
              </table>
            </>
          );
        }}
      </Async>
    </div>
  );
}
