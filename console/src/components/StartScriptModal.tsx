import { useEffect, useRef, useState } from 'react';
import Modal from './Modal';
import { api } from '../lib/api';
import type { BuildProfile, StartScriptOut } from '../lib/types';

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
 * 스크립트는 **프로필·컴포넌트마다 따로**다. 개발 배포와 운영 배포는 기동 방법이 아예 다르고
 * (dev 서버 vs 빌드본 서빙), 복합 배포는 컴포넌트마다 유닛·포트·공개 경로가 따로다 — 한
 * 스크립트가 둘을 띄우면 서비스 감시자가 자식 하나만 본다.
 *
 * 「LLM으로 작성」은 **전체 배포 단위부터 확인한다** — 리포를 최신으로 두고 구조를 다시 감지해 유닛 목록에
 * 반영한 뒤, 고른 유닛이 그대로 있으면 그 유닛만, 없어졌으면 모든 유닛을 쓴다(결과에 어느 쪽인지 보인다).
 * 제안은 유닛별 초안으로 들고 있다가(저장 전), 유닛을 골라 확인하고 하나씩 저장한다.
 */
interface Props {
  projectId: number;
  /** 어느 프로필의 스크립트인가 — 개요 화면의 프로필 행에서 그대로 넘어온다. */
  profile: BuildProfile;
  onClose: () => void;
  /**
   * 저장한 스크립트로 실제 배포를 건다. **저장만으로는 아무것도 바뀌지 않는다** — 다음
   * 배포에서 쓰일 뿐이라, 저장하고 창을 닫으면 사람은 배포 화면을 다시 찾아가야 한다.
   * 진행 표시는 호출측이 이미 가지고 있으므로(개요의 진행 팝업, 진단 팝업의 재시도)
   * 여기서 만들지 않고 그 경로를 그대로 쓴다.
   */
  onRedeploy: () => Promise<void>;
  /** 배포 진단 창의 LLM 원인 분석 — 그 창에서 「기동 스크립트 재작성」으로 열면 작성에 함께 싣는다. */
  analysis?: string;
}

export default function StartScriptModal({ projectId, profile, onClose, onRedeploy, analysis = '' }: Props) {
  const [component, setComponent] = useState('');
  const [data, setData] = useState<StartScriptOut | null>(null);
  const [script, setScript] = useState('');
  const [components, setComponents] = useState<string[]>([]);
  const [providers, setProviders] = useState<{ id: number; name: string; model: string }[]>([]);
  const [providerId, setProviderId] = useState(0);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // 이 창에서 저장(또는 되돌리기)을 한 번 했는가 — 했다면 다음 할 일은 저장이 아니라 배포다.
  const [applied, setApplied] = useState(false);
  // LLM이 유닛마다 쓴 초안(저장 전) — 유닛을 바꿔도 남고, 그 유닛을 저장·되돌리면 지운다.
  const drafts = useRef<Record<string, StartScriptOut>>({});
  const [draftUnits, setDraftUnits] = useState<string[]>([]);
  // 마지막 「LLM으로 작성」이 고친 범위 — 선택한 유닛만인지, 모든 유닛인지
  const [result, setResult] = useState<{ all: boolean; text: string } | null>(null);
  const showing = drafts.current[component] !== undefined && data === drafts.current[component];

  // 유닛을 바꾸면 그 유닛의 스크립트를 다시 읽는다 — 화면에 남은 본문을 그대로 두면
  // 다른 컴포넌트의 스크립트를 이 컴포넌트에 저장하게 된다.
  useEffect(() => {
    let alive = true;
    setError('');
    setNotice('');
    setApplied(false);  // 유닛·프로필을 바꾸면 다른 스크립트다
    setBusy('불러오는 중…');
    api.startScript(projectId, profile, component).then(
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
        // 이 유닛의 초안이 있으면 그것을 보인다 — 저장한 것은 초안을 저장하거나 버리면 보인다.
        const draft = drafts.current[component];
        setData(draft ?? res);
        setScript((draft ?? res).script);
        setBusy('');
      },
      (err: Error) => {
        if (!alive) return;
        setError(err.message);
        setBusy('');
      },
    );
    return () => { alive = false; };
  }, [projectId, profile, component]);

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
    // 무엇을 하든 "방금 저장한 상태"는 깨진다 — 새 제안을 받아 놓고 재배포 버튼을 누르면
    // 저장되지 않은 본문으로 배포하게 된다(그러면 방금 쓴 것이 쓰이지 않는다).
    setApplied(false);
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

  // 배포 유닛부터 확인하고 유닛마다 쓴다 — 유닛 하나에 수십 초라 한 요청에 몰지 않고 하나씩 부른다.
  const propose = async () => {
    setError('');
    setNotice('');
    setApplied(false);
    setResult(null);   // 지난 작성의 결과가 새 작성 동안 남아 있으면 그것이 이번 결과로 읽힌다
    try {
      // 늘 전체 배포 단위부터 다시 확인하고 목록에 반영한다 — 리포를 최신으로 받지 못하거나 배포 단위가 없으면
      // 서버가 멈추고 이유를 준다.
      setBusy('배포 유닛을 확인하는 중… (리포를 최신으로 받아 전체 구조를 다시 감지합니다)');
      const units = await api.startScriptUnits(projectId);
      setComponents(units.components);
      const all = units.components.length ? units.components : [''];
      // 없어진 유닛의 초안은 버린다 — 남기면 목록에 없는 유닛을 저장하려 한다.
      Object.keys(drafts.current).filter((u) => !all.includes(u)).forEach((u) => { delete drafts.current[u]; });
      // 고른 유닛이 새 목록에 있으면 그 유닛만, 없어졌으면(이름이 바뀌었거나 단일↔복합이 바뀌었으면) 오류로
      // 끝내지 않고 모든 유닛을 쓴다.
      const kept = all.includes(component);
      const list = kept ? [component] : all;
      const failed: string[] = [];
      const retried: string[] = [];
      const learned: string[] = [];   // 지난 실패를 참고한 유닛
      for (const [i, unit] of list.entries()) {
        const label = unit || '단일 배포';
        setBusy(`LLM이 「${label}」 스크립트를 작성하는 중… (${i + 1}/${list.length}, 유닛마다 수십 초)`);
        try {
          const res = await api.proposeStartScript(projectId, providerId, profile, unit, analysis);
          drafts.current[unit] = res;
          if (res.failure_deployment_id) learned.push(`${label}(배포 #${res.failure_deployment_id})`);
          // 검증에 걸리면 서버가 그 문구를 모델에 돌려주고 한 번 고치게 한다 — "고쳐서 온 것"은 알린다.
          if (res.attempts > 1) retried.push(`${label}${res.problems.length ? '(아직 검증 실패)' : ''}`);
        } catch (err) {
          failed.push(`${label}: ${(err as Error).message}`);
        }
      }
      setDraftUnits(Object.keys(drafts.current));
      const first = list.find((u) => drafts.current[u]) ?? list[0];
      if (first === component && drafts.current[first]) {
        setData(drafts.current[first]);
        setScript(drafts.current[first].script);
      } else {
        setComponent(first);   // 바꾸면 위 effect가 그 유닛의 초안을 보인다
      }
      const written = list.filter((u) => drafts.current[u]);
      const names = (us: string[]) => us.map((u) => u || '단일 배포').join('·');
      const scope = kept
        ? `선택한 유닛 「${component || '단일 배포'}」만 갱신했습니다`
        : `선택한 유닛 「${component || '단일 배포'}」이 지금 구조에 없어 모든 유닛(${all.length}개: ${names(all)})을 갱신했습니다`;
      setResult({ all: !kept, text: `${scope} — 작성 ${written.length}/${list.length}${written.length ? ` (${names(written)})` : ''}` });
      setNotice(`배포 유닛 ${all.length}개 — ${units.structure}`
        + (units.changed ? ` (구조가 바뀌어 목록을 갱신했습니다: ${units.before || '없음'} → ${units.structure})` : '')
        + '. 작성한 유닛을 확인하고 저장하세요.'
        + (learned.length ? ` 지난 실패를 참고해 썼습니다: ${learned.join(', ')}${analysis ? ' — 진단 창의 분석도 함께' : ''}.` : '')
        + (retried.length ? ` 검증에 걸려 다시 쓴 유닛: ${retried.join(', ')}.` : ''));
      if (failed.length) setError(`작성하지 못한 유닛 — ${failed.join(' / ')}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy('');
    }
  };
  // 저장·되돌리기를 하면 그 유닛의 초안은 끝났다
  const settle = () => {
    delete drafts.current[component];
    setDraftUnits(Object.keys(drafts.current));
  };
  const save = async () => {
    const res = await run('저장 중…',
      () => api.setStartScript(projectId, script, profile, component));
    if (res) {
      settle();
      setApplied(true);
      setNotice('저장했습니다 — 재배포하면 이 스크립트로 뜹니다.');
    }
  };
  const reset = async () => {
    const res = await run('되돌리는 중…',
      () => api.resetStartScript(projectId, profile, component));
    if (res) {
      settle();
      // 되돌리기도 다음 배포가 실행할 것을 바꾼 것이다 — 그다음 할 일은 똑같이 배포다.
      setApplied(true);
      setNotice('템플릿으로 되돌렸습니다 — 재배포하면 템플릿으로 뜹니다.');
    }
  };
  const redeploy = async () => {
    setBusy('재배포를 요청하는 중…');
    setError('');
    try {
      await onRedeploy();
      onClose();  // 진행은 호출측 화면이 보여 준다 — 이 창이 그것을 가리면 안 된다
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy('');
    }
  };

  // 저장은 검증을 통과해야만 한다. 화면의 본문이 서버가 검증한 것과 다르면(직접 고쳤으면)
  // 서버가 저장 시점에 다시 검증한다 — 여기서 막는 것은 "이미 문제라고 아는" 경우다.
  const unchecked = data !== null && script !== data.script;
  const blocked = !unchecked && (data?.problems.length ?? 0) > 0;
  // 저장한 뒤 본문을 또 고쳤으면 저장이 다시 필요하다 — 그때는 재배포가 아니라 저장을
  // 보여 준다(고친 것을 저장하지 않은 채 배포하면 방금 쓴 것이 쓰이지 않는다).

  return (
    <Modal title={`기동 스크립트 (start.cmd) — ${profile}`} onClose={onClose}>
      <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        {components.length > 0 && (
          <label className="field" style={{ margin: 0 }}>
            배포 유닛
            <select value={component} onChange={(e) => setComponent(e.target.value)}>
              {components.map((c) => (
                <option key={c} value={c}>{c}{draftUnits.includes(c) ? ' · LLM 제안(저장 전)' : ''}</option>
              ))}
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
        현재: {showing ? 'LLM 제안 — 아직 저장하지 않았습니다'
          : data?.source === 'project' ? '이 프로젝트에 지정된 스크립트' : '플랫폼 템플릿'}
        {' · '}{profile} 프로필에만 적용됩니다{' · '}이 스크립트는 서버에서 서비스 권한으로
        실행됩니다 — 내용을 읽고 확인한 뒤 저장하세요.
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

      {result && (
        <p style={{ fontSize: 13, margin: '8px 0 0' }}>
          <span className={`status ${result.all ? 'warn' : 'info'}`} style={{ marginRight: 6 }}>
            {result.all ? '모든 유닛 갱신' : '선택한 유닛만 갱신'}
          </span>
          {result.text}
        </p>
      )}
      {busy && <p style={{ fontSize: 13 }}>{busy}</p>}
      {error && <p className="error">{error}</p>}
      {notice && <p style={{ fontSize: 13, color: '#7ee787' }}>{notice}</p>}

      <div className="row" style={{ marginTop: 10 }}>
        {applied && !unchecked ? (
          <button onClick={redeploy} disabled={!!busy}>
            재배포
          </button>
        ) : (
          <button onClick={save} disabled={!!busy || !script.trim() || blocked}>
            확인했습니다 — 저장
          </button>
        )}
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
