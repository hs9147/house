import { useState } from 'react';
import Async from './Async';
import Modal from './Modal';
import { api } from '../lib/api';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { ModuleOut, ModuleUsage, OrgOut } from '../lib/types';

const MASK = '•••';
// 어떤 항목인지 — 외부 API는 이 셋이면 부를 수 있다.
const HINT: Record<string, string> = {
  url: '실제 호출 주소(예: https://apis.data.go.kr/…/오퍼레이션)',
  key_param: '쿼리 인자로 키를 받는 API의 인자 이름(serviceKey · crtfc_key · authkey) — 비우면 헤더로 붙인다',
  api_key: '인증키 — 공공데이터포털은 디코딩 키',
};

/** 이 모듈을 쓰는 에이전트 — 바인딩한 과제, 노드로 쓰는 워크플로, 최근 게이트웨이로 부른 키. */
export function ModuleUsageList({ usage }: { usage: ModuleUsage }) {
  const none = !usage.bindings.length && !usage.workflows.length && !usage.callers.length;
  if (none) return <p className="mutedtext">쓰는 에이전트가 없습니다(바인딩·워크플로 없음, 최근 {usage.days}일 호출 없음).</p>;
  return (
    <table>
      <thead><tr><th>구분</th><th>에이전트</th><th>비고</th></tr></thead>
      <tbody>
        {usage.bindings.map((b) => (
          <tr key={`b-${b.project_id}-${b.env_prefix}`}>
            <td>바인딩</td><td>{b.project_name}</td><td className="mono">{b.env_prefix}</td>
          </tr>
        ))}
        {usage.workflows.map((w) => (
          <tr key={`w-${w.id}`}><td>워크플로</td><td>{w.name}</td><td /></tr>
        ))}
        {usage.callers.map((c) => (
          <tr key={`c-${c.actor}`}>
            <td>최근 호출</td>
            <td className="mono">{c.actor}</td>
            <td className="mutedtext">{usage.days}일 {c.calls}회 · 마지막 {fmtDate(c.last_called_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * 모듈 설정 — 항목마다 값을 고치고 새 항목을 더한다. 비밀 항목(키·비밀번호)은 저장된 값을 보여 주지 않는다:
 * 빈칸이면 그대로 두고, 입력하면 바꾼다. 바꾼 항목만 보낸다(서버가 기존 설정에 합친다). catalog는 카탈로그의 어느
 * 항목에서 왔는지라 고치지 않는다(같은 항목을 다시 바인딩하면 이 모듈을 찾는 표시다).
 *
 * meta(도구 관리의 「수정」)면 이름·카테고리·범위도 고치고, 이 모듈을 쓰는 에이전트를 함께 보여 준다 — 이름을
 * 바꾸면 게이트웨이 주소(/proxy/modules/{이름})가 바뀌고, 범위를 좁히면 바인딩하지 않은 키는 못 부른다.
 */
export default function ModuleSettings({ moduleId, title, meta = false, onClose, onSaved }: {
  moduleId: number;
  title: string;
  meta?: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const modules = useApi(() => api.listModules(), []);
  const module = modules.data?.find((x) => x.id === moduleId);
  const orgs = useApi<OrgOut[]>(() => (meta ? api.listOrgs() : Promise.resolve([])), [meta]);
  const usage = useApi<ModuleUsage | null>(() => (meta ? api.moduleUsage(moduleId) : Promise.resolve(null)), [meta, moduleId]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [added, setAdded] = useState<{ key: string; value: string }[]>([]);
  const [head, setHead] = useState<{ name?: string; category?: string; organization_id?: number | null }>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');

  const entries = module ? Object.entries(module.config) : [];
  const missing = module?.type === 'external_api'
    ? ['url', 'key_param', 'api_key'].filter((k) => !(k in module.config) && !added.some((a) => a.key === k))
    : [];
  const name = head.name ?? module?.name ?? '';
  const category = head.category ?? module?.category ?? '';
  const orgId = head.organization_id !== undefined ? head.organization_id : module?.organization_id ?? null;
  const renamed = !!module && name.trim() !== module.name;
  const rescoped = !!module && orgId !== module.organization_id;

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
    const metaChanged = renamed || rescoped || category.trim() !== (module.category ?? '');
    if (Object.keys(config).length === 0 && !metaChanged) {
      setNote('바꾼 항목이 없습니다.');
      return;
    }
    const affected = usage.data && (usage.data.bindings.length + usage.data.workflows.length + usage.data.callers.length);
    if ((renamed || rescoped) && affected
        && !window.confirm(`'${module.name}'을 쓰는 에이전트 ${affected}곳이 영향을 받습니다. 그래도 저장하시겠습니까?`)) return;
    setBusy(true);
    setError('');
    setNote('');
    try {
      await api.updateModule(
        { ...module, name: name.trim(), category: category.trim() || null, organization_id: orgId }, config);
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
    <Modal title={title} onClose={onClose} wide>
      <Async state={modules}>
        {() => !module ? <p className="error">모듈을 찾지 못했습니다(삭제됐을 수 있습니다).</p> : (
          <>
            {meta && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
                <div className="row" style={{ gap: 10 }}>
                  <label className="field" style={{ flex: 1 }}>
                    도구 이름 — 소문자·숫자·하이픈
                    <input className="mono" value={name} pattern="[a-z0-9][a-z0-9-]{1,40}"
                      onChange={(e) => setHead({ ...head, name: e.target.value })} />
                  </label>
                  <label className="field" style={{ flex: 1 }}>
                    카테고리
                    <input value={category} onChange={(e) => setHead({ ...head, category: e.target.value })} />
                  </label>
                  <label className="field" style={{ flex: 1 }}>
                    범위
                    <select value={orgId ?? ''}
                      onChange={(e) => setHead({ ...head, organization_id: e.target.value ? Number(e.target.value) : null })}>
                      <option value="">전역 (모든 키가 부를 수 있음)</option>
                      {(orgs.data ?? []).map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
                    </select>
                  </label>
                </div>
                {renamed && (
                  <p className="error" style={{ margin: 0, fontSize: 12 }}>
                    이름을 바꾸면 게이트웨이 주소가 /proxy/modules/{name.trim()}(으)로 바뀝니다 — 옛 이름으로 부르는
                    에이전트는 404가 납니다.
                  </p>
                )}
                {rescoped && (
                  <p className="error" style={{ margin: 0, fontSize: 12 }}>
                    {orgId === null
                      ? '전역으로 바꾸면 바인딩하지 않은 키도 이 모듈의 인증키로 부를 수 있습니다(호출 한도를 함께 씁니다).'
                      : '조직으로 좁히면 그 조직 사람과 바인딩한 과제의 앱 키만 부를 수 있습니다 — 아래 최근 호출 키 중 그 밖의 것은 403이 납니다.'}
                  </p>
                )}
                <div>
                  <h4 style={{ margin: '4px 0' }}>쓰는 에이전트</h4>
                  <Async state={usage}>{(u) => (u ? <ModuleUsageList usage={u} /> : null)}</Async>
                </div>
              </div>
            )}
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
              게이트웨이(/proxy/modules/{module.name})는 저장하자마자 이 값으로 부르고, 환경변수는 다음 배포부터
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
