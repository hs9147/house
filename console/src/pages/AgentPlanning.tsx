import { useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type {
  BuildTaskOut, ComplianceOut, PlanArtifactContent, PlanArtifactOut, PlanBuildEvent,
  PlanSessionOut, PlanSessionSummary, ProjectOut,
} from '../lib/types';
import { CreateModal } from './Projects';

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  usedModules?: string[];
  contextFiles?: string[];
  boundModules?: string[];
}

// 확정 시 git 상태에 따라 자동 수행된 결과의 표시 문구
const GIT_ACTION_LABEL: Record<string, string> = {
  committed: '기본 브랜치에 직접 커밋',
  merged: 'PR 생성 후 자동 머지 완료',
  pr_opened: 'PR 생성됨 (자동 머지 불가 — 확인 필요)',
  skipped: 'PR 미수행',
};

// 작업 지시 상태 — 외부 빌더가 MCP로 갱신하고 콘솔에서도 바꿀 수 있다.
const TASK_STATUS: { key: BuildTaskOut['status']; label: string; color: string }[] = [
  { key: 'pending', label: '대기', color: '#94a3b8' },
  { key: 'in_progress', label: '진행', color: '#38bdf8' },
  { key: 'done', label: '완료', color: '#10b981' },
  { key: 'blocked', label: '차단', color: '#f59e0b' },
];

const COMPLIANCE_RULE_LABEL: Record<string, string> = {
  llm_direct: '외부 LLM 직접 호출',
  hardcoded_secret: '코드에 박힌 자격증명',
  unknown_module: '가용 목록 밖 모듈 호출',
};

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

  // 관리자만 모든 조직의 프로젝트를 본다. 일반 사용자는 소속 조직 프로젝트 +
  // 조직 미지정(전역) 프로젝트만 — 소속이 없다고 해서 다른 조직 프로젝트가 보이면 안 된다.
  const availableProjects = (projects.data ?? []).filter((p) => {
    if (me.data?.is_admin) return true;
    return p.organization_id == null || userOrgIds.includes(p.organization_id);
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
  const [gitResult, setGitResult] = useState<PlanArtifactOut | null>(null);
  const [tasks, setTasks] = useState<BuildTaskOut[]>([]);
  const [compliance, setCompliance] = useState<ComplianceOut | null>(null);
  const [draftSource, setDraftSource] = useState<PlanArtifactContent['source']>('');
  const history = useApi(() => api.listPlanSessions());

  // 프로젝트 페이지와 동일한 CreateModal(빈 프로젝트 옵션 포함)을 재사용한다.
  const [showCreate, setShowCreate] = useState(false);

  const handleProjectCreated = (created?: ProjectOut) => {
    setShowCreate(false);
    projects.reload();
    if (created) setProjectId(String(created.id)); // 생성 즉시 선택
  };

  const artifactOf = (stage: string) => session?.artifacts.find((a) => a.stage === stage);
  const isConfirmed = (stage: string) => !!artifactOf(stage)?.confirmed;
  // 서버가 내려준 단계별 기본 생성 요청 — 입력창 기본값이라 바로 '생성 요청'을 누를 수 있다.
  const defaultRequestOf = (s: PlanSessionOut | null, stage: string) =>
    s?.artifacts.find((a) => a.stage === stage)?.default_request ?? '';
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
      setGitResult(null);
      setInput(defaultRequestOf(s, 'spec'));
      await loadArtifact(s.id, 'spec'); // 리포에 이미 기획 문서가 있으면 그대로 불러온다
      history.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  // 기존 산출물이 있으면 편집기를 그 내용으로 채운다(없으면 비운다).
  const loadArtifact = async (sessionId: number, stage: string) => {
    try {
      const a = await api.planArtifactContent(sessionId, stage);
      setDraft(a.content);
      setDraftSource(a.source);
    } catch {
      setDraft('');
      setDraftSource('');
    }
  };

  // 이력에서 세션을 다시 연다 — 대화·산출물을 복원하고 첫 미확정 단계로 이동한다.
  const resume = async (row: PlanSessionSummary) => {
    setError('');
    try {
      const [s, msgs] = await Promise.all([
        api.getPlanSession(row.id),
        api.planSessionMessages(row.id),
      ]);
      const next = STAGES.find((st) => !s.artifacts.find((a) => a.stage === st.key)?.confirmed)
        ?? STAGES[STAGES.length - 1];
      setSession(s);
      setActiveStage(next.key);
      setMessages(msgs.map((m) => ({ role: m.role, content: m.content })));
      setGitResult(null);
      setCompliance(null);
      setInput(defaultRequestOf(s, next.key));
      await loadArtifact(row.id, next.key);
      setTasks(await api.listPlanTasks(row.id));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const removeSession = async (row: PlanSessionSummary) => {
    // confirm은 이 컴포넌트의 '단계 확정' 함수와 이름이 겹친다 — window.confirm을 명시한다.
    if (!window.confirm(
      `기획 세션 #${row.id} (${row.project_name})을(를) 삭제하시겠습니까?\n` +
      `대화·단계 확정 기록·작업 지시와 작업 브랜치(${row.branch})가 삭제됩니다.\n` +
      '기본 브랜치로 머지된 산출물 문서와 감사 로그는 남습니다.',
    )) return;
    try {
      await api.deletePlanSession(row.id);
      if (session?.id === row.id) setSession(null);
      history.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const refreshSession = async () => {
    if (!session) return;
    setSession(await api.getPlanSession(session.id));
  };

  const selectStage = (stage: PlanArtifactOut['stage']) => {
    if (!stageUnlocked(stage) || !session) return;
    setActiveStage(stage);
    setMessages([]);
    setError('');
    setInput(defaultRequestOf(session, stage));
    void loadArtifact(session.id, stage); // 확정된 산출물이 있으면 그대로 보여준다
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
      // 편집 중인 산출물을 함께 보낸다 — 새로 쓰지 않고 이것을 고치게 한다.
      const res = await api.sendPlanMessage(session.id, activeStage, content, fileList, draft);
      setMessages((prev) => [...prev, {
        role: 'assistant', content: res.summary,
        usedModules: res.used_modules, contextFiles: res.context_files,
        boundModules: res.bound_modules,
      }]);
      setDraft(res.document); // 문서 본문은 산출물 란으로
      setDraftSource('session');
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
      const confirmed = await api.confirmPlanStage(session.id, activeStage, draft);
      setGitResult(confirmed); // 커밋 후 자동 수행된 PR/머지 결과
      await refreshSession();
      // 다음 단계로 자동 이동
      const next = STAGES[stageIndex(activeStage) + 1];
      if (next) {
        setActiveStage(next.key);
        setMessages([]);
        setInput(defaultRequestOf(session, next.key));
        await loadArtifact(session.id, next.key);
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
      const [s, t] = await Promise.all([
        api.planBuildStatus(session.id),
        api.listPlanTasks(session.id),
      ]);
      setBuildEvents(s.events);
      setTasks(t);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const generateTasks = async () => {
    if (!session) return;
    setBusy(true);
    setError('');
    try {
      setTasks(await api.generatePlanTasks(session.id));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const setTaskStatus = async (task: BuildTaskOut, status: string) => {
    try {
      const updated = await api.updatePlanTask(task.id, { status });
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const runCompliance = async () => {
    if (!session) return;
    setBusy(true);
    setError('');
    try {
      setCompliance(await api.planCompliance(session.project_id));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
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
          <div className="spacer" />
          <button onClick={() => setShowCreate(true)}>+ 새 프로젝트</button>
        </div>
        <p className="mutedtext" style={{ fontSize: 12, marginTop: 6, marginBottom: 12 }}>
          코딩 전에 기획서 → 아키텍처 → 솔루션 구성 → 개발원칙을 순서대로 확정합니다. 각 단계는 앞 단계의
          확정 문서를 참조하고, 확정 산출물은 프로젝트 Gitea 리포에 커밋되며 작업 브랜치면 PR·머지까지
          자동 수행됩니다. 커밋된 문서는 외부 개발도구(VSCode·Claude·Antigravity)에서 그대로 활용합니다.
        </p>

        <form className="row" onSubmit={start}>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} required>
            <option value="">프로젝트 선택...</option>
            {availableProjects.map((p) => (
              <option key={p.id} value={p.id}>{p.org_name ? `[${p.org_name}] ${p.name}` : p.name}</option>
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

      {/* 기획 세션 이력 — 재개·삭제 */}
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0 }}>🗂️ 기획 세션 이력</h3>
          <button className="secondary small" onClick={() => history.reload()}>새로고침</button>
        </div>
        <Async state={history} empty="기획 세션이 없습니다.">
          {(rows) => {
            // 목록에 보이는 프로젝트(조직 범위)의 세션만 노출한다.
            const visible = rows.filter((r) =>
              availableProjects.some((p) => p.id === r.project_id));
            if (visible.length === 0) {
              return <p className="mutedtext" style={{ fontSize: 12 }}>기획 세션이 없습니다.</p>;
            }
            return (
              <table>
                <thead>
                  <tr>
                    <th>세션</th><th>프로젝트</th><th>브랜치</th>
                    <th>확정 단계</th><th>작업</th><th>생성일</th><th />
                  </tr>
                </thead>
                <tbody>
                  {visible.map((r) => (
                    <tr key={r.id} style={{ background: session?.id === r.id ? 'rgba(56, 189, 248, 0.08)' : undefined }}>
                      <td className="mono">#{r.id}</td>
                      <td>{r.project_name}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{r.branch}</td>
                      <td style={{ fontSize: 12 }}>
                        {r.confirmed_stages.length}/{STAGES.length}
                        {r.confirmed_stages.length > 0 && (
                          <span className="mutedtext">
                            {' '}({STAGES.filter((s) => r.confirmed_stages.includes(s.key))
                              .map((s) => s.label.replace(/^[①-④]\s*/, '')).join(', ')})
                          </span>
                        )}
                      </td>
                      <td style={{ fontSize: 12 }}>{r.task_count}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{fmtDate(r.created_at)}</td>
                      <td>
                        <div className="row" style={{ gap: 6 }}>
                          <button className="small" onClick={() => resume(r)}>재개</button>
                          <button className="small danger" onClick={() => removeSession(r)}>삭제</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          }}
        </Async>
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
                  {m.boundModules && m.boundModules.length > 0 && (
                    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
                      <span className="mutedtext" style={{ fontSize: 11 }}>이번에 바인딩된 모듈:</span>
                      {m.boundModules.map((mod) => (
                        <span key={mod} style={{ fontSize: 10, padding: '1px 6px', borderRadius: 4, background: 'rgba(16, 185, 129, 0.2)', color: '#10b981' }}>🔗 {mod}</span>
                      ))}
                    </div>
                  )}
                  {m.contextFiles && m.contextFiles.length > 0 && (
                    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
                      <span className="mutedtext" style={{ fontSize: 11 }}>내용 참조 파일:</span>
                      {m.contextFiles.map((f) => (
                        <span key={f} className="mono" style={{ fontSize: 10, padding: '1px 6px', borderRadius: 4, background: 'rgba(148, 163, 184, 0.2)' }}>{f}</span>
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
                  placeholder={`${STAGES.find((s) => s.key === activeStage)?.label} 생성·수정을 요청하세요...`}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                />
                <button type="submit" className="primary" disabled={busy || !input.trim()}>
                  {busy ? '처리 중...' : '생성 요청'}
                </button>
              </div>
            </form>

            {/* 확정용 산출물 편집기 */}
            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                📄 산출물 (마크다운) — 검토·수정 후 확정하면 Gitea에 커밋됩니다. 생성 요청 시 이 내용이 수정 대상으로 함께 전달됩니다
                {draftSource === 'repo' && (
                  <span style={{ marginLeft: 6, fontWeight: 400, color: '#f59e0b' }}>
                    · 리포에 이미 있는 문서를 불러왔습니다 (이 세션에서는 아직 미확정)
                  </span>
                )}
              </div>
              <textarea
                className="mono"
                style={{ width: '100%', minHeight: 220, fontSize: 12 }}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="생성 요청 결과가 여기에 들어옵니다. 직접 편집해도 됩니다."
              />
              <div className="row" style={{ marginTop: 8 }}>
                <button className="primary" onClick={confirm} disabled={busy || !draft.trim()}>
                  ✅ 이 단계 확정 (Gitea 커밋)
                </button>
                {gitResult?.git_action && (
                  <span className="mutedtext" style={{ fontSize: 12 }}>
                    {gitResult.title} 커밋 · {GIT_ACTION_LABEL[gitResult.git_action] ?? gitResult.git_action}
                    {gitResult.pull_request_url && (
                      <> · <a href={gitResult.pull_request_url} target="_blank" rel="noreferrer">PR 열기</a></>
                    )}
                    {gitResult.git_detail && <> · {gitResult.git_detail}</>}
                  </span>
                )}
              </div>
            </div>
          </div>

          {/* 외주 빌드 작업 지시(work order) */}
          <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>📋 외주 빌드 작업 지시</h3>
              <button className="secondary small" onClick={generateTasks} disabled={busy}>
                확정 산출물에서 작업 지시 생성
              </button>
            </div>
            <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
              외부 빌더가 MCP(<span className="mono">list_tasks·update_task·submit_build_result</span>)로
              집어가고 상태를 갱신합니다. 막히면 <span className="mono">request_clarification</span>으로
              질의가 이 기획 세션에 남습니다.
            </p>
            {tasks.length === 0 ? (
              <p className="mutedtext" style={{ fontSize: 12 }}>작업 지시가 없습니다.</p>
            ) : (
              <table>
                <thead>
                  <tr><th>작업</th><th>완료 판정</th><th>상태</th><th>커밋</th></tr>
                </thead>
                <tbody>
                  {tasks.map((t) => (
                    <tr key={t.id}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{t.title}</div>
                        {t.detail && <div className="mutedtext" style={{ fontSize: 12 }}>{t.detail}</div>}
                        {t.note && (
                          <div style={{ fontSize: 12, color: '#f59e0b' }}>💬 {t.note}</div>
                        )}
                      </td>
                      <td className="mutedtext" style={{ fontSize: 12 }}>{t.verify}</td>
                      <td>
                        <select
                          value={t.status}
                          onChange={(e) => setTaskStatus(t, e.target.value)}
                          style={{ fontSize: 12, color: TASK_STATUS.find((s) => s.key === t.status)?.color }}
                        >
                          {TASK_STATUS.map((s) => (
                            <option key={s.key} value={s.key}>{s.label}</option>
                          ))}
                        </select>
                      </td>
                      <td className="mono" style={{ fontSize: 12 }}>{t.commit_sha?.substring(0, 7) ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* 외주 결과 검증 — LLM·모듈 사용 */}
          <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>🔍 LLM·모듈 사용 검증</h3>
              <button className="secondary small" onClick={runCompliance} disabled={busy}>검사 실행</button>
            </div>
            <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
              커밋된 코드가 게이트웨이를 우회하거나 가용 목록 밖 모듈을 쓰는지 검사합니다.
              위반이 있으면 아래 프롬프트를 외주 빌더에게 그대로 전달하세요.
            </p>
            {compliance && (compliance.findings.length === 0 ? (
              <p style={{ fontSize: 13, color: '#10b981' }}>✅ 위반 없음 — 제약을 지켰습니다.</p>
            ) : (
              <>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
                  {Object.entries(compliance.summary).map(([rule, count]) => (
                    <span key={rule} style={{ fontSize: 11, padding: '2px 8px', borderRadius: 4, background: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', border: '1px solid rgba(239, 68, 68, 0.3)' }}>
                      {COMPLIANCE_RULE_LABEL[rule] ?? rule} {count}건
                    </span>
                  ))}
                </div>
                <ul style={{ margin: '0 0 12px', paddingLeft: 16, fontSize: 12 }}>
                  {compliance.findings.map((f, i) => (
                    <li key={i}>
                      <span className="mono">{f.file}:{f.line}</span> — {COMPLIANCE_RULE_LABEL[f.rule] ?? f.rule}
                      {' '}(<span className="mono">{f.detail}</span>)
                    </li>
                  ))}
                </ul>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                  📤 외주 빌더 전달용 수정 지시 프롬프트
                </div>
                <textarea
                  className="mono"
                  readOnly
                  style={{ width: '100%', minHeight: 180, fontSize: 12 }}
                  value={compliance.builder_prompt}
                />
              </>
            ))}
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

      {/* 프로젝트 페이지와 동일한 생성 UI(빈 프로젝트 옵션 포함) */}
      {showCreate && (
        <CreateModal onClose={() => setShowCreate(false)} onCreated={handleProjectCreated} />
      )}
    </>
  );
}
