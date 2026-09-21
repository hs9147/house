import { useEffect, useState } from 'react';
import Async from '../components/Async';
import C4Diagram from '../components/C4Diagram';
import Modal from '../components/Modal';
import VscodeWorkButton from '../components/VscodeWorkButton';
import ZipDownloadButton from '../components/ZipDownloadButton';
import { ApiError, api } from '../lib/api';
import { getEmail } from '../lib/auth';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type {
  BuildTaskOut, BuildTaskSync, ComplianceOut, PlanArtifactContent, PlanArtifactOut,
  PlanBuildEvent, PlanMergeOut, PlanSessionOut, PlanSessionSummary, ProjectOut,
} from '../lib/types';
import { CreateModal } from './Projects';

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  usedModules?: string[];
  contextFiles?: string[];
  boundModules?: string[];
  compacted?: boolean;
}

// 진행 중인 서버 작업. 팝업이 화면을 덮어 처리 중에는 다른 조작을 받지 않는다 —
// 생성 중에 단계를 옮기거나 생성을 다시 요청하면 요청이 엉키고, 먼저 온 응답이 나중
// 것을 덮어써 결과가 조용히 버려진다.
interface RunningTask {
  label: string;
  detail: string;
  controller: AbortController;
}

// 확정 시 git 상태에 따라 자동 수행된 결과의 표시 문구
const GIT_ACTION_LABEL: Record<string, string> = {
  committed: '기본 브랜치에 직접 커밋',
  merged: 'PR 생성 후 자동 머지 완료',
  pr_opened: 'PR 생성됨 (자동 머지 불가 — 확인 필요)',
  skipped: 'PR 미수행',
};

// 작업 지시 상태 — 외부 빌더가 MCP로 갱신하고, 콘솔은 표시만 한다(읽기 전용).
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

// 에이전트 기획 5단계(진행단계 표시) — 순차 진행, 앞 단계 확정을 전제로 한다.
// ⑤ 작업 지시도 다른 단계와 같은 확정 단계다. 다만 본문을 대화로 쓰지 않고 작업 지시
// 목록에서 렌더한다 — 문서와 MCP(list_tasks)가 어긋나지 않게 원천을 하나로 둔다.
const TASK_STAGE = 'tasks';
const STAGES: { key: PlanArtifactOut['stage']; label: string }[] = [
  { key: 'spec', label: '① 기획서' },
  { key: 'architecture', label: '② 아키텍처 설계' },
  { key: 'solution', label: '③ 솔루션 구성' },
  { key: 'principles', label: '④ 개발원칙' },
  { key: TASK_STAGE, label: '⑤ 작업 지시' },
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
  const [draft, setDraft] = useState('');
  const [task, setTask] = useState<RunningTask | null>(null);
  const busy = task !== null;
  const [error, setError] = useState('');
  // 취소처럼 실패가 아닌 알림 — 빨간 경고와 섞으면 사고인지 아닌지 구분되지 않는다.
  const [notice, setNotice] = useState('');
  const [buildEvents, setBuildEvents] = useState<PlanBuildEvent[]>([]);
  const [gitResult, setGitResult] = useState<PlanArtifactOut | null>(null);
  const [tasks, setTasks] = useState<BuildTaskOut[]>([]);
  const [taskSync, setTaskSync] = useState<BuildTaskSync | null>(null);
  const [compliance, setCompliance] = useState<ComplianceOut | null>(null);
  const [mergeResult, setMergeResult] = useState<PlanMergeOut | null>(null);
  const [draftSource, setDraftSource] = useState<PlanArtifactContent['source']>('');
  const history = useApi(() => api.listPlanSessions());
  // 공통 제약사항은 관리자만 본다 — 일반 사용자가 손댈 수 없는 환경 제약이고, 어차피
  // 서버가 각 단계 컨텍스트에 실어 주므로(services/planning.build_constraints) 화면에
  // 두 번 보여 줄 이유가 없다. 관리자가 아니면 조회 자체를 하지 않는다.
  const commonConstraints = useApi(
    () => (me.data?.is_admin ? api.listCommonConstraints() : Promise.resolve([])),
    [me.data?.is_admin],
  );
  const [constraintText, setConstraintText] = useState('');

  // 프로젝트 페이지와 동일한 CreateModal(빈 프로젝트 옵션 포함)을 재사용한다.
  const [showCreate, setShowCreate] = useState(false);

  const handleProjectCreated = (created?: ProjectOut) => {
    setShowCreate(false);
    projects.reload();
    if (created) setProjectId(String(created.id)); // 생성 즉시 선택
  };

  // 대화에 찍히는 사용자 식별자 — 계정 로그인은 이메일, API 키는 키 이름이 곧 id다.
  const userLabel = me.data?.name || getEmail() || '사용자';

  const artifactOf = (stage: string) => session?.artifacts.find((a) => a.stage === stage);
  const isConfirmed = (stage: string) => !!artifactOf(stage)?.confirmed;
  // 서버가 내려준 단계별 기본 생성 요청 — 입력창 기본값이라 바로 '생성 요청'을 누를 수 있다.
  const defaultRequestOf = (s: PlanSessionOut | null, stage: string) =>
    s?.artifacts.find((a) => a.stage === stage)?.default_request ?? '';
  const stageIndex = (stage: string) => STAGES.findIndex((s) => s.key === stage);
  // 앞 단계가 모두 확정돼야 진입 가능(진행단계 순차 강제).
  const stageUnlocked = (stage: string) =>
    STAGES.slice(0, stageIndex(stage)).every((s) => isConfirmed(s.key));

  // 세션이 마무리(브랜치 머지)됐는지 — **서버가 기억한다**(ChatSession.merged_at).
  // 방금 머지한 결과(mergeResult)만 보면 세션을 다시 열었을 때 이미 머지한 세션에 머지
  // 버튼이 또 보인다. 작업 지시를 재생성하면 서버가 merged_at을 지우므로 다시 머지 단계로
  // 돌아간다.
  const merged = session?.merged_at != null;

  // ⑤ 단계에서 지금 강조할 버튼 — 작업 지시 생성 → 단계 확정 → 브랜치 머지 → 진행 현황 순.
  const taskStep: 'generate' | 'confirm' | 'merge' | 'progress' =
    tasks.length === 0 ? 'generate'
      : !isConfirmed(TASK_STAGE) ? 'confirm'
        : merged ? 'progress' : 'merge';

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
      setMergeResult(null); // 이전 세션의 머지 결과 문구가 남지 않게
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
      setMergeResult(null); // 이전 세션의 머지 결과 문구가 남지 않게
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

  const addConstraint = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = constraintText.trim();
    if (!text) return;
    setError('');
    try {
      await api.addCommonConstraint(text);
      setConstraintText('');
      commonConstraints.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const removeConstraint = async (id: number) => {
    setError('');
    try {
      await api.deleteCommonConstraint(id);
      commonConstraints.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const refreshSession = async () => {
    if (!session) return;
    setSession(await api.getPlanSession(session.id));
  };

  // 진행 중 작업을 등록하고 취소용 신호를 돌려준다 — 팝업은 이 상태를 보고 뜬다.
  const begin = (label: string, detail: string): AbortSignal => {
    const controller = new AbortController();
    setTask({ label, detail, controller });
    setError('');
    setNotice('');
    return controller.signal;
  };

  const isCancel = (err: unknown) => (err as Error).name === 'AbortError';

  // 취소는 **이 화면의 기다림만** 끊는다. 서버에서 시작된 일(LLM 호출·Gitea 커밋)은
  // 계속될 수 있으므로, 화면이 사실과 어긋나지 않게 세션 상태를 다시 읽는다.
  const afterCancel = async () => {
    setNotice('취소했습니다 — 서버에서 이미 시작된 작업은 계속될 수 있습니다. '
      + '화면을 서버 상태로 다시 맞췄습니다.');
    try {
      await refreshSession();
      if (session) await loadArtifact(session.id, activeStage);
    } catch {
      /* 재동기화 실패는 취소 자체를 되돌리지 않는다 */
    }
  };

  const selectStage = (stage: PlanArtifactOut['stage']) => {
    if (!stageUnlocked(stage) || !session) return;
    setActiveStage(stage);
    setMessages([]);
    setError('');
    // ⑤ 산출물은 작업 지시 목록에서 렌더되므로 목록도 함께 불러온다.
    if (stage === TASK_STAGE) {
      void api.listPlanTasks(session.id).then(setTasks).catch(() => setTasks([]));
    }
    setInput(defaultRequestOf(session, stage));
    void loadArtifact(session.id, stage); // 확정된 산출물이 있으면 그대로 보여준다
  };

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!session || !input.trim()) return;
    const content = input.trim();
    setMessages((prev) => [...prev, { role: 'user', content }]);
    setInput('');
    const stageLabel = STAGES.find((s) => s.key === activeStage)?.label ?? activeStage;
    const signal = begin(
      `${stageLabel} 생성 중`,
      'LLM이 앞 단계 확정 문서와 가용 모듈 제약을 읽고 산출물을 작성하고 있습니다. '
      + '문서 길이에 따라 수십 초가 걸릴 수 있습니다.',
    );
    try {
      // 편집 중인 산출물을 함께 보낸다 — 새로 쓰지 않고 이것을 고치게 한다.
      let res;
      try {
        res = await api.sendPlanMessage(session.id, activeStage, content, draft, false, signal);
      } catch (err) {
        // 413 = 컨텍스트가 모델 한도를 넘었다 — 압축해서 다시 시도할지 묻는다.
        if ((err as ApiError).status !== 413) throw err;
        if (!window.confirm(
          `${(err as Error).message}\n\n` +
          '압축하면 앞 단계 문서는 제목만 싣고 코드 구조·참조 파일 본문은 빠집니다.\n' +
          '컨텍스트를 압축해 다시 실행할까요?',
        )) {
          setMessages((prev) => prev.slice(0, -1)); // 보낸 요청을 되돌린다
          setInput(content);
          setTask(null);
          return;
        }
        res = await api.sendPlanMessage(session.id, activeStage, content, draft, true, signal);
      }
      setMessages((prev) => [...prev, {
        role: 'assistant', content: res.summary,
        usedModules: res.used_modules, contextFiles: res.context_files,
        boundModules: res.bound_modules, compacted: res.compacted,
      }]);
      setDraft(res.document); // 문서 본문은 산출물 란으로
      setDraftSource('session');
    } catch (err) {
      if (isCancel(err)) {
        setMessages((prev) => prev.slice(0, -1)); // 보낸 요청을 되돌린다
        setInput(content);
        await afterCancel();
      } else {
        setError((err as Error).message);
      }
    } finally {
      setTask(null);
    }
  };

  const confirm = async () => {
    if (!session || !draft.trim()) return;
    const stageLabel = STAGES.find((s) => s.key === activeStage)?.label ?? activeStage;
    const signal = begin(
      `${stageLabel} 확정 중`,
      '산출물을 Gitea 리포에 커밋하고, 작업 브랜치면 PR 생성·자동 머지까지 시도합니다.',
    );
    try {
      let confirmed;
      try {
        confirmed = await api.confirmPlanStage(session.id, activeStage, draft, false, signal);
      } catch (err) {
        // 412 = 리포에 이미 다른 내용의 같은 문서가 있다 — 덮어쓸지 확인하고 재시도한다.
        if ((err as ApiError).status !== 412) throw err;
        if (!window.confirm(`${(err as Error).message}\n\n덮어쓰고 확정할까요?`)) {
          setTask(null);
          return;
        }
        confirmed = await api.confirmPlanStage(session.id, activeStage, draft, true, signal);
      }
      setGitResult(confirmed); // 커밋 후 자동 수행된 PR/머지 결과
      await refreshSession();
      // 다음 단계로 자동 이동. 마지막 단계면 그 자리에 남아 머지로 이어진다.
      const next = STAGES[stageIndex(activeStage) + 1];
      setMessages([]);
      if (next) {
        setActiveStage(next.key);
        setInput(defaultRequestOf(session, next.key));
        await loadArtifact(session.id, next.key);
        if (next.key === TASK_STAGE) setTasks(await api.listPlanTasks(session.id));
      } else {
        setDraftSource('session');
      }
    } catch (err) {
      if (isCancel(err)) await afterCancel();
      else setError((err as Error).message);
    } finally {
      setTask(null);
    }
  };

  // 진행 현황은 기본 브랜치(main) 기준으로 갱신한다 — 빌더의 보고가 아니라 거기에
  // 반영된 커밋이 완료의 근거다.
  const loadBuildStatus = async () => {
    if (!session) return;
    const signal = begin(
      '진행 현황 확인 중',
      '기본 브랜치를 최신화하고, 보고된 커밋이 거기에 반영됐는지 판정합니다.',
    );
    try {
      const [s, sync] = await Promise.all([
        api.planBuildStatus(session.id, signal),
        api.syncPlanTasks(session.id, signal),
      ]);
      setBuildEvents(s.events);
      setTasks(sync.tasks);
      setTaskSync(sync);
    } catch (err) {
      if (isCancel(err)) await afterCancel();
      else setError((err as Error).message);
    } finally {
      setTask(null);
    }
  };

  const generateTasks = async () => {
    if (!session) return;
    const signal = begin(
      '작업 지시 생성 중',
      'LLM이 확정 산출물을 외주 빌드 단위로 나누고 있습니다.',
    );
    try {
      setTasks(await api.generatePlanTasks(session.id, signal));
      // 산출물 문서는 이 목록을 렌더한 것이다 — 편집기도 새 목록으로 맞춘다.
      await loadArtifact(session.id, TASK_STAGE);
      // 재생성으로 세션 마무리가 무효가 됐다(서버가 merged_at을 지운다) — 버튼 상태를
      // 다시 읽어 '진행 현황 업데이트'가 '브랜치 머지'로 돌아가게 한다.
      setMergeResult(null);
      await refreshSession();
    } catch (err) {
      if (isCancel(err)) await afterCancel();
      else setError((err as Error).message);
    } finally {
      setTask(null);
    }
  };

  const mergeSession = async () => {
    if (!session) return;
    const signal = begin(
      '브랜치 머지 중',
      '작업 브랜치를 기본 브랜치로 반영합니다(PR 생성·머지).',
    );
    try {
      setMergeResult(await api.mergePlanSession(session.id, signal));
      await refreshSession();
      history.reload();
    } catch (err) {
      if (isCancel(err)) await afterCancel();
      else setError((err as Error).message);
    } finally {
      setTask(null);
    }
  };

  const runCompliance = async () => {
    if (!session) return;
    const signal = begin(
      'LLM·모듈 사용 검증 중',
      '커밋된 코드가 게이트웨이를 우회하거나 가용 목록 밖 모듈을 쓰는지 훑습니다.',
    );
    try {
      setCompliance(await api.planCompliance(session.project_id, signal));
    } catch (err) {
      if (isCancel(err)) await afterCancel();
      else setError((err as Error).message);
    } finally {
      setTask(null);
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
          코딩 전에 기획서 → 아키텍처 → 솔루션 구성 → 개발원칙 → 작업 지시 5단계를 순서대로 확정하고,
          마지막에 브랜치를 머지해 세션을 마무리합니다. ⑤ 작업 지시는 대화 대신 앞 단계 확정 산출물에서
          외주 빌드 단위를 뽑아 문서로 확정합니다. 각 단계는 앞 단계의 확정 문서를 참조하며, 확정 산출물은
          프로젝트 Gitea 리포에 커밋되어 외부 개발도구(VSCode·Claude·Antigravity)에서 그대로 활용합니다.
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

      {/* 공통 제약사항 — 프로젝트와 무관하게 늘 지켜야 하는 환경 제약.
          **관리자 전용 화면이다.** 일반 사용자가 바꿀 수 없는 값이고, 서버가 각 단계
          컨텍스트에 알아서 실어 주므로(services/planning.build_constraints) 화면에 또
          보여 줄 이유가 없다. */}
      {me.data?.is_admin && (
        <div className="panel">
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ margin: 0 }}>📌 공통 제약사항 (모든 프로젝트 적용)</h3>
            <button className="secondary small" onClick={() => commonConstraints.reload()}>새로고침</button>
          </div>
          <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
            여기에 등록한 제약은 기획 ①~⑤ 각 단계의 제약 문서에 함께 실려 매 단계에서 고려되고,
            작업 지시 생성과 외주 빌더(MCP <span className="mono">get_constraints</span>)·
            위반 검사 수정 지시에도 그대로 전달됩니다.
          </p>
          <form onSubmit={addConstraint} className="row" style={{ alignItems: 'flex-start', marginBottom: 8 }}>
            <textarea
              style={{ flex: 1, minHeight: 60, fontFamily: 'inherit' }}
              placeholder="예: 기업 내부 에이전트이므로 외부 솔루션은 사용하지 않는다(Redis 등 불필요)."
              value={constraintText}
              onChange={(e) => setConstraintText(e.target.value)}
            />
            <button type="submit" disabled={!constraintText.trim()}>+ 추가</button>
          </form>
          <Async state={commonConstraints} empty="등록된 공통 제약사항이 없습니다.">
            {(rows) => (
              <ul style={{ margin: 0, paddingLeft: 16, fontSize: 13 }}>
                {rows.map((r) => (
                  <li key={r.id} style={{ marginBottom: 6 }}>
                    <span style={{ whiteSpace: 'pre-wrap' }}>{r.text}</span>
                    <button
                      className="small danger"
                      style={{ marginLeft: 8 }}
                      onClick={() => removeConstraint(r.id)}
                    >
                      삭제
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </div>
      )}

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
                              .map((s) => s.label.replace(/^[①-⑤]\s*/, '')).join(', ')})
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

          {/* 단계별 시각화 — 산출물에 실린 mermaid C4 블록이 그림의 원천이다.
              어느 레벨이 열리는지는 문서가 정한다(app/services/c4.py): 기획서의
              C4Context → 사용자·외부 환경, 아키텍처 설계의 C4Container·C4Component,
              솔루션 구성이 같은 레벨을 다시 그리면 그것으로 구체화된다.
              **확정을 기다리지 않는다** — 편집 중인 초안을 함께 넘겨 바로 그린다.
              그림은 확정 여부를 검토하기 위한 도구이고, 확정은 그 검토의 결과다. */}
          <div className="panel">
            <h3 style={{ margin: 0 }}>🗺️ 단계별 시각화 (C4 모델)</h3>
            <C4Diagram
              // 세션을 바꾸면 보고 있던 레벨·선택 컴포넌트를 초기화한다 — 이전 세션의
              // 컴포넌트 코드가 새 세션 화면에 남으면 안 된다.
              key={session.id}
              sessionId={session.id}
              projectId={session.project_id}
              reloadKey={session.artifacts
                .filter((a) => a.confirmed).map((a) => a.commit_sha ?? '').join(',')}
              stage={activeStage}
              draft={draft}
              stageLabels={Object.fromEntries(STAGES.map((s) => [s.key, s.label]))}
            />
          </div>

          {/* 단계 작업 영역 — ①~④는 대화로, ⑤는 작업 지시 목록으로 산출물을 만든다 */}
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

            {activeStage !== TASK_STAGE ? (
              <>
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
                        {m.role === 'user' ? `👤 ${userLabel}` : '🧭 기획 에이전트'}
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
                          <span className="mutedtext" style={{ fontSize: 11 }}>자동 참조된 파일:</span>
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
              </>
            ) : (
              <>
                {/* ⑤ 작업 지시 — 산출물 본문은 이 목록을 렌더한 것이다(대화 없음).
                    지금 할 일 하나만 강조한다: 생성 → 단계 확정 → 브랜치 머지 → 진행 현황.
                    지난 단계 버튼은 남겨 두되(재생성·재머지가 필요할 수 있다) 강조는 뺀다. */}
                <div className="row" style={{ gap: 8, marginTop: 12 }}>
                  <button
                    className={taskStep === 'generate' ? 'primary small' : 'secondary small'}
                    onClick={generateTasks}
                    disabled={busy}
                  >
                    {tasks.length === 0 ? '확정 산출물에서 작업 지시 생성' : '작업 지시 재생성'}
                  </button>
                  {/* 작업 지시까지 나오면 다음은 실제 구현이다 — 이 세션의 프로젝트
                      리포를 바로 VS Code로 받게 한다. 세션에는 project_id만 있으므로
                      목록에서 프로젝트를 찾아 넘긴다. */}
                  <VscodeWorkButton
                    project={availableProjects.find((p) => p.id === session?.project_id)}
                  />
                  <ZipDownloadButton
                    project={availableProjects.find((p) => p.id === session?.project_id)}
                  />
                  {/* 머지 전에는 '브랜치 머지', 머지 후에는 '진행 현황 업데이트' —
                      둘을 함께 두지 않는다. 마무리한 세션에 머지 버튼이 남아 있으면 다시
                      눌러야 하는 것처럼 보인다. 작업 지시를 재생성하면 서버가 마무리를
                      무효로 만들어(merged_at=None) 다시 머지 버튼으로 돌아온다. */}
                  {isConfirmed(TASK_STAGE) && !merged && (
                    <button
                      className={taskStep === 'merge' ? 'primary small' : 'secondary small'}
                      onClick={mergeSession}
                      disabled={busy}
                    >
                      🔀 브랜치 머지 (세션 마무리)
                    </button>
                  )}
                  {merged && (
                    <button
                      className={taskStep === 'progress' ? 'primary small' : 'secondary small'}
                      onClick={loadBuildStatus}
                      disabled={busy}
                    >
                      🔄 진행 현황 업데이트
                    </button>
                  )}
                </div>
                {mergeResult && (
                  <p style={{ fontSize: 12, marginTop: 6, color: mergeResult.action === 'merged' ? '#10b981' : '#f59e0b' }}>
                    {mergeResult.branch} → {GIT_ACTION_LABEL[mergeResult.action] ?? mergeResult.action}
                    {mergeResult.pull_request_url && (
                      <> · <a href={mergeResult.pull_request_url} target="_blank" rel="noreferrer">PR 열기</a></>
                    )}
                    {mergeResult.detail && <> · {mergeResult.detail}</>}
                  </p>
                )}
                <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
                  외부 빌더가 MCP(<span className="mono">list_tasks·update_task·submit_build_result</span>)로
                  집어가고 상태를 갱신합니다. 막히면 <span className="mono">request_clarification</span>으로
                  질의가 이 기획 세션에 남습니다. 진행 현황은 빌더의 보고가 아니라
                  <b> 기본 브랜치에 반영된 커밋</b>을 기준으로 갱신됩니다 — 빌더가 작업 브랜치를 push하면
                  기본 브랜치로 가는 PR이 자동 생성되고, 그 PR이 머지되면 완료로 바뀝니다.
                  {' '}<b>상태는 여기서 직접 바꾸지 않습니다</b> — 손으로 고쳐도 다음 갱신이
                  같은 기준으로 다시 판정해 되돌립니다.
                </p>
                {taskSync && (
                  <p style={{ fontSize: 12, marginTop: 6 }}>
                    {taskSync.base_ref ? (
                      <>
                        <span className="mono">{taskSync.base_ref}</span> 기준 ·
                        <span style={{ color: '#10b981' }}> 반영 {taskSync.merged}건</span> ·
                        <span style={{ color: '#f59e0b' }}> 머지 대기 {taskSync.pending}건</span>
                        {/* 근거를 못 찾은 작업을 드러낸다 — 이게 없으면 "반영 0건 · 대기
                            0건"이 찍혀서, 판정할 것이 없었던 것과 반영이 없는 것이
                            구분되지 않는다(동작하는 것처럼 보이면서 아무 일도 안 한다). */}
                        {taskSync.unmatched > 0 && (
                          <>
                            {' · '}
                            <span style={{ color: '#94a3b8' }}>
                              근거 없음 {taskSync.unmatched}건
                            </span>
                            <span className="mutedtext">
                              {' '}— 커밋 메시지에 <span className="mono">task #번호</span>를
                              넣으면 그 커밋이 기본 브랜치에 반영될 때 자동으로 완료가 됩니다
                            </span>
                          </>
                        )}
                      </>
                    ) : (
                      <span className="mutedtext">
                        기본 브랜치를 읽지 못해 진행 현황을 판정하지 못했습니다 (워킹카피 없음).
                      </span>
                    )}
                  </p>
                )}
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
                          {/* 손으로 고쳐 두면 다음 '진행 현황 업데이트'가 되돌린다 — 표시만 한다 */}
                          <td style={{ fontSize: 12, color: TASK_STATUS.find((s) => s.key === t.status)?.color }}>
                            {TASK_STATUS.find((s) => s.key === t.status)?.label ?? t.status}
                          </td>
                          <td className="mono" style={{ fontSize: 12 }}>{t.commit_sha?.substring(0, 7) ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}

            {/* 확정용 산출물 편집기 — 모든 단계 공통. 여기서 확정하면 Gitea에 커밋된다. */}
            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                📄 산출물 (마크다운) — 검토·수정 후 확정하면 Gitea에 커밋됩니다.
                {activeStage === TASK_STAGE
                  ? ' 위 작업 지시 목록에서 생성된 문서입니다'
                  : ' 생성 요청 시 이 내용이 수정 대상으로 함께 전달됩니다'}
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
                placeholder={activeStage === TASK_STAGE
                  ? '작업 지시를 생성하면 여기에 문서가 만들어집니다. 직접 편집해도 됩니다.'
                  : '생성 요청 결과가 여기에 들어옵니다. 직접 편집해도 됩니다.'}
              />
              <div className="row" style={{ marginTop: 8 }}>
                <button
                  className={activeStage === TASK_STAGE && taskStep !== 'confirm' ? 'secondary' : 'primary'}
                  onClick={confirm}
                  disabled={busy || !draft.trim()}
                >
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

          {/* 외부 빌드 모니터링 — 진행 현황과 결과 검증을 한 화면에 둔다.
              둘은 같은 질문의 두 면이다: 외주 빌더가 무엇을 했고(이벤트·커밋), 그것이
              제약을 지켰는지(LLM·모듈 사용). 패널을 갈라 두면 커밋이 올라온 것을 보고도
              검증은 다른 자리에서 따로 눌러야 해서 그냥 넘어가기 쉽다. */}
          <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>🛠️ 외부 빌드 모니터링</h3>
              <div className="row" style={{ gap: 6 }}>
                <button className="secondary small" onClick={runCompliance} disabled={busy}>
                  🔍 LLM·모듈 사용 검증
                </button>
                <button className="secondary small" onClick={loadBuildStatus} disabled={busy}>
                  새로고침
                </button>
              </div>
            </div>
            <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
              빌드는 외부 개발도구에서 수행됩니다. 커밋(Gitea 웹훅)과 모듈 사용·진행 보고(MCP)가
              아래에 집계되고, 커밋된 코드가 게이트웨이를 우회하거나 가용 목록 밖 모듈을 쓰는지도
              여기서 검증합니다.
            </p>

            <div style={{ fontSize: 12, fontWeight: 600, margin: '12px 0 4px' }}>수집된 이벤트</div>
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

            <div style={{ fontSize: 12, fontWeight: 600, margin: '16px 0 4px' }}>
              LLM·모듈 사용 검증
              {!compliance && (
                <span className="mutedtext" style={{ fontWeight: 400 }}>
                  {' '}— 아직 검사하지 않았습니다 (위 버튼으로 실행)
                </span>
              )}
            </div>
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
        </>
      )}

      {error && (
        <div style={{ padding: 12, borderRadius: 6, background: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', border: '1px solid rgba(239, 68, 68, 0.3)', marginTop: 16 }}>
          ⚠️ {error}
        </div>
      )}
      {notice && (
        <div style={{ padding: 12, borderRadius: 6, background: 'rgba(56, 189, 248, 0.12)', color: '#38bdf8', border: '1px solid rgba(56, 189, 248, 0.3)', marginTop: 16 }}>
          ℹ️ {notice}
        </div>
      )}

      {/* 처리 중에는 이 팝업이 화면을 덮는다 — 진행 상황을 보여 주는 동시에 다른 조작을
          막는 것이 목적이다. 생성 중에 단계를 옮기거나 다시 생성을 요청하면 요청이
          엉키고, 먼저 온 응답이 나중 것을 덮어써 결과가 조용히 버려진다. */}
      {task && (
        <ProgressModal task={task} onCancel={() => task.controller.abort()} />
      )}

      {/* 프로젝트 페이지와 동일한 생성 UI(빈 프로젝트 옵션 포함) */}
      {showCreate && (
        <CreateModal onClose={() => setShowCreate(false)} onCreated={handleProjectCreated} />
      )}
    </>
  );
}

// 진행 중 팝업. 닫기 버튼이 없다(closable=false) — 창만 닫고 다른 조작을 하게 두면
// 막는 의미가 없다. 나가는 길은 '취소' 하나다.
//
// 퍼센트를 보여 주지 않는다. LLM 호출은 서버가 진행률을 알려 주지 않으므로 진행률을
// 그리면 그건 지어낸 숫자다 — 대신 무엇을 하는 중인지와 경과 시간을 보여 준다.
function ProgressModal({ task, onCancel }: { task: RunningTask; onCancel: () => void }) {
  const [seconds, setSeconds] = useState(0);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <Modal title={task.label} onClose={onCancel} closable={false}>
      <p style={{ marginTop: 0, fontSize: 13 }}>{task.detail}</p>
      <div className="progress-indeterminate"><div /></div>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        {seconds}초 경과 · 처리가 끝날 때까지 다른 조작을 받지 않습니다.
      </p>
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
        <button
          className="secondary"
          disabled={cancelling}
          onClick={() => { setCancelling(true); onCancel(); }}
        >
          {cancelling ? '취소 중...' : '취소'}
        </button>
      </div>
      {cancelling && (
        <p className="mutedtext" style={{ fontSize: 11, marginBottom: 0 }}>
          취소는 이 화면의 기다림을 끊습니다. 서버에서 이미 시작된 작업(LLM 호출·Gitea
          커밋)은 끝까지 진행될 수 있어, 취소 후 화면을 서버 상태로 다시 맞춥니다.
        </p>
      )}
    </Modal>
  );
}
