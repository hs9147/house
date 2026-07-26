import { useState } from 'react';
import Async from '../components/Async';
import DeployProgressModal from '../components/DeployProgressModal';
import Modal from '../components/Modal';
import StatusPill from '../components/StatusPill';
import TopologyDiagram from '../components/TopologyDiagram';
import { api, ApiError } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { BuildProfile, RedirectRule } from '../lib/types';

export default function ServerConfig() {
  const state = useApi(() => api.serverConfig());
  const [rulesFor, setRulesFor] = useState<{ id: number; name: string } | null>(null);
  const [deployFor, setDeployFor] =
    useState<{ projectId: number; ids: number[]; name: string; profile: BuildProfile } | null>(null);
  const [busyKey, setBusyKey] = useState('');
  const [error, setError] = useState('');
  const [showTopology, setShowTopology] = useState(true);

  // 배포는 큐(비블로킹)로 한 번만 요청하고, 받은 레코드 id를 진행 로그 모달에 넘겨
  // 폴링으로 진행 상황을 보여준다.
  const startDeploy = async (projectId: number, name: string, profile: BuildProfile) => {
    setBusyKey(`${projectId}-${profile}-deploy`);
    setError('');
    try {
      const result = await api.deployQueued(projectId, profile);
      const records = Array.isArray(result) ? result : [result];
      setDeployFor({ projectId, ids: records.map((r) => r.id), name, profile });
    } catch (e) {
      const err = e as ApiError;
      setError(err.status === 409 ? '이미 배포가 진행 중입니다. 잠시 후 다시 시도하세요.' : err.message);
    } finally {
      setBusyKey('');
    }
  };

  const stopProject = async (projectId: number, profile: BuildProfile) => {
    setBusyKey(`${projectId}-${profile}-stop`);
    setError('');
    try {
      await api.stop(projectId, profile);
      state.reload();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusyKey('');
    }
  };

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>서버구성</h2>
        <div className="spacer" />
        {state.data && (
          <>
            <span className="status info" title="실행 런타임">
              runtime: {state.data.runtime_backend}
            </span>
            <span className="status info" title="리버스프록시">
              proxy: {state.data.proxy_backend}
            </span>
            <button className="small secondary" onClick={() => setShowTopology((v) => !v)}>
              {showTopology ? '다이어그램 숨기기' : '다이어그램 보기'}
            </button>
          </>
        )}
      </div>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        프로젝트별 라우팅(URL)·실행 상태·리다이렉트 규칙 수를 한눈에 봅니다. 배포는
        기본적으로 서브패스로 구성됩니다 — <code>/apps/조직/프로젝트/</code>(development는
        그 아래 <code>/dev/</code>), 조직이 없는 프로젝트는 <code>/apps/_/프로젝트/</code>.
        커스텀 도메인을 지정한 release 배포만 예외로 그 도메인 루트를 그대로 씁니다.
        리다이렉트/재작성 규칙은 프로젝트당 하나로 관리되며 다음 배포·롤백부터
        반영됩니다. 복합(백엔드+프론트엔드) 프로젝트는 같은 경로 아래 <code>api/*</code>
        (백엔드)·나머지(프론트엔드)로 자동 라우팅됩니다 — 아래 다이어그램에서
        컴포넌트별 상태를 볼 수 있습니다.
      </p>
      {error && <p className="error">{error}</p>}
      {showTopology && state.data && <TopologyDiagram cfg={state.data} />}
      <Async state={state} empty="등록된 프로젝트가 없습니다.">
        {(cfg) => (
          <table>
            <thead>
              <tr>
                <th>프로젝트</th>
                <th>프로필</th>
                <th>URL</th>
                <th>상태</th>
                <th>리다이렉트</th>
                <th style={{ width: 280 }}>동작</th>
              </tr>
            </thead>
            <tbody>
              {cfg.sites.map((s) => {
                const deployKey = `${s.project_id}-${s.profile}-deploy`;
                const stopKey = `${s.project_id}-${s.profile}-stop`;
                return (
                  <tr key={`${s.project_id}-${s.profile}`}>
                    <td>{s.project_name}</td>
                    <td><StatusPill value={s.profile} /></td>
                    <td className="mono">{s.domain}{s.path_prefix !== '/' ? s.path_prefix : ''}</td>
                    <td><StatusPill value={s.status} /></td>
                    <td>{s.redirect_count}</td>
                    <td>
                      <div className="row">
                        <button
                          className="small"
                          disabled={busyKey === deployKey}
                          onClick={() => startDeploy(s.project_id, s.project_name, s.profile)}
                        >
                          배포
                        </button>
                        <button
                          className="small danger"
                          disabled={busyKey === stopKey}
                          onClick={() => stopProject(s.project_id, s.profile)}
                        >
                          중지
                        </button>
                        <button
                          className="small secondary"
                          onClick={() => setRulesFor({ id: s.project_id, name: s.project_name })}
                        >
                          리다이렉트 규칙
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
      {state.data && state.data.unregistered.length > 0 && (
        <div style={{ marginTop: 18 }}>
          <h3 style={{ marginBottom: 6 }}>미등록 라우트</h3>
          <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
            프록시 설정(web.config)에는 있으나 프로젝트로 등록되지 않은 항목입니다 —
            이름과 rewrite 대상 주소만 표시합니다.
          </p>
          <table>
            <thead>
              <tr>
                <th>이름</th>
                <th>rewrite 주소</th>
              </tr>
            </thead>
            <tbody>
              {state.data.unregistered.map((u) => (
                <tr key={u.name}>
                  <td className="mono">{u.name}</td>
                  <td className="mono">
                    {u.rewrite_targets.length > 0 ? u.rewrite_targets.join(', ') : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rulesFor && (
        <RedirectRulesModal
          projectId={rulesFor.id}
          projectName={rulesFor.name}
          onClose={() => setRulesFor(null)}
          onChanged={() => state.reload()}
        />
      )}
      {deployFor && (
        <DeployProgressModal
          projectId={deployFor.projectId}
          projectName={deployFor.name}
          profile={deployFor.profile}
          deploymentIds={deployFor.ids}
          onClose={() => {
            setDeployFor(null);
            state.reload();
          }}
        />
      )}
    </div>
  );
}

function RedirectRulesModal({
  projectId,
  projectName,
  onClose,
  onChanged,
}: {
  projectId: number;
  projectName: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const rules = useApi(() => api.listRedirects(projectId), [projectId]);
  const [fromPath, setFromPath] = useState('');
  const [toPath, setToPath] = useState('');
  const [kind, setKind] = useState<'redirect' | 'rewrite'>('redirect');
  const [statusCode, setStatusCode] = useState(302);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api.createRedirect(projectId, fromPath.trim(), toPath.trim(), kind, statusCode);
      setFromPath('');
      setToPath('');
      rules.reload();
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    setBusy(true);
    try {
      await api.deleteRedirect(id);
      rules.reload();
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={`${projectName} — 리다이렉트/재작성 규칙`} onClose={onClose}>
      <Async state={rules} empty="등록된 규칙이 없습니다.">
        {(list: RedirectRule[]) => (
          <table style={{ marginBottom: 14 }}>
            <thead>
              <tr>
                <th>From</th>
                <th>To</th>
                <th>종류</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {list.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.from_path}</td>
                  <td className="mono">{r.to_path}</td>
                  <td>
                    <StatusPill value={r.kind} />
                    {r.kind === 'redirect' && (
                      <span className="mutedtext" style={{ marginLeft: 6, fontSize: 11 }}>
                        {r.status_code}
                      </span>
                    )}
                  </td>
                  <td>
                    <button className="small danger" disabled={busy} onClick={() => remove(r.id)}>
                      삭제
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      <form onSubmit={add} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            From 경로
            <input
              className="mono"
              value={fromPath}
              onChange={(e) => setFromPath(e.target.value)}
              placeholder="/old"
              required
            />
          </label>
          <label className="field" style={{ flex: 1 }}>
            To 경로
            <input
              className="mono"
              value={toPath}
              onChange={(e) => setToPath(e.target.value)}
              placeholder="/new"
              required
            />
          </label>
        </div>
        <div className="row">
          <label className="field">
            종류
            <select value={kind} onChange={(e) => setKind(e.target.value as 'redirect' | 'rewrite')}>
              <option value="redirect">redirect (브라우저 리다이렉트)</option>
              <option value="rewrite">rewrite (내부 재작성)</option>
            </select>
          </label>
          {kind === 'redirect' && (
            <label className="field">
              상태 코드
              <select value={statusCode} onChange={(e) => setStatusCode(Number(e.target.value))}>
                <option value={301}>301 Permanent</option>
                <option value={302}>302 Found</option>
                <option value={307}>307 Temporary</option>
              </select>
            </label>
          )}
        </div>
        {error && <p className="error">{error}</p>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="secondary" onClick={onClose}>
            닫기
          </button>
          <button type="submit" disabled={busy}>
            {busy ? '추가 중...' : '규칙 추가'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
