import { useEffect, useState } from 'react';
import Modal from './Modal';
import { api } from '../lib/api';
import type { StartScriptOut } from '../lib/types';

/**
 * 기동 스크립트(start.cmd) 확인·수정 — LLM이 쓰고, **사람이 확인해 저장한다.**
 *
 * 플랫폼의 제네릭 템플릿은 흔한 모양만 맞힌다. 맞지 않는 프로젝트는 조용히 엉뚱하게 뜨거나
 * 배포가 실패한다(실측: streamlit을 uvicorn으로 띄우려 함). 그래서 리포를 보고 LLM이
 * 스크립트를 제안한다.
 *
 * 자동 적용하지 않는 이유: 이 스크립트는 서버에서 **서비스 권한으로** 실행된다. LLM이 쓴
 * 것을 그대로 저장·실행하면 그것은 원격 코드 실행이다. 그래서 검증 결과(problems)를 함께
 * 보여 주고, 하나라도 걸리면 저장 버튼이 막힌다 — 경고만 하고 통과시키면 아무도 읽지 않는다.
 *
 * 복합 배포는 컴포넌트마다 스크립트가 따로다 — 유닛·포트·공개 경로가 이미 컴포넌트별이고,
 * 한 스크립트가 둘을 띄우면 서비스 감시자가 자식 하나만 본다.
 */
interface Props {
  projectId: number;
  onClose: () => void;
}

export default function StartScriptModal({ projectId, onClose }: Props) {
  const [component, setComponent] = useState('');
  const [data, setData] = useState<StartScriptOut | null>(null);
  const [script, setScript] = useState('');
  const [components, setComponents] = useState<string[]>([]);
  const [providers, setProviders] = useState<{ id: number; name: string; model: string }[]>([]);
  const [providerId, setProviderId] = useState(0);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  // 유닛을 바꾸면 그 유닛의 스크립트를 다시 읽는다 — 화면에 남은 본문을 그대로 두면
  // 다른 컴포넌트의 스크립트를 이 컴포넌트에 저장하게 된다.
  useEffect(() => {
    let alive = true;
    setError('');
    setNotice('');
    setBusy('불러오는 중…');
    api.startScript(projectId, component).then(
      (res) => {
        if (!alive) return;
        setComponents(res.components);
        // 복합 배포에는 "단일" 스크립트가 쓰일 자리가 없다(배포는 언제나 컴포넌트별로
        // 부른다) — 첫 컴포넌트로 옮겨 다시 읽는다. 그러지 않으면 아무도 안 쓰는
        // 스크립트를 보고 저장하게 된다.
        if (res.components.length > 0 && !component) {
          setComponent(res.components[0]);
          return;
        }
        setData(res);
        setScript(res.script);
        setBusy('');
      },
      (err: Error) => {
        if (!alive) return;
        setError(err.message);
        setBusy('');
      },
    );
    return () => { alive = false; };
  }, [projectId, component]);

  useEffect(() => {
    let alive = true;
    api.listProviders().then(
      (rows) => {
        if (!alive) return;
        setProviders(rows);
        if (rows.length) setProviderId((cur) => cur || rows[0].id);
      },
      () => {},  // 프로바이더를 못 읽어도 스크립트 확인·직접 수정은 된다
    );
    return () => { alive = false; };
  }, []);

  const run = async (label: string, fn: () => Promise<StartScriptOut>) => {
    setBusy(label);
    setError('');
    setNotice('');
    try {
      const res = await fn();
      setData(res);
      setScript(res.script);
      setComponents(res.components);
      return res;
    } catch (err) {
      setError((err as Error).message);
      return null;
    } finally {
      setBusy('');
    }
  };

  const propose = () => run(
    'LLM이 리포를 보고 작성하는 중… (수십 초 걸릴 수 있습니다)',
    () => api.proposeStartScript(projectId, providerId, component),
  );
  const save = async () => {
    const res = await run('저장 중…', () => api.setStartScript(projectId, script, component));
    if (res) setNotice('저장했습니다 — 다음 배포에서 이 스크립트가 쓰입니다.');
  };
  const reset = async () => {
    const res = await run('되돌리는 중…', () => api.resetStartScript(projectId, component));
    if (res) setNotice('템플릿으로 되돌렸습니다.');
  };

  // 저장은 검증을 통과해야만 한다. 화면의 본문이 서버가 검증한 것과 다르면(직접 고쳤으면)
  // 서버가 저장 시점에 다시 검증한다 — 여기서 막는 것은 "이미 문제라고 아는" 경우다.
  const unchecked = data !== null && script !== data.script;
  const blocked = !unchecked && (data?.problems.length ?? 0) > 0;

  return (
    <Modal title="기동 스크립트 (start.cmd)" onClose={onClose}>
      <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        {components.length > 0 && (
          <label className="field" style={{ margin: 0 }}>
            배포 유닛
            <select value={component} onChange={(e) => setComponent(e.target.value)}>
              {components.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
        )}
        <label className="field" style={{ margin: 0, flex: 1, minWidth: 180 }}>
          작성에 쓸 LLM
          <select
            value={providerId}
            onChange={(e) => setProviderId(Number(e.target.value))}
            disabled={!providers.length}
          >
            {providers.length === 0 && <option value={0}>등록된 프로바이더가 없습니다</option>}
            {providers.map((p) => (
              <option key={p.id} value={p.id}>{p.name} ({p.model})</option>
            ))}
          </select>
        </label>
        <button
          className="secondary"
          style={{ alignSelf: 'flex-end' }}
          onClick={propose}
          disabled={!!busy || !providerId}
        >
          LLM으로 작성
        </button>
      </div>

      <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 6px' }}>
        현재: {data?.source === 'project' ? '이 프로젝트에 지정된 스크립트' : '플랫폼 템플릿'}
        {' · '}이 스크립트는 서버에서 서비스 권한으로 실행됩니다 — 내용을 읽고 확인한 뒤
        저장하세요.
      </p>

      {(data?.problems.length ?? 0) > 0 && (
        <ul className="error" style={{ fontSize: 12, margin: '0 0 8px', paddingLeft: 18 }}>
          {data?.problems.map((p) => <li key={p}>{p}</li>)}
        </ul>
      )}

      <textarea
        className="mono"
        value={script}
        onChange={(e) => setScript(e.target.value)}
        spellCheck={false}
        style={{ width: '100%', height: 300, fontSize: 12, whiteSpace: 'pre' }}
      />

      {data?.facts && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ fontSize: 12, cursor: 'pointer' }}>
            LLM에게 준 사실(무엇을 보고 썼는지)
          </summary>
          <pre
            className="mono"
            style={{
              background: '#090d16', padding: 10, borderRadius: 6, fontSize: 11,
              maxHeight: 200, overflow: 'auto', whiteSpace: 'pre-wrap',
            }}
          >
            {data.facts}
          </pre>
        </details>
      )}

      {busy && <p style={{ fontSize: 13 }}>{busy}</p>}
      {error && <p className="error">{error}</p>}
      {notice && <p style={{ fontSize: 13, color: '#7ee787' }}>{notice}</p>}

      <div className="row" style={{ marginTop: 10 }}>
        <button onClick={save} disabled={!!busy || !script.trim() || blocked}>
          확인했습니다 — 저장
        </button>
        <button className="secondary" onClick={reset} disabled={!!busy}>
          템플릿으로 되돌리기
        </button>
        <div className="spacer" />
        <button className="secondary" onClick={onClose} disabled={!!busy}>닫기</button>
      </div>
      {blocked && (
        <p className="error" style={{ fontSize: 12 }}>
          검증을 통과하지 못한 스크립트는 저장할 수 없습니다 — 위 항목을 고치세요.
        </p>
      )}
    </Modal>
  );
}
