import { useState } from 'react';
import Async from '../components/Async';
import CodeStructure from '../components/CodeStructure';
import DiffView from '../components/DiffView';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { extractDiffFromReply } from '../lib/diff';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { ChatSessionOut, ResourceItem, ReviewResult } from '../lib/types';

const BUILDER_SESSIONS_STORAGE_KEY = 'paas_saved_builder_sessions';

export interface SavedBuilderSession {
  id: number;
  projectId: number;
  projectName: string;
  providerId: number;
  providerName: string;
  branch: string;
  createdAt: number;
  messages: Msg[];
}

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  changeId?: number | null;
  changeStatus?: 'proposed' | 'applied' | 'rejected';
  appliedSha?: string;
  usedModules?: string[];
}

function groupResources(items: ResourceItem[]) {
  const apiByCategory: Record<string, ResourceItem[]> = {};
  const files: ResourceItem[] = [];
  const databases: ResourceItem[] = [];
  const mcpServers: ResourceItem[] = [];
  for (const r of items) {
    if (r.type === 'external_api' || r.type === 'internal_api') {
      const key = r.category || '기타';
      (apiByCategory[key] ??= []).push(r);
    } else if (r.type === 'file_storage') {
      files.push(r);
    } else if (r.type === 'database') {
      databases.push(r);
    } else if (r.type === 'mcp') {
      mcpServers.push(r);
    }
  }
  return { apiByCategory, files, databases, mcpServers };
}

export default function Chat() {
  const projects = useApi(() => api.listProjects());
  const providers = useApi(() => api.listProviders());

  const [projectId, setProjectId] = useState('');
  const resourcesState = useApi(
    () => (projectId ? api.projectResources(Number(projectId)) : Promise.resolve([])),
    [projectId],
  );
  const [showStructure, setShowStructure] = useState(false);
  const codemapState = useApi(
    () =>
      projectId && showStructure
        ? api.projectCodemap(Number(projectId)).then((r) => r.files)
        : Promise.resolve([]),
    [projectId, showStructure],
  );
  const [providerId, setProviderId] = useState('');
  const [branch, setBranch] = useState('');
  const [session, setSession] = useState<ChatSessionOut | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [files, setFiles] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [review, setReview] = useState<ReviewResult | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);

  // 저장된 빌더 세션 목록 State (기억 & 목록 선택)
  const [savedSessions, setSavedSessions] = useState<SavedBuilderSession[]>(() => {
    try {
      const saved = localStorage.getItem(BUILDER_SESSIONS_STORAGE_KEY);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [selectedSessionId, setSelectedSessionId] = useState<string>('');

  // 메시지 업데이트 시 localStorage 세션 내용 동기화
  const syncSessionMessages = (sessionId: number, updatedMessages: Msg[]) => {
    setSavedSessions((prev) => {
      const updated = prev.map((item) =>
        item.id === sessionId ? { ...item, messages: updatedMessages } : item,
      );
      try {
        localStorage.setItem(BUILDER_SESSIONS_STORAGE_KEY, JSON.stringify(updated));
      } catch {}
      return updated;
    });
  };

  const startSession = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    try {
      const s = await api.createChatSession(
        Number(projectId), Number(providerId), branch.trim() || undefined,
      );
      setSession(s);
      setMessages([]);
      setReview(null);

      const targetProject = (projects.data ?? []).find((p) => p.id === Number(projectId));
      const targetProvider = (providers.data ?? []).find((p) => p.id === Number(providerId));

      const newSavedItem: SavedBuilderSession = {
        id: s.id,
        projectId: Number(projectId),
        projectName: targetProject?.name || `Project #${projectId}`,
        providerId: Number(providerId),
        providerName: targetProvider?.name || `Provider #${providerId}`,
        branch: s.branch,
        createdAt: Date.now(),
        messages: [],
      };

      setSavedSessions((prev) => {
        const filtered = prev.filter((item) => item.id !== s.id);
        const updated = [newSavedItem, ...filtered].slice(0, 50); // 최근 50개 기억
        try {
          localStorage.setItem(BUILDER_SESSIONS_STORAGE_KEY, JSON.stringify(updated));
        } catch {}
        return updated;
      });
      setSelectedSessionId(String(s.id));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const handleSelectSavedSession = (sidStr: string) => {
    setSelectedSessionId(sidStr);
    if (!sidStr) return;

    const target = savedSessions.find((s) => s.id === Number(sidStr));
    if (target) {
      setProjectId(String(target.projectId));
      setProviderId(String(target.providerId));
      setBranch(target.branch);
      setSession({
        id: target.id,
        provider: target.providerName,
        branch: target.branch,
      });
      setMessages(target.messages || []);
      setReview(null);
    }
  };

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!session || !input.trim()) return;
    const content = input.trim();
    const newMsgList: Msg[] = [...messages, { role: 'user', content }];
    setMessages(newMsgList);
    if (session.id) {
      syncSessionMessages(session.id, newMsgList);
    }

    setInput('');
    setBusy(true);
    setError('');
    try {
      const fileList = files.split(',').map((f) => f.trim()).filter(Boolean);
      const res = await api.sendChatMessage(session.id, content, fileList);
      const finalMsgList: Msg[] = [
        ...newMsgList,
        {
          role: 'assistant',
          content: res.reply,
          changeId: res.proposed_change_id,
          changeStatus: res.proposed_change_id ? 'proposed' : undefined,
          usedModules: res.used_modules,
        },
      ];
      setMessages(finalMsgList);
      if (session.id) {
        syncSessionMessages(session.id, finalMsgList);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const decide = async (idx: number, action: 'apply' | 'reject') => {
    const msg = messages[idx];
    if (!msg.changeId) return;
    setBusy(true);
    setError('');
    try {
      let appliedSha: string | undefined;
      if (action === 'apply') {
        const res = await api.applyChange(msg.changeId);
        appliedSha = res.applied_sha;
      } else {
        await api.rejectChange(msg.changeId);
      }

      const nextMessages = messages.map((m, i) =>
        i === idx
          ? {
              ...m,
              changeStatus: action === 'apply' ? ('applied' as const) : ('rejected' as const),
              appliedSha,
            }
          : m,
      );
      setMessages(nextMessages);
      if (session?.id) {
        syncSessionMessages(session.id, nextMessages);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runReview = async () => {
    if (!projectId || !providerId || !session) return;
    setReviewBusy(true);
    setError('');
    try {
      const res = await api.review(
        Number(projectId), Number(providerId), undefined, `origin/${session.branch}`,
      );
      setReview(res);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setReviewBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="row" style={{ alignItems: 'center', gap: 10 }}>
          <h2 style={{ margin: 0 }}>🤖 에이전트 빌더 (Agent Builder)</h2>
          <span style={{ fontSize: 12, padding: '2px 8px', borderRadius: 4, background: 'rgba(16, 185, 129, 0.15)', color: '#10b981', border: '1px solid rgba(16, 185, 129, 0.3)' }}>
            체크된 모듈 프롬프트 자동 반영
          </span>
        </div>
        <p className="mutedtext" style={{ fontSize: 12, marginTop: 6, marginBottom: 12 }}>
          프로젝트에 연동/체크된 모듈 및 환경변수 명세가 LLM 시스템 프롬프트에 자동 반영되어 최적화된 에이전트 연동 코드가 작성됩니다.
        </p>

        {/* 저장된 이전 빌더 세션 선택 드롭다운 */}
        {savedSessions.length > 0 && (
          <div style={{ marginBottom: 12, padding: '8px 12px', background: 'rgba(255, 255, 255, 0.03)', borderRadius: 6, border: '1px solid rgba(255, 255, 255, 0.08)' }}>
            <div className="row" style={{ alignItems: 'center', gap: 8 }}>
              <label style={{ fontSize: 12, fontWeight: 600, color: '#38bdf8' }}>
                📂 기억된 빌더 세션 목록 ({savedSessions.length}개):
              </label>
              <select
                style={{ flex: 1, maxWidth: 500, fontSize: 12 }}
                value={selectedSessionId}
                onChange={(e) => handleSelectSavedSession(e.target.value)}
              >
                <option value="">이전 세션 선택 복원...</option>
                {savedSessions.map((s) => (
                  <option key={s.id} value={s.id}>
                    세션 #{s.id} · {s.projectName} ({s.branch}) - {fmtDate(new Date(s.createdAt).toISOString())} ({s.messages.length}개 대화)
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}

        <form className="row" onSubmit={startSession}>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} required>
            <option value="">프로젝트 선택...</option>
            {(projects.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
          <select value={providerId} onChange={(e) => setProviderId(e.target.value)} required>
            <option value="">LLM 프로바이더 선택...</option>
            {(providers.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.kind})
              </option>
            ))}
          </select>
          <input
            className="mono"
            placeholder="작업 브랜치 (선택)"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            style={{ width: 200 }}
          />
          <button type="submit">신규 빌더 세션 시작</button>
          {session && (
            <span className="mutedtext" style={{ fontSize: 12 }}>
              현재 세션 #{session.id} · 브랜치 <span className="mono">{session.branch}</span> ·{' '}
              {session.provider}
            </span>
          )}
        </form>
      </div>

      {projectId && (
        <div className="panel">
          <h2 style={{ margin: '0 0 10px' }}>📦 프롬프트 반영 체크 모듈 & 자원 현황</h2>
          <Async state={resourcesState} empty="등록된 자원이 없습니다.">
            {(items) => {
              const { apiByCategory, files, databases, mcpServers } = groupResources(items);
              return (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {Object.keys(apiByCategory).length > 0 && (
                    <div>
                      <div className="mutedtext" style={{ fontSize: 11, marginBottom: 6 }}>
                        API (카테고리별)
                      </div>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        {Object.entries(apiByCategory).map(([cat, list]) => (
                          <div
                            key={cat}
                            style={{
                              border: '1px solid var(--border)',
                              borderRadius: 6,
                              padding: '8px 12px',
                              background: 'var(--panel-bg)',
                            }}
                          >
                            <strong style={{ fontSize: 12 }}>{cat}</strong>
                            <ul style={{ margin: '4px 0 0', paddingLeft: 16, fontSize: 12 }}>
                              {list.map((r) => (
                                <li key={r.name}>
                                  {r.name}{' '}
                                  <span className="mutedtext">
                                    ({r.env_prefix}_URL, {r.env_prefix}_API_KEY)
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {files.length > 0 && (
                    <div>
                      <div className="mutedtext" style={{ fontSize: 11, marginBottom: 6 }}>
                        공유 파일 저장소
                      </div>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        {files.map((r) => (
                          <div
                            key={r.name}
                            style={{
                              border: '1px solid var(--border)',
                              borderRadius: 6,
                              padding: '8px 12px',
                              background: 'var(--panel-bg)',
                              fontSize: 12,
                            }}
                          >
                            📁 <strong>{r.name}</strong>{' '}
                            <span className="mutedtext">({r.env_prefix}_ENDPOINT)</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {databases.length > 0 && (
                    <div>
                      <div className="mutedtext" style={{ fontSize: 11, marginBottom: 6 }}>
                        데이터베이스
                      </div>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        {databases.map((r) => (
                          <div
                            key={r.name}
                            style={{
                              border: '1px solid var(--border)',
                              borderRadius: 6,
                              padding: '8px 12px',
                              background: 'var(--panel-bg)',
                              fontSize: 12,
                            }}
                          >
                            🗄️ <strong>{r.name}</strong>{' '}
                            <span className="mutedtext">({r.env_prefix}_DSN)</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {mcpServers.length > 0 && (
                    <div>
                      <div className="mutedtext" style={{ fontSize: 11, marginBottom: 6 }}>
                        MCP 서버 (도구)
                      </div>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        {mcpServers.map((r) => (
                          <div
                            key={r.name}
                            style={{
                              border: '1px solid var(--border)',
                              borderRadius: 6,
                              padding: '8px 12px',
                              background: 'var(--panel-bg)',
                              fontSize: 12,
                            }}
                          >
                            🔌 <strong>{r.name}</strong>{' '}
                            <span className="mutedtext">({r.env_prefix}_URL)</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              );
            }}
          </Async>
        </div>
      )}

      {session && (
        <>
          <div className="panel" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button
              onClick={() => setShowStructure((v) => !v)}
              className="secondary small"
            >
              {showStructure ? '📁 파일 구조 숨기기' : '📁 파일 구조 보기'}
            </button>
            <button onClick={runReview} disabled={reviewBusy} className="secondary small">
              {reviewBusy ? '리뷰 수행 중...' : '🔍 코드 리뷰 수행'}
            </button>
          </div>

          {showStructure && (
            <div className="panel">
              <h3>프로젝트 파일 레프트 트리</h3>
              <Async state={codemapState} empty="파일 구조가 비어있습니다.">
                {(fileList) => <CodeStructure files={fileList} />}
              </Async>
            </div>
          )}

          {review && (
            <div className="panel">
              <h3>🔍 코드 리뷰 결과</h3>
              <p>
                <strong>최대 심각도:</strong> <StatusPill value={review.max_severity} />
              </p>

              {review.findings && review.findings.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
                  {review.findings.map((iss, i) => (
                    <div
                      key={i}
                      style={{
                        padding: 10,
                        borderRadius: 6,
                        background: 'var(--panel-bg)',
                        borderLeft: `4px solid ${
                          iss.severity === 'high'
                            ? '#ef4444'
                            : iss.severity === 'medium'
                            ? '#f59e0b'
                            : '#3b82f6'
                        }`,
                      }}
                    >
                      <div className="row" style={{ justifyContent: 'space-between' }}>
                        <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>
                          {iss.file}
                        </span>
                        <StatusPill value={iss.severity} />
                      </div>
                      <p style={{ margin: '4px 0 0', fontSize: 13 }}>{iss.comment}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mutedtext">감지된 특이 이슈가 없습니다. 아주 좋습니다!</p>
              )}
            </div>
          )}

          <div className="panel">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {messages.map((m, i) => {
                const diff = m.content ? extractDiffFromReply(m.content) : null;
                return (
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
                      {m.role === 'user' ? '👤 나 (User)' : '🤖 LLM 에이전트'}
                    </div>

                    {m.usedModules && m.usedModules.length > 0 && (
                      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
                        <span className="mutedtext" style={{ fontSize: 11 }}>
                          참조된 모듈:
                        </span>
                        {m.usedModules.map((mod) => (
                          <span
                            key={mod}
                            style={{
                              fontSize: 10,
                              padding: '1px 6px',
                              borderRadius: 4,
                              background: 'rgba(56, 189, 248, 0.2)',
                              color: '#38bdf8',
                            }}
                          >
                            {mod}
                          </span>
                        ))}
                      </div>
                    )}

                    <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{m.content}</div>

                    {diff && (
                      <div style={{ marginTop: 12 }}>
                        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                          📝 제안된 코드 변경사항 (Diff):
                        </div>
                        <DiffView diff={diff} />
                      </div>
                    )}

                    {m.changeId && (
                      <div
                        style={{
                          marginTop: 10,
                          paddingTop: 8,
                          borderTop: '1px dashed var(--border)',
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                        }}
                      >
                        <span className="mutedtext" style={{ fontSize: 12 }}>
                          변경 # {m.changeId}
                        </span>
                        {m.changeStatus === 'proposed' && (
                          <>
                            <button
                              className="primary small"
                              disabled={busy}
                              onClick={() => decide(i, 'apply')}
                            >
                              ✅ 승인 (Git Apply)
                            </button>
                            <button
                              className="danger small"
                              disabled={busy}
                              onClick={() => decide(i, 'reject')}
                            >
                              ❌ 거절
                            </button>
                          </>
                        )}
                        {m.changeStatus === 'applied' && (
                          <span style={{ color: '#10b981', fontSize: 12 }}>
                            ✓ 적용됨 ({m.appliedSha?.substring(0, 7)})
                          </span>
                        )}
                        {m.changeStatus === 'rejected' && (
                          <span style={{ color: '#ef4444', fontSize: 12 }}>
                            ✗ 거절됨
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <form onSubmit={send} style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <input
                className="mono"
                placeholder="참조할 특정 파일 경로들 (쉼표 구분 예: app/main.py, config.py)..."
                value={files}
                onChange={(e) => setFiles(e.target.value)}
              />
              <div className="row">
                <textarea
                  style={{ flex: 1, minHeight: 80, fontFamily: 'inherit' }}
                  placeholder="LLM에 명령/기능 구현 요청 입력..."
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                />
                <button type="submit" className="primary" disabled={busy || !input.trim()}>
                  {busy ? '요청 처리 중...' : '전송'}
                </button>
              </div>
            </form>
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
