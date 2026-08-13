import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import AutoDeployMark from '../../components/AutoDeployMark';
import DeployProgressModal from '../../components/DeployProgressModal';
import { Confirm } from '../../components/Modal';
import StatusPill from '../../components/StatusPill';
import { api, ApiError } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { BuildProfile } from '../../lib/types';
import type { ProjectContext } from '../ProjectDetail';

type Action = { kind: 'deploy' | 'rollback' | 'stop'; profile: BuildProfile } | null;

export default function OverviewTab() {
  const { project } = useOutletContext<ProjectContext>();
  const state = useApi(() => api.projectStatus(project.id), [project.id]);
  const health = useApi(() => api.health());
  const [gitSha, setGitSha] = useState('');
  const [action, setAction] = useState<Action>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  // 배포는 서버구성 화면과 동일하게 큐(비블로킹)로 요청하고, 받은 레코드 id를
  // 진행 로그 모달에 넘겨 폴링으로 보여준다(DeployProgressModal).
  const [deployFor, setDeployFor] = useState<{ ids: number[]; profile: BuildProfile } | null>(null);

  const run = async () => {
    if (!action) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (action.kind === 'deploy') {
        const result = await api.deployQueued(project.id, action.profile, gitSha.trim() || undefined);
        const records = Array.isArray(result) ? result : [result];
        setDeployFor({ ids: records.map((r) => r.id), profile: action.profile });
      } else if (action.kind === 'rollback') {
        const d = await api.rollback(project.id, action.profile);
        setMessage(`롤백 완료 — ${d.image_tag}`);
        state.reload();
      } else {
        await api.stop(project.id, action.profile);
        setMessage(`${action.profile} 중지됨`);
        state.reload();
      }
    } catch (e) {
      const err = e as ApiError;
      setMessage('');
      setError(err.status === 409 ? '이미 배포가 진행 중입니다. 잠시 후 다시 시도하세요.' : err.message);
    } finally {
      setBusy(false);
      setAction(null);
    }
  };

  const labels: Record<string, string> = {
    deploy: '배포', rollback: '롤백', stop: '중지',
  };

  return (
    <>
      <div className="panel">
        <h2>실행 상태</h2>
        <Async state={state}>
          {(status) => (
            <table>
              <thead>
                <tr>
                  <th>프로필</th>
                  <th>상태</th>
                  <th>자동배포</th>
                  <th>도메인</th>
                  <th style={{ width: 260 }}>동작</th>
                </tr>
              </thead>
              <tbody>
                {(['release', 'development'] as BuildProfile[]).map((profile) => {
                  const orgSegment = project.org_name || '_';
                  const pathUrl = profile === 'release'
                    ? `/apps/${orgSegment}/${project.name}/`
                    : `/apps/${orgSegment}/${project.name}/dev/`;
                  // base_domain을 아직 못 불러왔으면(로딩 중) 경로만 보여준다 —
                  // 잘못된 호스트로 링크를 만드는 것보다 안전하다.
                  // 스킴을 https로 박아 두면 80포트만 여는 구성에서 죽은 링크가 된다.
                  // 공개 주소가 설정돼 있으면 그 스킴을, 없으면 콘솔 자신이 열린 스킴을
                  // 쓴다 — 배포된 앱은 콘솔과 같은 프록시 뒤에 있다.
                  const scheme = health.data?.public_scheme
                    ?? window.location.protocol.replace(':', '');
                  const fullUrl = health.data ? `${scheme}://${health.data.base_domain}${pathUrl}` : pathUrl;
                  return (
                    <tr key={profile}>
                      <td><StatusPill value={profile} /></td>
                      <td><StatusPill value={status[profile] ?? 'unknown'} /></td>
                      <td><AutoDeployMark status={status[profile] ?? ''} branch={project.branch} /></td>
                      <td className="mono">
                        {health.data && status[profile] === 'running' ? (
                          <a
                            href={fullUrl}
                            target="_blank"
                            rel="noreferrer"
                            // target="_blank"만으로는 대부분의 브라우저가 새 "탭"을 연다.
                            // 새 창으로 띄우려면 창 속성을 지정해 window.open을 불러야 한다.
                            // href는 그대로 둔다 — 가운데 클릭·주소 복사 같은 링크 동작을
                            // 잃지 않고, 팝업이 막혀도 링크로는 열린다.
                            onClick={(e) => {
                              e.preventDefault();
                              window.open(fullUrl, '_blank', 'noopener,noreferrer,popup=yes');
                            }}
                          >
                            {fullUrl}
                          </a>
                        ) : (
                          fullUrl
                        )}
                      </td>
                      <td>
                        <div className="row">
                          <button
                            className="small"
                            disabled={busy}
                            onClick={() => setAction({ kind: 'deploy', profile })}
                          >
                            배포
                          </button>
                          <button
                            className="small secondary"
                            disabled={busy}
                            onClick={() => setAction({ kind: 'rollback', profile })}
                          >
                            롤백
                          </button>
                          <button
                            className="small danger"
                            disabled={busy}
                            onClick={() => setAction({ kind: 'stop', profile })}
                          >
                            중지
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Async>
        <div className="row" style={{ marginTop: 12 }}>
          <label className="field">
            특정 커밋 배포 (선택, git SHA)
            <input
              className="mono"
              value={gitSha}
              onChange={(e) => setGitSha(e.target.value)}
              placeholder="비우면 브랜치 최신"
              style={{ width: 320 }}
            />
          </label>
        </div>
        {message && <p style={{ color: 'var(--green)' }}>{message}</p>}
        {error && <p className="error">{error}</p>}
      </div>
      {action && (
        <Confirm
          title={`${action.profile} ${labels[action.kind]}`}
          message={`${project.name}에 ${action.profile} 프로필로 "${labels[action.kind]}"을(를) 실행합니다.`}
          confirmLabel={labels[action.kind]}
          danger={action.kind === 'stop'}
          busy={busy}
          onConfirm={run}
          onClose={() => !busy && setAction(null)}
        />
      )}
      {deployFor && (
        <DeployProgressModal
          projectId={project.id}
          projectName={project.name}
          profile={deployFor.profile}
          deploymentIds={deployFor.ids}
          onClose={() => {
            setDeployFor(null);
            state.reload();
          }}
        />
      )}
    </>
  );
}
