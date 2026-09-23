import { useEffect, useState } from 'react';
import Modal from './Modal';
import StartScriptModal from './StartScriptModal';
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
 *
 * **플랫폼이 고칠 수 없는 원인에는 '그대로 재배포'를 권하지 않는다.** 실행 방법을 못 찾은
 * 리포는 같은 자리에서 또 실패한다 — 그때 필요한 것은 이 리포를 어떻게 띄우는지 적는
 * 일이므로, 기동 스크립트 작성으로 보낸다(LLM이 제안하고 사람이 확인해 저장한다). 저장한
 * 스크립트는 다음 배포에서 그대로 start.cmd가 된다(build.write_start_script).
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
  const [writingScript, setWritingScript] = useState(false);
  // LLM 분석은 **누를 때만** 돈다 — 매번 자동으로 부르면 느리고, 비용이 들고, 결정론
  // 진단만으로 충분한 경우가 대부분이다.
  const [providers, setProviders] = useState<{ id: number; name: string; model: string }[]>([]);
  const [providerId, setProviderId] = useState(0);
  const [analysis, setAnalysis] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
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

  useEffect(() => {
    let alive = true;
    api.listProviders().then(
      (rows) => {
        if (!alive) return;
        setProviders(rows);
        if (rows.length) setProviderId((cur) => cur || rows[0].id);
      },
      () => {},  // 프로바이더를 못 읽어도 결정론 진단은 그대로 쓸 수 있다
    );
    return () => { alive = false; };
  }, []);

  const explain = async () => {
    setAnalyzing(true);
    setError('');
    try {
      const res = await api.explainDeployFailure(projectId, providerId, profile);
      setAnalysis(res.analysis);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setAnalyzing(false);
    }
  };

  const fix = state.data?.fix ?? null;
  const fixable = fix?.kind === 'source_subdir';
  // 리포에 시그니처가 없어 실행 방법을 못 찾은 경우 — 스크립트를 적는 것이 고침이다.
  const needsScript = fix?.kind === 'repo' || state.data?.cause === 'no_entry_marker'
    || state.data?.cause === 'streamlit_entry_missing';

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
              템플릿이 실행 방법을 찾지 못했습니다 — 리포에{' '}
              <span className="mono">{fix.needs.join(' · ')}</span> 중 하나를 넣거나,
              아래 <b>기동 스크립트 작성</b>으로 이 리포에 맞는 start.cmd를 저장하세요
              (LLM이 제안하고 사람이 확인합니다). 저장한 스크립트는 다음 배포에 바로 쓰입니다.
            </p>
          )}

          {/* 결정론 판정이 짚지 못한 경우가 남는다(build_failed·unknown) — 그때부터
              사람이 로그를 읽던 일을 LLM에 맡긴다. 결과는 글이고, 적용은 사람이 한다. */}
          <div className="row" style={{ gap: 8, margin: '10px 0 4px', flexWrap: 'wrap' }}>
            <select
              className="small"
              value={providerId}
              onChange={(e) => setProviderId(Number(e.target.value))}
              disabled={!providers.length || analyzing}
            >
              {providers.length === 0 && <option value={0}>등록된 LLM이 없습니다</option>}
              {providers.map((p) => (
                <option key={p.id} value={p.id}>{p.name} ({p.model})</option>
              ))}
            </select>
            <button
              className="small secondary"
              onClick={explain}
              disabled={analyzing || !providerId}
            >
              {analyzing ? '로그를 읽는 중…' : 'LLM으로 원인 분석'}
            </button>
          </div>
          {analysis && (
            <pre
              className="mono"
              style={{
                background: '#0d1117', padding: 10, borderRadius: 6, fontSize: 12,
                maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap', margin: '0 0 8px',
              }}
            >
              {analysis}
            </pre>
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
            {/* 같은 자리에서 또 실패할 재배포를 권하지 않는다 — 실행 방법을 적게 한다. */}
            {needsScript && !fixable ? (
              <button onClick={() => setWritingScript(true)} disabled={busy}>
                기동 스크립트 작성
              </button>
            ) : (
              <button onClick={confirm} disabled={busy}>
                {busy ? '재배포 중...' : fixable ? '적용하고 재배포' : '그대로 재배포'}
              </button>
            )}
            <button className="secondary" onClick={onClose} disabled={busy}>취소</button>
          </div>
        </>
      )}
      {writingScript && (
        <StartScriptModal
          projectId={projectId}
          profile={profile}
          onClose={() => setWritingScript(false)}
        />
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
