import { useState } from 'react';
import Async from '../components/Async';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

export default function Accounts() {
  const accounts = useApi(() => api.listAccounts());
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

  const reject = (id: number, email: string) => {
    if (!confirm(`${email} 계정을 삭제할까요? 발급된 세션도 함께 폐기됩니다.`)) return;
    return act(id, () => api.rejectAccount(id));
  };

  return (
    <div className="panel">
      <h2>계정 승인</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        가입은 신청일 뿐입니다. 관리자가 승인해야 로그인할 수 있고, 삭제하면 이미 발급된
        세션도 함께 폐기됩니다.
      </p>

      {error && <p className="error">{error}</p>}

      <Async state={accounts} empty="등록된 계정이 없습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>이메일</th>
                <th>이름</th>
                <th>상태</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.email}</td>
                  <td>{a.name}</td>
                  <td>
                    <StatusPill value={a.is_approved ? (a.is_admin ? 'admin' : '승인됨') : '승인 대기'} />
                  </td>
                  <td>
                    {!a.is_approved && (
                      <>
                        <button
                          className="small"
                          disabled={busy === a.id}
                          onClick={() => act(a.id, () => api.approveAccount(a.id))}
                        >
                          승인
                        </button>{' '}
                      </>
                    )}
                    <button
                      className="secondary small"
                      disabled={busy === a.id}
                      onClick={() => reject(a.id, a.email)}
                    >
                      {a.is_approved ? '삭제' : '거절'}
                    </button>
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
