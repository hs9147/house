import { useState } from 'react';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { PlanArtifactOut, PlanBuildEvent, PlanSessionOut } from '../lib/types';

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  usedModules?: string[];
}

// 에이전트 기획 4단계(진행단계 표시) — 순차 진행, 앞 단계 확정을 전제로 한다.
const STAGES: { key: PlanArtifactOut['stage']; label: string }[] = [
  { key: 'spec', label: '① 기획서' },
  { key: 'architecture', label: '② 아키텍처 설계' },
  { key: 'solution', label: '③ 솔루션 구성' },
  { key: 'principles', label: '④ 개발원칙' },
];

export default function AgentPlanning() {
  const me = useApi(() => api.me());
  const projects = useApi(() => api.listProjects());
  const providers = useApi(() => api.listProviders());

  const userOrgs = me.data?.organizations ?? [];
  const userOrgIds = userOrgs.map((o) => o.id);
  const orgNamesLabel = userOrgs.length > 0 ? userOrgs.map((o) => o.name).join(', ') : me.data?.organization_name;

  // 사용자가 소속된 조직들의 프로젝트만 노출(관리자/미지정이면 전체)
  const availableProjects = (projects.data ?? []).filter((p) => {
    if (userOrgIds.length === 0 || me.data?.is_admin) return true;
    return p.organization_id != null && userOrgIds.includes(p.organization_id);
  });

  const [projectId, setProjectId] = useState('');
  const [providerId, setProviderId] = useState('');
  const [branch, setBranch] = useState('');
  const [session, setSession] = useState<PlanSessionOut | null>(null);
  const [activeStage, setActiveStage] = useState<PlanArtifactOut['stage']>('spec');
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [files, setFiles] = useState('');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [buildEvents, setBuildEvents] = useState<PlanBuildEvent[]>([]);

  const artifactOf = (stage: string) => session?.artifacts.find((a) => a.stage === stage);
  const isConfirmed = (stage: string) => !!artifactOf(stage)?.confirmed;
  const stageIndex = (stage: string) => STAGES.findIndex((s) => s.key === stage);
  // 앞 단계가 모두 확정돼야 진입 가능(진행단계 순차 강제)
  const stageUnlocked = (stage: string) =>
    STAGES.slice(0, stageIndex(stage)).every((s) => isConfirmed(s.key));

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    try {
      const s = await api.createPlanSession(Number(projectId), Number(providerId), branch.trim() || undefined);
      setSession(s);
      setActiveStage('spec');
      setMessages([]);
      setDraft('');
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const refreshSession = async () => {
    if (!session) return;
    setSession(await api.getPlanSession(session.id));
  };

  const selectStage = (stage: PlanArtifactOut['stage']) => {
    if (!stageUnlocked(stage)) return;
    setActiveStage(stage);
    setMessages([]);
    setDraft('');
    setError('');
  };

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!session || !input.trim()) return;
    const content = input.trim();
    setMessages((prev) => [...prev, { role: 'user', content }]);
    setInput('');
    setBusy(true);
    setError('');
    try {
      const fileList = files.split(',').map((f) => f.trim()).filter(Boolean);
      const res = await api.sendPlanMessage(session.id, activeStage, content, fileList);
      setMessages((prev) => [...prev, { role: 'assistant', content: res.reply, usedModules: res.used_modules }]);
      setDraft(res.reply); // 확정용 편집 초안으로 채운다
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!session || !draft.trim()) return;
    setBusy(true);
    setError('');
    try {
      await api.confirmPlanStage(session.id, activeStage, draft);
      await refreshSession();
      // 다음 단계로 자동 이동
      const next = STAGES[stageIndex(activeStage) + 1];
      if (next) {
        setActiveStage(next.key);
        setMessages([]);
        setDraft('');
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const loadBuildStatus = async () => {
    if (!session) return;
    try {
      const s = await api.planBuildStatus(session.id);
      setBuildEvents(s.events);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="row" style={{ alignItems: 'center', gap: 10 }}>
          <h2 style={{ margin: 0 }}>🧭 에이전트 기획 (Agent Planning)</h2>
          <span style={{ fontSize: 12, padding: '2px 8px', borderRadius: 4, background: 'rgba(56, 189, 248, 0.15)', color: '#38bdf8', border: '1px solid rgba(56, 189, 248, 0.3)' }}>
            기획 단계 순차 수행 · 산출물 Gitea 저장
          </span>
        </div>
        <p className="mutedtext" style={{ fontSize: 12, marginTop: 6, marginBottom: 12 }}>
          코딩 전에 기획서 → 아키텍처 → 솔루션 구성 → 개발원칙을 순서대로 확정합니다. 확정 산출물은
          프로젝트 Gitea 리포에 커밋되어 외부 개발도구(VSCode·Claude·Antigravity)에서 그대로 활용합니다.
        </p>

        <form className="row" onSubmit={start}>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} required>
            <option value="">
              {orgNamesLabel ? `🏢 [${orgNamesLabel}] 프로젝트 선택...` : '프로젝트 선택...'}
            </option>
            {availableProjects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
          <select value={providerId} onChange={(e) => setProviderId(e.target.value)} required>
            <option value="">LLM 프로바이더 선택...</option>
            {(providers.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>{p.name} ({p.kind})</option>
            ))}
          </select>
          <input
            className="mono"
            placeholder="작업 브랜치 (선택)"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            style={{ width: 200 }}
          />
          <button type="submit">신규 기획 세션 시작</button>
          {session && (
            <span className="mutedtext" style={{ fontSize: 12 }}>
              세션 #{session.id} · 브랜치 <span className="mono">{session.branch}</span> · {session.provider}
            </span>
          )}
        </form>
      </div>

      {session && (
        <>
          {/* 진행단계 표시 */}
          <div className="panel">
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {STAGES.map((s) => {
                const confirmed = isConfirmed(s.key);
                const unlocked = stageUnlocked(s.key);
                const active = s.key === activeStage;
                return (
                  <button
                    key={s.key}
                    onClick={() => selectStage(s.key)}
                    disabled={!unlocked}
                    className={active ? 'primary small' : 'secondary small'}
                    style={{ opacity: unlocked ? 1 : 0.45 }}
                    title={unlocked ? '' : '앞 단계를 먼저 확정하세요'}
                  >
                    {confirmed ? '✅ ' : unlocked ? '' : '🔒 '}{s.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>
                {STAGES.find((s) => s.key === activeStage)?.label} 단계
                {isConfirmed(activeStage) && <span style={{ color: '#10b981', fontSize: 13 }}> · 확정됨 ({artifactOf(activeStage)?.commit_sha?.substring(0, 7)})</span>}
              </h3>
              <span className="mutedtext" style={{ fontSize: 12 }}>
                저장 경로 <span className="mono">{artifactOf(activeStage)?.repo_path}</span>
              </span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, margin: '12px 0' }}>
              {messages.map((m, i) => (
                <div
                  key={i}
                  style={{
                    alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
                    maxWidth: '90%',
                    background: m.role === 'user' ? 'rgba(59, 130, 246, 0.15)' : 'var(--panel-bg)',
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    padding: 12,
                  }}
                >
                  <div className="mutedtext" style={{ fontSize: 11, marginBottom: 4 }}>
                    {m.role === 'user' ? '👤 나 (User)' : '🧭 기획 에이전트'}
                  </div>
                  {m.usedModules && m.usedModules.length > 0 && (
                    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
                      <span className="mutedtext" style={{ fontSize: 11 }}>참조된 모듈:</span>
                      {m.usedModules.map((mod) => (
                        <span key={mod} style={{ fontSize: 10, padding: '1px 6px', borderRadius: 4, background: 'rgba(56, 189, 248, 0.2)', color: '#38bdf8' }}>{mod}</span>
                      ))}
                    </div>
                  )}
                  <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{m.content}</div>
                </div>
              ))}
            </div>

            <form onSubmit={send} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <input
                className="mono"
                placeholder="참조할 파일 경로들 (쉼표 구분, 선택)"
                value={files}
                onChange={(e) => setFiles(e.target.value)}
              />
              <div className="row">
                <textarea
                  style={{ flex: 1, minHeight: 70, fontFamily: 'inherit' }}
                  placeholder={`${STAGES.find((s) => s.key === activeStage)?.label} 초안 생성을 요청하세요...`}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                />
                <button type="submit" className="primary" disabled={busy || !input.trim()}>
                  {busy ? '처리 중...' : '초안 생성'}
                </button>
              </div>
            </form>

            {/* 확정용 산출물 편집기 */}
            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                📄 확정할 산출물 (마크다운) — 검토·수정 후 확정하면 Gitea에 커밋됩니다
              </div>
              <textarea
                className="mono"
                style={{ width: '100%', minHeight: 220, fontSize: 12 }}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="초안 생성 후 여기서 편집하거나 직접 작성하세요."
              />
              <div className="row" style={{ marginTop: 8 }}>
                <button className="primary" onClick={confirm} disabled={busy || !draft.trim()}>
                  ✅ 이 단계 확정 (Gitea 커밋)
                </button>
              </div>
            </div>
          </div>

          {/* 외부 빌드 모니터링 */}
          <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>🛠️ 외부 빌드 모니터링</h3>
              <button className="secondary small" onClick={loadBuildStatus}>새로고침</button>
            </div>
            <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
              빌드는 외부 개발도구에서 수행됩니다. 커밋(Gitea 웹훅)과 모듈 사용·진행 보고(MCP)가 아래에 집계됩니다.
            </p>
            {buildEvents.length === 0 ? (
              <p className="mutedtext" style={{ fontSize: 12 }}>수집된 이벤트가 없습니다.</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                {buildEvents.map((e, i) => (
                  <li key={i}>
                    <span className="mono">{e.action}</span> · {e.actor}
                    {e.created_at && <span className="mutedtext"> · {e.created_at}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      {error && (
        <div style={{ padding: 12, borderRadius: 6, background: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', border: '1px solid rgba(239, 68, 68, 0.3)', marginTop: 16 }}>
          ⚠️ {error}
        </div>
      )}
    </>
  );
}
