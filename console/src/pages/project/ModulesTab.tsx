import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import Modal from '../../components/Modal';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ModuleOut, ModuleSummary } from '../../lib/types';
import type { ProjectContext } from '../ProjectDetail';

/**
 * 바인딩된 모듈 — 조회·**설정 수정**·해제. 설정(호출 주소·인증키·키 인자 이름)은 바인딩한 뒤에 넣는 일이 많다 —
 * 카탈로그로 붙인 API는 주소가 홈페이지이고 키가 없다(bind_api의 setup_required). 모듈은 관리자·그 모듈 조직
 * 사람만 고친다(서버가 판정).
 *
 * 수작업 바인딩 폼이 있었는데, 감사 기록을 보면 쓰이지 않았다: 바인딩 34건 중 32건이 기획
 * "솔루션 구성" 단계의 도구(`via: plan.solution`)가 만든 것이고, 손으로 만든 2건은 같은 날
 * 해제됐다(실험 한 번). 모듈을 고르고 env 접두사를 사람이 정하는 일은 그 단계에서 문서와 함께
 * 결정되는 것이고, 여기서 다시 하게 두면 두 곳이 서로 다른 구성을 만든다.
 *
 * **해제는 남긴다.** 기획 단계에는 해제 도구가 없어서(바인딩만 한다), 접두사를 잘못 정한
 * 바인딩을 되돌릴 길이 여기밖에 없다 — 그걸 없애면 잘못 주입되는 환경변수를 고칠 방법이
 * 사라진다. 실제로 그 2건을 되돌린 것도 이 화면이다.
 */
export default function ModulesTab() {
  const { project } = useOutletContext<ProjectContext>();
  const bound = useApi(() => api.projectModules(project.id), [project.id]);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<ModuleSummary | null>(null);

  const unbind = async (m: ModuleSummary) => {
    if (!window.confirm(`'${m.agent_name}' 바인딩(${m.env_prefix})을 해제하시겠습니까?\n다음 배포부터 이 환경변수가 주입되지 않습니다.`)) return;
    setError('');
    try {
      await api.unbindModule(project.id, m.binding_id);
      bound.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="panel">
      <h2>바인딩된 도구</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        바인딩은 에이전트 기획의 <b>솔루션 구성</b> 단계에서 문서와 함께 결정됩니다 — 여기서는
        무엇이 붙어 있는지 확인하고, 잘못된 것을 해제합니다. 바인딩된 도구는 다음 배포부터
        규약된 환경변수로 주입되고, 이 목록은 가용 도구 제약(외부 빌드 guardrail)에도 그대로
        쓰입니다.
      </p>
      <Async state={bound} empty="바인딩된 도구가 없습니다 — 기획의 '솔루션 구성' 단계에서 붙습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>도구</th>
                <th>타입</th>
                <th>env 접두사</th>
                <th>능력</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.binding_id}>
                  <td>{m.agent_name}</td>
                  <td><StatusPill value={m.type} /></td>
                  <td className="mono">{m.env_prefix}</td>
                  <td className="mono">{m.skills.join(', ')}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="small secondary" onClick={() => setEditing(m)}>설정</button>{' '}
                    <button className="small danger" onClick={() => unbind(m)}>해제</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      {error && <p className="error">{error}</p>}
      {editing && <ModuleSettings summary={editing} onClose={() => setEditing(null)} onSaved={() => bound.reload()} />}
    </div>
  );
}

const MASK = '•••';
// 어떤 항목인지 — 외부 API는 이 셋이면 부를 수 있다.
const HINT: Record<string, string> = {
  url: '실제 호출 주소(예: https://apis.data.go.kr/…/오퍼레이션)',
  key_param: '쿼리 인자로 키를 받는 API의 인자 이름(serviceKey · crtfc_key · authkey) — 비우면 헤더로 붙인다',
  api_key: '인증키 — 공공데이터포털은 디코딩 키',
};

/**
 * 모듈 설정 — 항목마다 값을 고치고 새 항목을 더한다. 비밀 항목(키·비밀번호)은 저장된 값을 보여 주지 않는다:
 * 빈칸이면 그대로 두고, 입력하면 바꾼다. 바꾼 항목만 보낸다(서버가 기존 설정에 합친다). catalog는 카탈로그의 어느
 * 항목에서 왔는지라 고치지 않는다(같은 항목을 다시 바인딩하면 이 모듈을 찾는 표시다).
 */
function ModuleSettings({ summary, onClose, onSaved }: { summary: ModuleSummary; onClose: () => void; onSaved: () => void }) {
  const modules = useApi(() => api.listModules(), []);
  const module = modules.data?.find((x) => x.id === summary.module_id);
  const [values, setValues] = useState<Record<string, string>>({});
  const [added, setAdded] = useState<{ key: string; value: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');

  const entries = module ? Object.entries(module.config) : [];
  const missing = module?.type === 'external_api'
    ? ['url', 'key_param', 'api_key'].filter((k) => !(k in module.config) && !added.some((a) => a.key === k))
    : [];

  const changes = (m: ModuleOut) => {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(values)) {
      const before = m.config[k];
      if (before === MASK) {
        if (v.trim()) out[k] = v.trim();
      } else if (v !== before) {
        out[k] = v;
      }
    }
    for (const a of added) if (a.key.trim() && a.value.trim()) out[a.key.trim()] = a.value.trim();
    return out;
  };

  const save = async () => {
    if (!module) return;
    const config = changes(module);
    if (Object.keys(config).length === 0) {
      setNote('바꾼 항목이 없습니다.');
      return;
    }
    setBusy(true);
    setError('');
    setNote('');
    try {
      await api.updateModule(module, config);
      onSaved();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const setAddedAt = (i: number, patch: Partial<{ key: string; value: string }>) =>
    setAdded(added.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  return (
    <Modal title={`설정 — ${summary.agent_name} (${summary.env_prefix})`} onClose={onClose} wide>
      <Async state={modules}>
        {() => !module ? <p className="error">모듈을 찾지 못했습니다(삭제됐을 수 있습니다).</p> : (
          <>
            <table>
              <thead><tr><th>항목</th><th>값</th></tr></thead>
              <tbody>
                {entries.map(([k, v]) => (
                  <tr key={k}>
                    <td className="mono" style={{ whiteSpace: 'nowrap' }}>{k}</td>
                    <td>
                      {k === 'catalog' || typeof v !== 'string' ? (
                        <span className="mono mutedtext">{typeof v === 'string' ? v : JSON.stringify(v)}</span>
                      ) : (
                        <input
                          style={{ width: '100%' }}
                          type={v === MASK ? 'password' : 'text'}
                          placeholder={v === MASK ? '저장됨 — 바꿀 때만 입력' : HINT[k] ?? ''}
                          value={values[k] ?? (v === MASK ? '' : v)}
                          onChange={(e) => setValues({ ...values, [k]: e.target.value })}
                        />
                      )}
                      {HINT[k] && <div className="mutedtext" style={{ fontSize: 11 }}>{HINT[k]}</div>}
                    </td>
                  </tr>
                ))}
                {added.map((a, i) => (
                  <tr key={`new-${i}`}>
                    <td>
                      <input className="mono" placeholder="항목 이름" value={a.key}
                        onChange={(e) => setAddedAt(i, { key: e.target.value })} />
                    </td>
                    <td>
                      <input style={{ width: '100%' }} type={/key|secret|password|token/i.test(a.key) ? 'password' : 'text'}
                        placeholder={HINT[a.key.trim()] ?? '값'} value={a.value}
                        onChange={(e) => setAddedAt(i, { value: e.target.value })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
              {missing.map((k) => (
                <button key={k} className="small secondary" onClick={() => setAdded([...added, { key: k, value: '' }])}>
                  + {k}
                </button>
              ))}
              <button className="small secondary" onClick={() => setAdded([...added, { key: '', value: '' }])}>+ 항목 추가</button>
              <div style={{ flex: 1 }} />
              <button disabled={busy} onClick={() => void save()}>{busy ? '저장 중…' : '저장'}</button>
            </div>
            <p className="mutedtext" style={{ fontSize: 12 }}>
              게이트웨이(/proxy/modules/{summary.agent_name})는 저장하자마자 이 값으로 부르고, 환경변수는 다음 배포부터
              주입됩니다. 이 모듈을 쓰는 다른 과제에도 같이 적용됩니다.
            </p>
            {note && <p className="mutedtext">{note}</p>}
            {error && <p className="error">{error}</p>}
          </>
        )}
      </Async>
    </Modal>
  );
}
