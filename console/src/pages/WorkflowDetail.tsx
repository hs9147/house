import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Async from '../components/Async';
import WorkflowGraph from '../components/WorkflowGraph';
import { api } from '../lib/api';
import { useApi, usePolling } from '../lib/hooks';
import type {
  AgentVerdict, WorkflowAssessment, WorkflowExtracted, WorkflowMessageOut, WorkflowOut,
  WorkflowProposal, WorkflowRunOut, WorkflowSpec,
} from '../lib/types';
import { VIZ } from '../lib/viz';

/**
 * 워크플로 상세 — 왼쪽은 대화, 오른쪽은 그림. 그리고 **읽어 낸 업무**와 실행.
 *
 * 흐름은 산업·연구가 같이 쓰는 모양이다: LLM이 구조화 스펙을 만들고 → 검증기가 받고 →
 * 사람이 보고 고치고 → 사람이 저장한다. 제안은 저장되지 않는다(캔버스에만 올라간다) —
 * 이 스펙은 실행되면 파일을 쓰고 모듈을 부르므로, 사람의 확인 없이 운영에 들어가지 않는다.
 *
 * '대화에서 읽어 낸 것'(개체·상태·전이·제약)을 함께 보여 주는 이유: 스펙만 보면 그럴듯한데
 * 내 업무가 아닌 워크플로를 알아볼 수 없다. 모델이 무엇을 업무로 이해했는지 따로 적게 하고,
 * 그것과 스펙이 맞물리는지는 기계가 볼 수 있는 만큼 검토 메모로 내놓는다.
 */
export default function WorkflowDetail() {
  const { id } = useParams();
  const workflowId = Number(id);
  const navigate = useNavigate();
  const state = useApi(() => api.getWorkflow(workflowId), [workflowId]);
  const messages = useApi(() => api.workflowMessages(workflowId), [workflowId]);
  const runs = useApi(() => api.listWorkflowRuns(workflowId), [workflowId]);

  const [request, setRequest] = useState('');
  const [proposal, setProposal] = useState<WorkflowProposal | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState('');
  const [openRun, setOpenRun] = useState<number | null>(null);
  const [assessment, setAssessment] = useState<WorkflowAssessment | null>(null);
  // 이름 수정 중일 때만 값이 있다(빈 문자열은 '수정 중이지만 비움'과 구분이 안 되므로 null).
  const [newName, setNewName] = useState<string | null>(null);

  const ask = async () => {
    if (!request.trim()) return;
    setBusy('chat');
    setError('');
    setNotice('');
    try {
      const result = await api.workflowChat(workflowId, request.trim());
      setProposal(result);
      setRequest('');
      messages.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const save = async (spec: WorkflowSpec, extracted?: WorkflowExtracted) => {
    setBusy('save');
    setError('');
    try {
      await api.saveWorkflow(workflowId, spec, extracted);
      setProposal(null);
      setNotice('저장했습니다 — 이제 이 판으로 실행됩니다.');
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const rename = async () => {
    if (newName === null || !newName.trim()) return;
    setBusy('rename');
    setError('');
    try {
      await api.renameWorkflow(workflowId, newName.trim());
      setNewName(null);
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const assess = async () => {
    setBusy('assess');
    setError('');
    try {
      setAssessment(await api.assessWorkflow(workflowId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const run = async () => {
    setBusy('run');
    setError('');
    try {
      const started = await api.startWorkflowRun(workflowId);
      setOpenRun(started.id);
      runs.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <Async state={state}>
      {(workflow: WorkflowOut) => {
        const shown = proposal?.spec ?? workflow.spec;
        const isProposal = proposal !== null;
        return (
          <>
            <div className="row" style={{ marginBottom: 12, alignItems: 'center' }}>
              <button className="small secondary" onClick={() => navigate('/workflows')}>
                ← 목록
              </button>
              {newName === null ? (
                <>
                  <h2 style={{ margin: 0 }}>{workflow.name}</h2>
                  <button className="small secondary" title="이름 수정"
                          onClick={() => setNewName(workflow.name)}>
                    이름 수정
                  </button>
                </>
              ) : (
                <>
                  <input
                    value={newName}
                    autoFocus
                    style={{ fontSize: 18, minWidth: 260 }}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') rename();
                      if (e.key === 'Escape') setNewName(null);
                    }}
                  />
                  <button className="small" disabled={!newName.trim() || busy !== ''}
                          onClick={rename}>
                    {busy === 'rename' ? '저장 중…' : '저장'}
                  </button>
                  <button className="small secondary" onClick={() => setNewName(null)}>
                    취소
                  </button>
                </>
              )}
              <span className="mutedtext mono">
                {workflow.org_name} · v{workflow.version} · 단계 {workflow.summary.node_count}
                {workflow.summary.human_steps > 0
                  && ` · 사람 단계 ${workflow.summary.human_steps}`}
              </span>
            </div>

            {(workflow.problems?.length ?? 0) > 0 && (
              <div className="panel" style={{ borderColor: VIZ.status.critical }}>
                <b>저장된 스펙에 문제가 있습니다</b>
                <p className="mutedtext" style={{ fontSize: 12, margin: '4px 0' }}>
                  저장할 때는 통과했지만 지금은 아닙니다 — 저장소·모듈·프로바이더가 사라졌거나
                  이름이 바뀌었습니다. 이 상태로는 실행이 거부됩니다.
                </p>
                <ul style={{ fontSize: 12, margin: 0 }}>
                  {workflow.problems!.map((p) => <li key={p}>{p}</li>)}
                </ul>
              </div>
            )}

            <div className="row" style={{ gap: 12, alignItems: 'stretch', flexWrap: 'wrap' }}>
              <div className="panel" style={{ flex: '1 1 380px', minWidth: 340 }}>
                <h3 style={{ marginTop: 0 }}>구성 대화</h3>
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  하고 싶은 일을 업무 말로 적으세요. 등록된 공통 제약사항과 이 조직이 쓸 수 있는
                  자원이 함께 전달되고, 만들어진 스펙은 검증을 거칩니다.
                </p>
                <Async state={messages}>
                  {(rows: WorkflowMessageOut[]) => (
                    <div style={{
                      maxHeight: 260, overflowY: 'auto', display: 'flex',
                      flexDirection: 'column', gap: 6, marginBottom: 8,
                    }}>
                      {rows.length === 0 && (
                        <span className="mutedtext" style={{ fontSize: 12 }}>
                          아직 대화가 없습니다.
                        </span>
                      )}
                      {rows.map((m, i) => (
                        <div key={i} style={{
                          fontSize: 12, padding: '6px 8px', borderRadius: 6,
                          background: m.role === 'user' ? 'var(--panel-2, #20212f)' : 'transparent',
                          border: m.role === 'assistant'
                            ? '1px solid var(--border-soft)' : 'none',
                          whiteSpace: 'pre-wrap',
                        }}>
                          <div className="mutedtext" style={{ fontSize: 10, marginBottom: 2 }}>
                            {m.role === 'user' ? '요청' : 'LLM'}
                          </div>
                          {m.content}
                        </div>
                      ))}
                    </div>
                  )}
                </Async>
                <textarea
                  rows={5}
                  placeholder={'예: 계약 검토 건을 모아 금액이 1억을 넘으면 법무 승인을 받고, '
                    + '결과를 파일로 남겨 주세요.'}
                  value={request}
                  style={{ width: '100%' }}
                  onChange={(e) => setRequest(e.target.value)}
                />
                <div className="row" style={{ gap: 8, marginTop: 6 }}>
                  <button className="small" disabled={!request.trim() || busy !== ''} onClick={ask}>
                    {busy === 'chat' ? 'LLM 작성 중…' : '보내기'}
                  </button>
                  {isProposal && (
                    <button className="small secondary" onClick={() => setProposal(null)}>
                      제안 버리기
                    </button>
                  )}
                </div>
                {error && <p className="error">{error}</p>}
                {notice && <p style={{ fontSize: 12, color: 'var(--green)' }}>{notice}</p>}
              </div>

              <div className="panel" style={{ flex: '2 1 520px', minWidth: 420 }}>
                <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                  <h3 style={{ margin: 0 }}>
                    {isProposal ? '제안 (저장 전)' : '저장된 흐름'}
                  </h3>
                  <div className="row" style={{ gap: 8 }}>
                    {isProposal && (
                      <button className="small"
                              disabled={proposal!.problems.length > 0 || busy !== ''}
                              onClick={() => save(proposal!.spec, proposal!.extracted)}>
                        {busy === 'save' ? '저장 중…' : '이 제안 저장'}
                      </button>
                    )}
                    {!isProposal && workflow.summary.node_count > 0 && (
                      <button className="small" disabled={busy !== ''} onClick={run}>
                        {busy === 'run' ? '시작 중…' : '실행'}
                      </button>
                    )}
                  </div>
                </div>
                {isProposal && (
                  <p className="mutedtext" style={{ fontSize: 12 }}>
                    {proposal!.summary}
                    <br />
                    {`${proposal!.provider}`}
                    {proposal!.attempts > 1
                      && ' · 검증에서 한 번 거부돼 LLM이 고친 결과입니다'}
                  </p>
                )}
                {isProposal && proposal!.problems.length > 0 && (
                  <div style={{ fontSize: 12, color: VIZ.status.critical }}>
                    <b>검증을 통과하지 못해 저장할 수 없습니다</b>
                    <ul style={{ margin: '4px 0' }}>
                      {proposal!.problems.map((p) => <li key={p}>{p}</li>)}
                    </ul>
                  </div>
                )}
                <WorkflowGraph spec={shown} selected={selected} onSelect={setSelected} />
                {selected && (
                  <NodeDetail spec={shown} id={selected} />
                )}
              </div>
            </div>

            <ExtractedPanel
              extracted={proposal?.extracted ?? workflow.extracted}
              review={proposal?.review ?? []}
              isProposal={isProposal}
            />

            <Async state={runs}>
              {(rows: WorkflowRunOut[]) => (
                <div className="panel">
                  <h3 style={{ marginTop: 0 }}>실행</h3>
                  {rows.length === 0 ? (
                    <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                      아직 실행한 적이 없습니다.
                    </p>
                  ) : (
                    <table>
                      <thead>
                        <tr><th>#</th><th>상태</th><th>판</th><th>실행자</th><th>시작</th><th></th></tr>
                      </thead>
                      <tbody>
                        {rows.map((r) => (
                          <tr key={r.id}>
                            <td className="mono">{r.id}</td>
                            <td>{RUN_LABEL[r.status] ?? r.status}</td>
                            <td className="mono">v{r.version}</td>
                            <td>{r.actor}</td>
                            <td className="mutedtext" style={{ fontSize: 11 }}>
                              {new Date(r.created_at).toLocaleString('ko-KR')}
                            </td>
                            <td>
                              <button className="small secondary"
                                      onClick={() => setOpenRun(r.id)}>
                                진행 보기
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </Async>

            {openRun !== null && (
              <RunPanel runId={openRun} spec={workflow.spec}
                        onClose={() => { setOpenRun(null); runs.reload(); }} />
            )}

            <AssessmentPanel
              assessment={assessment}
              busy={busy === 'assess'}
              disabled={workflow.summary.node_count === 0 || busy !== ''}
              onAssess={assess}
              onApply={(text) => {
                setRequest(text);
                window.scrollTo({ top: 0, behavior: 'smooth' });
              }}
            />
          </>
        );
      }}
    </Async>
  );
}

const RUN_LABEL: Record<string, string> = {
  running: '도는 중', waiting: '사람 단계 대기', succeeded: '성공',
  failed: '실패', canceled: '취소',
};

const STEP_LABEL: Record<string, string> = {
  ok: '완료', skipped: '건너뜀', failed: '실패', waiting: '대기', rejected: '반려',
};

const STEP_COLOR: Record<string, string> = {
  ok: VIZ.status.good, skipped: VIZ.muted, failed: VIZ.status.critical,
  waiting: VIZ.status.warning, rejected: VIZ.status.critical,
};

/** 고른 노드가 실제로 무엇을 하는지 — 스펙의 그 줄을 그대로 보여 준다(그림은 요약이다). */
function NodeDetail({ spec, id }: { spec: WorkflowSpec; id: string }) {
  const node = (spec.nodes ?? []).find((n) => String(n.id) === id);
  if (!node) return null;
  return (
    <pre className="mono" style={{
      fontSize: 11, marginTop: 8, padding: 8, overflowX: 'auto',
      border: '1px solid var(--border-soft)', borderRadius: 6,
    }}>
      {JSON.stringify(node, null, 2)}
    </pre>
  );
}

/**
 * 대화에서 읽어 낸 업무 — 개체·상태·전이·제약. **검토용 화면**이다.
 *
 * 여기서 사람이 보는 것은 "LLM이 내 업무를 맞게 이해했나"다. 틀렸으면 스펙을 고치는 것이
 * 아니라 대화를 고쳐야 한다 — 그래서 스펙 옆이 아니라 아래에 따로 둔다.
 */
function ExtractedPanel({ extracted, review, isProposal }: {
  extracted: WorkflowExtracted;
  review: string[];
  isProposal: boolean;
}) {
  const entities = extracted.entities ?? [];
  const states = extracted.states ?? [];
  const transitions = extracted.transitions ?? [];
  const constraints = extracted.constraints ?? [];
  const empty = entities.length + states.length + transitions.length + constraints.length === 0;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>
        대화에서 읽어 낸 업무{isProposal ? ' (제안)' : ''}
      </h3>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        LLM이 무엇을 <b>업무로 이해했는지</b>입니다 — 개체(다루는 대상), 상태, 전이(무엇이
        일어나면 상태가 바뀌는가), 제약(지켜야 하는 규칙). 스펙만 보면 그럴듯한데 내 업무가
        아닌 워크플로를 알아볼 수 없어서 따로 내놓습니다. 틀렸으면 스펙을 고치지 말고
        대화로 바로잡으세요.
      </p>
      {empty ? (
        <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
          아직 읽어 낸 것이 없습니다.
        </p>
      ) : (
        <div className="row" style={{ gap: 18, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <div style={{ flex: '1 1 220px' }}>
            <h4 style={{ margin: '0 0 4px' }}>개체 {entities.length}</h4>
            <ul style={{ fontSize: 12, margin: 0, paddingLeft: 18 }}>
              {entities.map((e) => (
                <li key={e.name}>
                  {e.name}
                  {e.note && <span className="mutedtext"> — {e.note}</span>}
                </li>
              ))}
            </ul>
          </div>
          <div style={{ flex: '1 1 220px' }}>
            <h4 style={{ margin: '0 0 4px' }}>상태 {states.length}</h4>
            <ul style={{ fontSize: 12, margin: 0, paddingLeft: 18 }}>
              {states.map((s, i) => (
                <li key={`${s.entity}-${s.name}-${i}`}>
                  <span className="mutedtext">{s.entity}: </span>{s.name}
                </li>
              ))}
            </ul>
          </div>
          <div style={{ flex: '1 1 280px' }}>
            <h4 style={{ margin: '0 0 4px' }}>전이 {transitions.length}</h4>
            <ul style={{ fontSize: 12, margin: 0, paddingLeft: 18 }}>
              {transitions.map((t, i) => (
                <li key={i}>
                  {t.from} → {t.to}
                  {t.trigger && <span className="mutedtext"> ({t.trigger})</span>}
                  {t.node && <span className="mono"> · {t.node}</span>}
                </li>
              ))}
            </ul>
          </div>
          <div style={{ flex: '1 1 320px' }}>
            <h4 style={{ margin: '0 0 4px' }}>제약 {constraints.length}</h4>
            <ul style={{ fontSize: 12, margin: 0, paddingLeft: 18 }}>
              {constraints.map((c, i) => (
                <li key={i}>
                  {c.text}
                  {c.origin && <span className="mutedtext"> [{c.origin}]</span>}
                  {c.node
                    ? <span className="mono"> · {c.node}</span>
                    : <span style={{ color: VIZ.status.warning }}> · 지키는 단계 없음</span>}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {review.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <b style={{ color: VIZ.status.warning }}>검토할 자리</b>
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {review.map((note) => <li key={note}>{note}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

/** 진행 — 단계별 상태를 그림과 표에 함께 보이고, 사람 단계면 제출 창구를 준다. */
function RunPanel({ runId, spec, onClose }: {
  runId: number; spec: WorkflowSpec; onClose: () => void;
}) {
  const [run, setRun] = useState<WorkflowRunOut | null>(null);
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const seen = useRef(0);

  const load = async () => {
    try {
      setRun(await api.getWorkflowRun(runId));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => { void load(); }, [runId]);
  // 도는 동안만 폴링한다 — 끝난 실행을 계속 긁을 이유가 없다.
  usePolling(load, 2000, run?.status === 'running');

  const submit = async (approved: boolean) => {
    setBusy(true);
    setError('');
    try {
      await api.submitWorkflowHumanStep(runId, content, approved);
      setContent('');
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!run) return null;
  const status: Record<string, string> = {};
  run.steps.forEach((s) => { status[s.id] = s.status; });
  seen.current = run.steps.length;
  const pending = run.pending_node
    ? (spec.nodes ?? []).find((n) => String(n.id) === run.pending_node)
    : undefined;

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: 0 }}>
          실행 #{run.id} — {RUN_LABEL[run.status] ?? run.status}
        </h3>
        <button className="small secondary" onClick={onClose}>닫기</button>
      </div>
      {run.error && <p className="error" style={{ fontSize: 12 }}>{run.error}</p>}
      <WorkflowGraph spec={spec} status={status} height={320} />
      <table style={{ marginTop: 8 }}>
        <thead><tr><th>단계</th><th>종류</th><th>상태</th><th>요약</th><th>ms</th></tr></thead>
        <tbody>
          {run.steps.map((s, i) => (
            <tr key={`${s.id}-${i}`}>
              <td className="mono">{s.id}</td>
              <td className="mutedtext" style={{ fontSize: 11 }}>{s.type}</td>
              <td style={{ color: STEP_COLOR[s.status] }}>
                {STEP_LABEL[s.status] ?? s.status}
              </td>
              <td style={{ fontSize: 12, maxWidth: 420, overflowWrap: 'anywhere' }}>
                {s.summary}
              </td>
              <td className="mono">{s.ms}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {run.status === 'waiting' && pending && (
        <div style={{ marginTop: 10 }}>
          <h4 style={{ margin: '0 0 4px' }}>
            사람 작업: {String(pending.title ?? run.pending_node)}
            {pending.role ? ` (${String(pending.role)})` : ''}
          </h4>
          {Boolean(pending.instruction) && (
            <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
              {String(pending.instruction)}
            </p>
          )}
          {/* 판단 근거 — 앞 단계가 만든 자료를 여기서 바로 읽을 수 있어야 결정할 수 있다. */}
          <details>
            <summary style={{ fontSize: 12, cursor: 'pointer' }}>앞 단계 결과 보기</summary>
            <pre className="mono" style={{
              fontSize: 11, maxHeight: 260, overflow: 'auto', padding: 8,
              border: '1px solid var(--border-soft)', borderRadius: 6,
            }}>
              {Object.entries(run.outputs ?? {})
                .map(([key, value]) => `## ${key}\n${value.text || `(파일 ${value.paths.length}건)`}`)
                .join('\n\n')}
            </pre>
          </details>
          <textarea rows={3} style={{ width: '100%', marginTop: 6 }}
                    placeholder="의견·결과를 적습니다(다음 단계의 입력이 됩니다)"
                    value={content} onChange={(e) => setContent(e.target.value)} />
          <div className="row" style={{ gap: 8, marginTop: 6 }}>
            <button className="small" disabled={busy} onClick={() => submit(true)}>
              승인하고 계속
            </button>
            <button className="small secondary" disabled={busy} onClick={() => submit(false)}>
              반려(여기서 종료)
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

const VERDICT: Record<AgentVerdict, { label: string; color: string; mark: string }> = {
  agent: { label: '에이전트 전환 가능', color: VIZ.status.good, mark: '●' },
  partial: { label: '부분 전환(결정은 사람)', color: VIZ.status.warning, mark: '◐' },
  human: { label: '사람이 해야 함', color: VIZ.muted, mark: '○' },
};

/**
 * 워크플로 평가 — 화면 **맨 아래**. 사람이 하는 일을 에이전트로 옮길 수 있는지, 옮기려면
 * 무엇을 바꿔야 하는지.
 *
 * 구성 대화와 질문이 다르다: 거기서는 "이 업무를 흐름으로 적어 달라"였고, 여기서는 이미
 * 적힌 흐름을 보고 "어디가 사람 손을 떠날 수 있나"를 묻는다. 실측에서 바로 필요해졌다 —
 * GP구매 메모로 만든 워크플로는 29단계 중 24개가 사람 단계였다.
 *
 * 평가로 끝내지 않는다. **이 제안으로 다시 구성** 버튼이 결과를 그대로 구성 대화의 요청으로
 * 올린다 — 읽고 끝나는 평가는 아무것도 바꾸지 않는다. 비율과 개수는 서버가 다시 센 값이고
 * (모델의 숫자를 믿지 않는다), 모델이 빠뜨린 사람 단계는 검토 메모로 드러난다.
 */
function AssessmentPanel({ assessment, busy, disabled, onAssess, onApply }: {
  assessment: WorkflowAssessment | null;
  busy: boolean;
  disabled: boolean;
  onAssess: () => void;
  onApply: (text: string) => void;
}) {
  const m = assessment?.metrics;
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: 0 }}>워크플로 평가</h3>
        <button className="small" disabled={disabled} onClick={onAssess}>
          {busy ? '평가 중…' : assessment ? '다시 평가' : '평가하기'}
        </button>
      </div>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        사람이 하는 단계를 하나씩 보고 <b>에이전트로 옮길 수 있는지</b>, 옮기려면 워크플로를
        어떻게 바꿔야 하는지 판정합니다. 결재·승인처럼 권한과 책임이 걸린 자리는 사람으로
        남기고, 자료 수집과 초안 작성을 앞 단계로 떼어 내는 쪽이 현실적인 답입니다.
      </p>

      {!assessment && !busy && (
        <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
          아직 평가하지 않았습니다. 평가는 저장되지 않습니다 — 스펙이 바뀌면 평가도 옛것이
          됩니다.
        </p>
      )}

      {assessment && m && (
        <>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', margin: '6px 0 10px' }}>
            <StatBox label="전체 단계" value={String(m.total)} />
            <StatBox label="사람 단계" value={String(m.human)} />
            <StatBox label="전환 가능" value={String(m.agent)}
                     color={VIZ.status.good} sub="바로 옮길 수 있음" />
            <StatBox label="부분 전환" value={String(m.partial)}
                     color={VIZ.status.warning} sub="결정은 사람이 남음" />
            <StatBox label="사람 유지" value={String(m.human_only)} sub="권한·책임" />
            <StatBox label="손을 떠날 수 있는 비율" value={`${m.shift_rate}%`}
                     sub={`지금 당장 가능 ${m.ready_now}건`} />
          </div>
          {assessment.summary && (
            <p style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{assessment.summary}</p>
          )}

          <table>
            <thead>
              <tr>
                <th>단계</th><th>판정</th><th>바꿀 종류</th>
                <th>무엇을 바꾸는가</th><th>있어야 하는 것</th>
              </tr>
            </thead>
            <tbody>
              {assessment.steps.map((s) => {
                const look = VERDICT[s.verdict];
                return (
                  <tr key={s.id}>
                    <td className="mono" style={{ whiteSpace: 'nowrap' }}>{s.id}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <span aria-hidden style={{ color: look.color, marginRight: 4 }}>
                        {look.mark}
                      </span>
                      {look.label}
                      {s.ready_now && (
                        <div style={{ fontSize: 10, color: VIZ.status.good }}>지금 가능</div>
                      )}
                    </td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {s.becomes.join(' + ') || '—'}
                    </td>
                    <td style={{ fontSize: 12, maxWidth: 420, overflowWrap: 'anywhere' }}>
                      {s.change || s.why}
                    </td>
                    <td style={{ fontSize: 11, maxWidth: 240, overflowWrap: 'anywhere' }}>
                      {s.needs.length === 0
                        ? <span className="mutedtext">지금 다 있음</span>
                        : s.needs.join(' · ')}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {assessment.change_request && (
            <div className="row" style={{ gap: 8, marginTop: 10, alignItems: 'center' }}>
              <button className="small" onClick={() => onApply(assessment.change_request)}>
                이 제안으로 다시 구성
              </button>
              <span className="mutedtext" style={{ fontSize: 11 }}>
                구성 대화의 요청란에 올려 놓습니다 — 보내기 전에 고칠 수 있습니다.
              </span>
            </div>
          )}

          {assessment.missing.length > 0 && (
            <div style={{ marginTop: 10, fontSize: 12 }}>
              <b>더 자동화하려면 플랫폼에 있어야 하는 것</b>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {assessment.missing.map((x) => <li key={x}>{x}</li>)}
              </ul>
            </div>
          )}
          {assessment.risks.length > 0 && (
            <div style={{ marginTop: 10, fontSize: 12 }}>
              <b style={{ color: VIZ.status.warning }}>자동화하면 생기는 위험</b>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {assessment.risks.map((x) => <li key={x}>{x}</li>)}
              </ul>
            </div>
          )}
          {assessment.notes.length > 0 && (
            <div style={{ marginTop: 10, fontSize: 12 }}>
              <b style={{ color: VIZ.status.warning }}>검토할 자리</b>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {assessment.notes.map((x) => <li key={x}>{x}</li>)}
              </ul>
            </div>
          )}
          <p className="mutedtext" style={{ fontSize: 11, marginTop: 8 }}>
            {assessment.provider} · 평가는 저장되지 않습니다
          </p>
        </>
      )}
    </div>
  );
}

function StatBox({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <div style={{
      border: '1px solid var(--border-soft)', borderRadius: 8, padding: '8px 12px',
      minWidth: 118,
    }}>
      <div style={{ fontSize: 11, color: VIZ.muted }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: color ?? VIZ.ink }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: VIZ.muted }}>{sub}</div>}
    </div>
  );
}
