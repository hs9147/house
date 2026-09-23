import { useEffect, useState } from 'react';
import Modal from './Modal';
import { api } from '../lib/api';
import type { BuildProfile } from '../lib/types';

/**
 * 배포 실패 진단 — 원인과 고칠 것을 보여 주고, **재시도는 사람이 확인한다.**
 *
 * 실패하면 화면에는 오류 한 줄과 로그 파일만 남는다. 그것으로는 "리포가 잘못됐나 · 설정이
 * 잘못됐나 · 플랫폼이 잘못됐나"가 갈리지 않아서 사람이 로그를 열어 추측한다. 서버가 리포와
 * 로그를 실제로 보고 짚은 원인을 여기서 그대로 보여 준다(app/services/deploydiag.py).
 *
 * 고침을 자동으로 적용하지 않는 이유: 배포는 되돌리기 쉬운 일이 아니고, 고침이 프로젝트
 * 설정(빌드 대상 폴더)을 바꾸는 경우도 있다. 무엇을 바꾸고 다시 배포하는지 사람이 보고
 * 확인하거나 취소해야 한다.
 */
interface Props {
  projectId: number;
  profile: BuildProfile;
  onClose: () => void;
  /** 확인했을 때 실제 재배포 — 호출측이 기존 배포 경로를 그대로 쓴다. */
  onRetry: () => Promise<void>;
}

const CAUSE_LABEL: Record<string, string> = {
  source_in_subdir: '소스가 하위 폴더에 있습니다',
  no_entry_marker: '실행 방법을 알 수 없습니다',
  streamlit_entry_missing: 'streamlit 진입 파일이 없습니다',
  missing_source_dir: '빌드 대상 폴더가 없습니다',
  npm_failed: 'npm 단계에서 실패했습니다',
  pip_failed: 'pip가 의존성을 찾지 못했습니다',
  build_failed: '빌드·설치가 실패했습니다',
  unknown: '원인을 짚지 못했습니다',
};

export default function DeployDiagnoseModal({ projectId, profile, onClose, onRetry }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [subdir, setSubdir] = useState('');
  const state = useDiagnosis(projectId, profile, setSubdir);

  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      // 제안을 골랐으면 먼저 적용한다 — 적용하지 않고 재배포하면 같은 자리에서 또 실패한다.
      if (subdir && subdir !== state.data?.source_subdir) {
        await api.setSourceSubdir(projectId, subdir);
      }
      await onRetry();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const fix = state.data?.fix ?? null;
  const fixable = fix?.kind === 'source_subdir';

  return (
    <Modal title={`배포 실패 진단 — ${profile}`} onClose={onClose}>
      {state.loading && <p style={{ fontSize: 13 }}>리포와 로그를 보고 원인을 짚는 중…</p>}
      {state.error && <p className="error">{state.error}</p>}
      {state.data && (
        <>
          <p style={{ fontSize: 13, margin: '0 0 6px' }}>
            <b>{CAUSE_LABEL[state.data.cause] ?? state.data.cause}</b>
          </p>
          <p style={{ fontSize: 13, margin: '0 0 8px' }}>{state.data.detail}</p>
          <p className="mutedtext" style={{ fontSize: 12 }}>
            배포 #{state.data.deployment_id} · 감지된 배포 단위: {state.data.detected}
            {state.data.source_subdir && (
              <> · 현재 빌드 대상: <span className="mono">{state.data.source_subdir}</span></>
            )}
          </p>

          {fixable && (
            <label className="field" style={{ marginTop: 6 }}>
              적용할 빌드 대상 폴더 — 다음 배포에서 스크립트가 이 폴더 기준으로 다시 만들어집니다
              <select className="mono" value={subdir} onChange={(e) => setSubdir(e.target.value)}>
                {fix.options.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </label>
          )}
          {fix?.kind === 'repo' && (
            <p className="mutedtext" style={{ fontSize: 12 }}>
              플랫폼이 대신 만들 수 없습니다 — 리포에{' '}
              <span className="mono">{fix.needs.join(' · ')}</span> 중 하나를 넣고 다시
              배포하세요.
            </p>
          )}

          {state.data.log_tail && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ fontSize: 12, cursor: 'pointer' }}>빌드 로그 꼬리(근거)</summary>
              <pre
                className="mono"
                style={{
                  background: '#090d16', padding: 10, borderRadius: 6, fontSize: 11,
                  maxHeight: 200, overflow: 'auto', whiteSpace: 'pre-wrap',
                }}
              >
                {state.data.log_tail}
              </pre>
            </details>
          )}

          {error && <p className="error">{error}</p>}
          <div className="row" style={{ marginTop: 10 }}>
            <button onClick={confirm} disabled={busy}>
              {busy ? '재배포 중...' : fixable ? '적용하고 재배포' : '그대로 재배포'}
            </button>
            <button className="secondary" onClick={onClose} disabled={busy}>취소</button>
          </div>
        </>
      )}
    </Modal>
  );
}

/** 진단 조회 — 제안 값을 선택 상자의 초기값으로 심는다. */
function useDiagnosis(projectId: number, profile: BuildProfile, setSubdir: (v: string) => void) {
  const [state, setState] = useState<{
    loading: boolean;
    error: string;
    data: Awaited<ReturnType<typeof api.diagnoseDeploy>> | null;
  }>({ loading: true, error: '', data: null });

  useEffect(() => {
    let alive = true;
    api.diagnoseDeploy(projectId, profile).then(
      (data) => {
        if (!alive) return;
        setState({ loading: false, error: '', data });
        if (data.fix?.kind === 'source_subdir') setSubdir(data.fix.value);
      },
      (err: Error) => {
        if (alive) setState({ loading: false, error: err.message, data: null });
      },
    );
    // 팝업을 바로 닫으면 응답이 뒤늦게 와서 사라진 화면에 setState를 부른다.
    return () => { alive = false; };
    // setSubdir은 호출측의 setState라 참조가 고정돼 있다 — deps에 넣으면 의미 없이 다시 돈다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, profile]);

  return state;
}
