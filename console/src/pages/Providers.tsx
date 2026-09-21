import { useState } from 'react';
import Async from '../components/Async';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';

export default function Providers() {
  const state = useApi(() => api.listProviders());
  const orgs = useApi(() => api.listOrgs());
  const admin = isAdmin();
  // 서버 ~/.aws의 프로필. admin 전용 엔드포인트라 일반 사용자에게는 아예 부르지 않는다
  // (403을 받아 화면에 오류만 남는다).
  const awsProfiles = useApi(() => (admin ? api.listAwsProfiles() : Promise.resolve(null)), []);
  const [form, setForm] = useState({
    name: '', kind: 'openai', base_url: '', api_key: '', model: '',
    aws_profile: '', organization_id: '',
  });
  const selectedProfile = (awsProfiles.data?.profiles ?? [])
    .find((p) => p.name === form.aws_profile);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api.createProvider({
        ...form,
        api_key: form.api_key || undefined,
        // 종류를 aws에서 바꿔 놓고 제출하면 서버가 거부한다 — 안 쓰는 값은 보내지 않는다.
        aws_profile: form.kind === 'aws' ? form.aws_profile || undefined : undefined,
        organization_id: form.organization_id ? Number(form.organization_id) : null,
      });
      setForm({
        name: '', kind: 'openai', base_url: '', api_key: '', model: '',
        aws_profile: '', organization_id: '',
      });
      state.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>LLM 프로바이더</h2>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          내부(internal)는 base_url에 <span className="mono">project://llm-프로젝트명</span>을
          쓰면 배포 도메인으로 자동 해석됩니다 — 소스가 사내망을 벗어나지 않는 모드.
        </p>
        <Async state={state} empty="등록된 프로바이더가 없습니다.">
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>이름</th>
                  <th>구분</th>
                  <th>Endpoint</th>
                  <th>모델</th>
                  <th>인증</th>
                  <th>사용 범위</th>
                  {admin && <th>작업</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    <td>{p.name}</td>
                    <td>
                      <StatusPill value={p.kind === 'internal' ? 'release' : 'proposed'} />{' '}
                      <span style={{ fontWeight: 600, textTransform: 'uppercase', fontSize: 12 }}>{p.kind}</span>
                    </td>
                    <td className="mono">{p.base_url}</td>
                    <td className="mono">{p.model}</td>
                    <td>
                      {p.aws_profile
                        ? <span className="mono" title="AWS 자격증명 프로필로 SigV4 서명">🔑 {p.aws_profile}</span>
                        : p.has_api_key ? 'API 키 설정됨' : '-'}
                    </td>
                    <td>{p.org_name ? `🏢 ${p.org_name}` : '전역'}</td>
                    {admin && (
                      <td>
                        <button
                          className="small danger"
                          onClick={async () => {
                            if (confirm(`정말로 LLM 프로바이더 '${p.name}'을(를) 삭제하시겠습니까?`)) {
                              try {
                                await api.deleteProvider(p.id);
                                state.reload();
                              } catch (err) {
                                alert((err as Error).message);
                              }
                            }
                          }}
                        >
                          삭제
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </div>
      {admin && (
        <div className="panel">
          <h2>프로바이더 등록 (admin)</h2>
          <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                이름
                <input value={form.name} onChange={(e) => set('name', e.target.value)} required />
              </label>
              <label className="field">
                프로바이더 종류
                <select value={form.kind} onChange={(e) => set('kind', e.target.value)}>
                  <option value="openai">openai — OpenAI Official API</option>
                  <option value="anthropic">anthropic — Anthropic Claude API</option>
                  <option value="aws">aws — AWS Bedrock</option>
                  <option value="azure">azure — Azure OpenAI Service</option>
                  <option value="gcp">gcp — GCP Vertex AI / Gemini API</option>
                  <option value="internal">internal — 사내 배포 LLM (vLLM / Ollama)</option>
                </select>
              </label>
            </div>
            <label className="field">
              Endpoint URL
              <input
                className="mono"
                value={form.base_url}
                onChange={(e) => set('base_url', e.target.value)}
                placeholder={
                  form.kind === 'internal'
                    ? 'project://llm-main'
                    : form.kind === 'aws'
                    ? 'https://bedrock-runtime.us-east-1.amazonaws.com'
                    : form.kind === 'azure'
                    ? 'https://my-resource.openai.azure.com'
                    : form.kind === 'gcp'
                    ? 'https://generativelanguage.googleapis.com'
                    : form.kind === 'anthropic'
                    ? 'https://api.anthropic.com'
                    : 'https://api.openai.com/v1'
                }
                required
              />
            </label>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                모델
                <input
                  className="mono"
                  value={form.model}
                  onChange={(e) => set('model', e.target.value)}
                  required
                />
              </label>
              {/* Bedrock은 붙여넣을 정적 키가 없다 — 서버 ~/.aws의 자격증명으로 서명한다.
                  그래서 aws에서는 키 입력 대신 프로필을 고른다. */}
              {form.kind === 'aws' ? (
                <label className="field" style={{ flex: 1 }}>
                  AWS 자격증명 프로필
                  <select
                    className="mono"
                    value={form.aws_profile}
                    onChange={(e) => set('aws_profile', e.target.value)}
                  >
                    <option value="">(프로필 없이 — Endpoint가 OpenAI 호환 게이트웨이인 경우)</option>
                    {(awsProfiles.data?.profiles ?? []).map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.name}{p.region ? ` (${p.region})` : ''}{p.sso_session ? ' · SSO' : ''}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <label className="field" style={{ flex: 1 }}>
                  API 키 (선택 — 암호화 저장)
                  <input
                    type="password"
                    value={form.api_key}
                    onChange={(e) => set('api_key', e.target.value)}
                  />
                </label>
              )}
            </div>
            {/* 자격증명이 지금 유효한지. SSO 토큰은 보통 8시간이면 만료되고, 만료되면
                호출이 실패한다 — 그때 무엇을 해야 하는지를 등록 시점에도 보여 둔다. */}
            {form.kind === 'aws' && (
              <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                {awsProfiles.data && !awsProfiles.data.botocore_available ? (
                  <>⚠️ 서버에 botocore가 없어 자격증명을 쓸 수 없습니다 —{' '}
                    <span className="mono">pip install botocore</span> 후 백엔드를 재시작하세요.</>
                ) : (awsProfiles.data?.profiles ?? []).length === 0 ? (
                  /* 경로를 밝힌다 — 서비스로 돌면 홈이 서비스 계정 것이라(nssm 기본값은
                     LocalSystem) `aws sso login`을 해도 목록이 빈다. 경로를 안 보여 주면
                     왜 비었는지 알 방법이 없다. */
                  <>⚠️ 프로필이 없습니다. 읽은 경로:{' '}
                    <span className="mono">{awsProfiles.data?.config_path}</span>
                    {' '}— 이 경로가 로그인한 계정의 것이 아니면(서비스 계정) 그 계정으로{' '}
                    <span className="mono">aws sso login</span>을 하거나{' '}
                    <span className="mono">AWS_CONFIG_FILE</span>을 지정하세요.</>
                ) : !selectedProfile ? (
                  <>프로필을 고르면 자격증명이 지금 유효한지 확인해 보여 줍니다.</>
                ) : selectedProfile.ok === false ? (
                  /* 서버가 준 사유에 이미 재로그인 명령이 들어 있는 경우가 많다(만료·미로그인) —
                     그때 또 붙이면 같은 명령이 두 번 나온다. 없을 때만 덧붙인다. */
                  <>❌ {selectedProfile.reason}
                    {selectedProfile.expires_at
                      && ` (SSO 토큰 만료: ${new Date(selectedProfile.expires_at).toLocaleString()})`}
                    {!selectedProfile.reason.includes(selectedProfile.login_command) && (
                      <>{' '}서버에서{' '}
                        <span className="mono">{selectedProfile.login_command}</span></>
                    )}</>
                ) : selectedProfile.ok === null ? (
                  <>❓ {selectedProfile.reason}</>
                ) : (
                  /* 자격증명(STS) 만료가 아니라 **SSO 토큰** 만료다 — 재로그인이
                     필요해지는 시각은 이쪽이고 보통 8시간이다. */
                  <>✅ 자격증명 유효
                    {selectedProfile.expires_at
                      && ` · SSO 토큰 만료 ${new Date(selectedProfile.expires_at).toLocaleString()}`}
                    {' '}· 만료되면 서버에서{' '}
                    <span className="mono">{selectedProfile.login_command}</span>
                  </>
                )}
              </p>
            )}
            <label className="field">
              사용 범위 (선택 — 비우면 전역, 모든 프로젝트에서 사용 가능)
              <select value={form.organization_id} onChange={(e) => set('organization_id', e.target.value)}>
                <option value="">전역 (모든 조직)</option>
                {(orgs.data ?? []).map((o) => (
                  <option key={o.id} value={o.id}>🏢 {o.name}</option>
                ))}
              </select>
            </label>
            {error && <p className="error">{error}</p>}
            <div className="row">
              <button type="submit" disabled={busy}>
                {busy ? '등록 중...' : '등록'}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
