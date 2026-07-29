import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { fmtDate } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import type { ProjectContext } from '../ProjectDetail';

export default function ModuleReportTab() {
  const { project } = useOutletContext<ProjectContext>();
  const reportState = useApi(() => api.getProjectModuleReport(project.id), [project.id]);

  return (
    <div className="panel" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div>
        <h2 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
          📦 프로젝트 모듈 사용이력 리포트
        </h2>
        <p className="mutedtext" style={{ fontSize: 12, marginTop: 4 }}>
          <strong>{project.name}</strong> 프로젝트에 바인딩된 모듈 구성과 주입된 환경변수, 그리고 모듈 바인딩 변경 감사 이력을 종합적으로 리포팅합니다.
        </p>
      </div>

      <Async state={reportState}>
        {(report) => (
          <>
            {/* 요약 통계 카드 */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12 }}>
              <div style={{ padding: 14, borderRadius: 8, background: 'rgba(56, 189, 248, 0.08)', border: '1px solid rgba(56, 189, 248, 0.2)' }}>
                <div style={{ fontSize: 12, color: '#888' }}>현재 바인딩 모듈</div>
                <div style={{ fontSize: 24, fontWeight: 700, color: '#38bdf8', marginTop: 4 }}>
                  {report.total_active_modules}개
                </div>
              </div>
              <div style={{ padding: 14, borderRadius: 8, background: 'rgba(16, 185, 129, 0.08)', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
                <div style={{ fontSize: 12, color: '#888' }}>자동 주입 환경변수</div>
                <div style={{ fontSize: 24, fontWeight: 700, color: '#10b981', marginTop: 4 }}>
                  {report.total_injected_envs}개
                </div>
              </div>
              <div style={{ padding: 14, borderRadius: 8, background: 'rgba(245, 158, 11, 0.08)', border: '1px solid rgba(245, 158, 11, 0.2)' }}>
                <div style={{ fontSize: 12, color: '#888' }}>모듈 변경 기록</div>
                <div style={{ fontSize: 24, fontWeight: 700, color: '#f59e0b', marginTop: 4 }}>
                  {report.history.length}건
                </div>
              </div>
            </div>

            {/* 1. 현재 바인딩된 모듈 목록 */}
            <div style={{ marginTop: 8 }}>
              <h3 style={{ margin: '0 0 8px 0', fontSize: 15, display: 'flex', alignItems: 'center', gap: 6 }}>
                🧩 활성 바인딩 모듈 ({report.active_modules.length})
              </h3>
              {report.active_modules.length === 0 ? (
                <div className="mutedtext" style={{ padding: 20, textAlign: 'center', background: 'rgba(255, 255, 255, 0.02)', borderRadius: 6 }}>
                  현재 이 프로젝트에 바인딩된 모듈이 없습니다.
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>모듈명</th>
                      <th>타입</th>
                      <th>카테고리</th>
                      <th>ENV 접두사</th>
                      <th>자동 주입 환경변수 키</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.active_modules.map((m) => (
                      <tr key={m.id}>
                        <td style={{ fontWeight: 600 }}>{m.name}</td>
                        <td><StatusPill value={m.type} /></td>
                        <td>{m.category || '기타'}</td>
                        <td className="mono">{m.env_prefix}</td>
                        <td>
                          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                            {m.injected_env_keys.map((k) => (
                              <span
                                key={k}
                                className="mono"
                                style={{
                                  fontSize: 11,
                                  padding: '1px 6px',
                                  borderRadius: 4,
                                  background: 'rgba(16, 185, 129, 0.15)',
                                  color: '#10b981',
                                  border: '1px solid rgba(16, 185, 129, 0.3)',
                                }}
                              >
                                {k}
                              </span>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* 2. 모듈 바인딩 변경 및 작업 감사 이력 */}
            <div style={{ marginTop: 12 }}>
              <h3 style={{ margin: '0 0 8px 0', fontSize: 15, display: 'flex', alignItems: 'center', gap: 6 }}>
                📜 모듈 작업 및 변경 이력 ({report.history.length})
              </h3>
              {report.history.length === 0 ? (
                <div className="mutedtext" style={{ padding: 20, textAlign: 'center', background: 'rgba(255, 255, 255, 0.02)', borderRadius: 6 }}>
                  기록된 모듈 변경 이력이 없습니다.
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>일시</th>
                      <th>작업자 (Actor)</th>
                      <th>Action</th>
                      <th>대상</th>
                      <th>상세 Payload</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.history.map((h) => (
                      <tr key={h.id}>
                        <td className="mono">{fmtDate(h.created_at)}</td>
                        <td className="mono">{h.actor}</td>
                        <td><StatusPill value={h.action} /></td>
                        <td className="mono">{h.target}</td>
                        <td className="mono" style={{ fontSize: 11, color: '#aaa' }}>
                          {JSON.stringify(h.payload)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </Async>
    </div>
  );
}
