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
  // 모델 ID는 손으로 적으면 틀린다(리전에 따라 inference profile ID를 넣어야 한다) —
  // 로그인된 자격증명으로 계정에서 받아 고르게 한다. 프로필을 고른 뒤에만 읽는다.
  // base_url은 deps에 넣지 않는다 — 한 글자마다 다시 부르게 된다(리전은 프로필에도 있다).
  const awsModels = useApi(
    () => (form.kind === 'aws' && form.aws_profile
      ? api.listAwsModels(form.aws_profile, form.base_url)
      : Promise.resolve(null)),
    [form.kind, form.aws_profile],
  );
  const modelOptions = awsModels.data?.models ?? [];
  // 만료됐을 때 서버에서 SSO 로그인을 시작한다. 승인은 사람이 브라우저에서 해야 하므로
  // (SSO는 그렇게 설계돼 있다) 주소·코드를 띄우고, 승인이 끝났는지는 프로필 상태를 다시
  // 물어 확인한다 — 이 응답만으로는 알 수 없다(프로세스가 기다리는 중이다).
  const [login, setLogin] = useState<
    { url: string; code: string; autofilled: boolean; tail: string } | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);

  const startLogin = async () => {
    if (!form.aws_profile) return;
    setLoggingIn(true);
    setLogin(null);
    try {
      const r = await api.startAwsSsoLogin(form.aws_profile);
      setLogin({
        url: r.verification_url, code: r.user_code,
        autofilled: r.code_autofilled, tail: r.log_tail,
      });
      if (r.verification_url) window.open(r.verification_url, '_blank', 'noopener');
      // 승인을 기다린다 — 되면 프로필 상태가 ok로 바뀌고 모델 목록도 따라 열린다.
      // **직접 물어본다.** useApi의 reload()는 Promise가 아니고, 이 루프가 잡고 있는
      // awsProfiles.data는 렌더 시점 값으로 굳어서 영원히 바뀌지 않는다(종료 조건이 안 걸린다).
      // 무한히 돌지 않는다: 2분 안에 안 되면 사람이 다시 누르는 편이 낫다.
      for (let i = 0; i < 24; i += 1) {
        await new Promise((done) => setTimeout(done, 5000));
        const fresh = await api.listAwsProfiles();
        if (fresh.profiles.find((p) => p.name === form.aws_profile)?.ok) {
          awsProfiles.reload();  // 화면(상태·모델 목록)을 갱신한다
          break;
        }
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoggingIn(false);
    }
  };
  // 프로필을 고르면 Endpoint는 리전에서 정해진다 — 그때만 입력을 선택으로 푼다.
  const endpointFromProfile = form.kind === 'aws' && Boolean(form.aws_profile);
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
                  <th>기본값</th>
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
                    {/* 배포 점검·실패 원인 분석·레포 검토는 버튼 하나로 도는 자리라
                        모델을 물을 곳이 없다 — 그 자리에서 쓰는 모델을 여기서 한 번 정한다. */}
                    <td>
                      {p.is_default ? (
                        <span title="배포 점검·실패 원인 분석·레포 검토가 이 모델로 돕니다">
                          ★ 기본
                        </span>
                      ) : admin ? (
                        <button
                          className="small secondary"
                          onClick={async () => {
                            try {
                              await api.setDefaultProvider(p.id);
                              state.reload();
                            } catch (err) {
                              alert((err as Error).message);
                            }
                          }}
                        >
                          기본값으로
                        </button>
                      ) : '-'}
                    </td>
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
            {/* Endpoint가 필요한지는 고른 프로필이 정한다. Bedrock을 자격증명으로 부를
                때는 리전이 주소를 결정하므로 사람이 적을 값이 아니다. */}
            <label className="field">
              Endpoint URL
              {endpointFromProfile && (
                <span className="mutedtext" style={{ fontWeight: 400, fontSize: 12 }}>
                  {' '}— 비워 두면 프로필 리전으로 정해집니다
                  {selectedProfile?.region && (
                    <>: <span className="mono">
                      https://bedrock-runtime.{selectedProfile.region}.amazonaws.com
                    </span></>
                  )}
                </span>
              )}
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
                required={!endpointFromProfile}
              />
            </label>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                모델
                {/* 목록을 불러오는 동안 입력칸이 그대로 보이면 "콤보박스가 안 나온다"로
                    읽힌다 — 무엇을 기다리는지 말한다. */}
                {form.kind === 'aws' && form.aws_profile && awsModels.loading && (
                  <span className="mutedtext" style={{ fontWeight: 400, fontSize: 12 }}>
                    {' '}— 계정에서 모델 목록을 불러오는 중…
                  </span>
                )}
                {modelOptions.length > 0 ? (
                  <select
                    className="mono"
                    value={form.model}
                    onChange={(e) => set('model', e.target.value)}
                    required
                  >
                    <option value="">— 선택 —</option>
                    {/* 추론 프로필이 먼저다. 실측(ap-northeast-2): 파운데이션 모델 ID를
                        그대로 넣으면 "on-demand throughput isn't supported — use an
                        inference profile"로 거부된다. 실제로 통하는 ID가 앞에 있어야 한다. */}
                    <optgroup label={`추론 프로필 (권장 · ${awsModels.data?.region ?? ''})`}>
                      {modelOptions.filter((m) => m.kind === 'inference_profile').map((m) => (
                        <option key={m.id} value={m.id}>{m.id}</option>
                      ))}
                    </optgroup>
                    <optgroup label="온디맨드 모델">
                      {modelOptions.filter((m) => m.kind === 'on_demand').map((m) => (
                        <option key={m.id} value={m.id}>{m.id}</option>
                      ))}
                    </optgroup>
                  </select>
                ) : (
                  <input
                    className="mono"
                    value={form.model}
                    onChange={(e) => set('model', e.target.value)}
                    required
                    placeholder={form.kind === 'aws'
                      ? (form.aws_profile
                        ? '모델 목록을 받지 못했습니다 — ID를 직접 입력하세요'
                        : '프로필을 고르면 계정에서 모델 목록을 받아옵니다')
                      : undefined}
                  />
                )}
              </label>
              {/* Bedrock은 붙여넣을 정적 키가 없다 — 서버 ~/.aws의 자격증명으로 서명한다.
                  그래서 aws에서는 키 입력 대신 프로필을 고른다. */}
              {form.kind === 'aws' ? (
                <label className="field" style={{ flex: 1 }}>
                  AWS 자격증명 프로필
                  {/* 읽은 경로는 목록이 있을 때도 보여 준다 — 서비스 계정 홈을 보고 있으면
                      목록이 나오더라도 로그인한 계정의 것이 아닐 수 있다. */}
                  {awsProfiles.data?.config_path && (
                    <span className="mutedtext" style={{ fontWeight: 400, fontSize: 12 }}>
                      {' '}— <span className="mono">{awsProfiles.data.config_path}</span>
                      {!awsProfiles.data.config_exists && ' (파일 없음)'}
                    </span>
                  )}
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
                {awsProfiles.loading ? (
                  <>서버 <span className="mono">~/.aws</span>를 읽는 중…</>
                ) : awsProfiles.error ? (
                  /* 조회 자체가 실패한 것을 "프로필이 없습니다"로 말하면 엉뚱한 곳을 찾게
                     된다(백엔드가 옛 코드면 404다) — 받은 오류를 그대로 보여 준다. */
                  <>❌ 프로필 목록을 받지 못했습니다: {awsProfiles.error}</>
                ) : awsProfiles.data && !awsProfiles.data.botocore_available ? (
                  /* venv에 설치했는데도 이 문구가 남는 경우가 있다 — 백엔드가 그 venv가 아닌
                     다른 인터프리터로 돌고 있으면 그쪽 site-packages를 보지 않는다. 그래서
                     "pip install"이 아니라 **찾고 있는 인터프리터**로 명령을 못 박는다. */
                  <>⚠️ 서버에 botocore가 없어 자격증명을 쓸 수 없습니다. 백엔드가 쓰는
                    인터프리터에 설치해야 합니다:{' '}
                    <span className="mono">
                      "{awsProfiles.data.python}" -m pip install botocore
                    </span></>
                ) : (awsProfiles.data?.profiles ?? []).length === 0 ? (
                  /* 경로를 밝힌다 — 서비스로 돌면 홈이 서비스 계정 것이라(nssm 기본값은
                     LocalSystem) `aws sso login`을 해도 목록이 빈다. 경로를 안 보여 주면
                     왜 비었는지 알 방법이 없다. */
                  <>⚠️ 프로필이 없습니다 —{' '}
                    {awsProfiles.data?.config_exists
                      ? '위 경로에 파일은 있는데 프로필 섹션이 없습니다.'
                      : '위 경로에 파일이 없습니다'}
                    {!awsProfiles.data?.config_exists && (
                      <>{' '}— 백엔드가 서비스로 돌면 홈이 로그인한 계정이 아니라 서비스 계정의
                        것입니다. 그 계정으로 <span className="mono">aws sso login</span>을
                        하거나, 서비스 환경변수{' '}
                        <span className="mono">AWS_CONFIG_FILE</span>·
                        <span className="mono">USERPROFILE</span>을 실제 홈으로 지정하세요.</>
                    )}</>
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
                    )}
                    {/* 서버에 원격 접속해 명령을 치지 않아도 되게 — 플랫폼이 로그인을
                        시작하고 사람은 브라우저에서 코드만 승인한다. */}
                    <button
                      type="button"
                      className="secondary small"
                      style={{ marginLeft: 8 }}
                      disabled={loggingIn}
                      onClick={startLogin}
                    >
                      {loggingIn ? '승인 대기 중...' : '서버에서 SSO 로그인'}
                    </button>
                    {login && (
                      <div style={{ marginTop: 6 }}>
                        {login.url ? (
                          <>
                            {/* 코드가 박힌 주소면 옮겨 적을 것이 없다 — '허용' 한 번이 전부다. */}
                            열린 브라우저에서{' '}
                            {login.autofilled ? <b>[허용]을 누르면 끝입니다</b> : (
                              <>코드 <b className="mono">{login.code}</b>를 입력하고 허용하세요</>
                            )}
                            . 창이 닫혔으면{' '}
                            <a href={login.url} target="_blank" rel="noopener">이 주소</a>를
                            다시 여세요.{' '}승인되면 이 화면이 스스로 갱신됩니다.
                          </>
                        ) : (
                          /* 주소를 못 뽑았다 — CLI 문구가 바뀌었거나 오류다. 감추지 않는다. */
                          <>승인 주소를 읽지 못했습니다. 서버 로그:
                            <pre className="mono" style={{ fontSize: 11, whiteSpace: 'pre-wrap' }}>
                              {login.tail}
                            </pre>
                          </>
                        )}
                      </div>
                    )}</>
                ) : selectedProfile.ok === null ? (
                  <>❓ {selectedProfile.reason}</>
                ) : awsModels.error ? (
                  /* 자격증명은 되는데 목록을 못 받았다 — 권한(bedrock:ListFoundationModels)이나
                     리전 문제다. 모델 칸은 직접 입력으로 남는다. */
                  <>⚠️ 모델 목록을 받지 못했습니다: {awsModels.error}{' '}
                    — 모델 ID를 직접 입력하세요.</>
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
