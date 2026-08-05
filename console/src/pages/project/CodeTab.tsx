import { useEffect, useState, useMemo } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ResourceItem } from '../../lib/types';
import type { ProjectContext } from '../ProjectDetail';

export default function CodeTab() {
  const { project } = useOutletContext<ProjectContext>();
  const filesState = useApi(() => api.projectFiles(project.id), [project.id]);
  const modulesState = useApi(() => api.listModules(), []);
  const resourcesState = useApi(() => api.projectResources(project.id), [project.id]);
  
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [allContentMap, setAllContentMap] = useState<Record<string, string>>({});
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState('');

  // 첫 번째 파일 자동 선택
  useEffect(() => {
    if (filesState.data?.files && filesState.data.files.length > 0 && !selected) {
      setSelected(filesState.data.files[0]);
    }
  }, [filesState.data, selected]);

  // 파일 내용 로드 및 메모리 수집
  useEffect(() => {
    if (!selected) return;
    setLoadingContent(true);
    setError('');
    api
      .projectFileContent(project.id, selected)
      .then((res) => {
        setContent(res.content);
        setAllContentMap((prev) => ({ ...prev, [selected]: res.content }));
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoadingContent(false));
  }, [project.id, selected]);

  // 코드 전체 텍스트 수집 (실시간 감지용)
  const fullCodeText = useMemo(() => {
    return Object.values(allContentMap).join('\n') + '\n' + content;
  }, [allContentMap, content]);

  // 프로젝트에서 사용 가능한 통합 자원 목록 (전체 모듈 + projectResources 흡수)
  const mergedResources = useMemo(() => {
    const list: Array<{ id: string; name: string; type: string; category?: string; env_prefix?: string; bound: boolean }> = [];
    const seen = new Set<string>();

    // 1. projectResources 흡수
    if (resourcesState.data && Array.isArray(resourcesState.data)) {
      for (const r of resourcesState.data as ResourceItem[]) {
        seen.add(r.name);
        list.push({
          id: `res-${r.name}`,
          name: r.name,
          type: r.type,
          category: r.category || undefined,
          env_prefix: r.env_prefix || undefined,
          bound: Boolean(r.bound),
        });
      }
    }

    // 2. listModules 중 누락된 모듈 흡수
    if (modulesState.data && Array.isArray(modulesState.data)) {
      for (const m of modulesState.data) {
        if (!seen.has(m.name)) {
          seen.add(m.name);
          list.push({
            id: `mod-${m.id}`,
            name: m.name,
            type: m.type,
            category: m.category || undefined,
            bound: false,
          });
        }
      }
    }

    return list;
  }, [resourcesState.data, modulesState.data]);

  // 모듈/자원 사용 여부 실시간 판단 로직
  const checkItemUsage = (item: { name: string; type: string; env_prefix?: string }) => {
    if (!fullCodeText) return { used: false, matches: [] as string[] };
    
    const matches: string[] = [];
    const textUpper = fullCodeText.toUpperCase();
    const nameUpper = item.name.toUpperCase().replace(/-/g, '_');

    // 1. env_prefix 가 정의되어 있을 경우 코드 내 언급 확인 (예: PAY_URL, DB_DSN)
    if (item.env_prefix) {
      const pUpper = item.env_prefix.toUpperCase();
      if (textUpper.includes(pUpper)) {
        matches.push(`${pUpper}_*`);
      }
    }

    // 2. 모듈/자원 이름 또는 변환된 이름 언급 확인
    if (textUpper.includes(nameUpper) || fullCodeText.includes(item.name)) {
      matches.push(item.name);
    }

    // 3. 자원 이름 기반 환경변수 관례 확인
    const prefixCandidate = nameUpper.split('_')[0];
    if (prefixCandidate.length >= 2) {
      const envPattern = new RegExp(`${prefixCandidate}_[A-Z0-9_]+`, 'g');
      const foundEnvs = fullCodeText.match(envPattern);
      if (foundEnvs && foundEnvs.length > 0) {
        matches.push(...Array.from(new Set(foundEnvs)));
      }
    }

    return {
      used: matches.length > 0,
      matches: Array.from(new Set(matches)),
    };
  };

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>코드 구조 & 모듈 연동 현황</h2>
          <span className="mutedtext" style={{ fontSize: 12 }}>
            코드 변경에 따라 우측바의 모듈 사용 여부가 실시간 업데이트됩니다. (코드 구현은 외부
            개발도구에서 수행하고, 기획·작업 지시는 <Link to="/planning">에이전트 기획</Link>에서 합니다)
          </span>
        </div>
      </div>

      <Async state={filesState} empty="리포에 파일이 없습니다.">
        {(data) => (
          <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr 280px', gap: 14, minHeight: 480 }}>
            {/* 좌측: 파일 트리 */}
            <div style={{ background: 'rgba(0,0,0,0.2)', padding: 8, borderRadius: 6, border: '1px solid rgba(255,255,255,0.08)' }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: '#9ca3af', marginBottom: 8, padding: '0 4px' }}>
                📁 파일 목록 ({data.files.length})
              </div>
              <ul className="filelist" style={{ margin: 0, padding: 0 }}>
                {data.files.map((f) => (
                  <li
                    key={f}
                    className={f === selected ? 'active' : ''}
                    onClick={() => setSelected(f)}
                    style={{ fontSize: 13, cursor: 'pointer', padding: '6px 8px', borderRadius: 4 }}
                  >
                    📄 {f}
                  </li>
                ))}
              </ul>
            </div>

            {/* 중앙: 소스 코드 에디터/뷰어 */}
            <div style={{ display: 'flex', flexDirection: 'column', background: 'rgba(0,0,0,0.3)', borderRadius: 6, border: '1px solid rgba(255,255,255,0.08)', overflow: 'hidden' }}>
              <div style={{ padding: '8px 12px', background: 'rgba(255,255,255,0.03)', borderBottom: '1px solid rgba(255,255,255,0.08)', fontSize: 13, fontWeight: 600, color: '#60a5fa' }}>
                {selected ? `코드 뷰어 — ${selected}` : '파일을 선택하세요'}
              </div>
              <div style={{ flex: 1, padding: 12, overflow: 'auto' }}>
                {!selected && <p className="mutedtext">왼쪽에서 파일을 선택하세요.</p>}
                {selected && error && <p className="error">{error}</p>}
                {selected && !error && (
                  <pre className="logbox" style={{ margin: 0, height: '100%', fontSize: 13, lineHeight: 1.5, fontFamily: 'monospace' }}>
                    {loadingContent ? '소스 코드를 불러오는 중...' : content}
                  </pre>
                )}
              </div>
            </div>

            {/* 우측: 모듈 & 사용 가능 자원 패널 (자원 정보 완벽 흡수 + 실시간 체크) */}
            <div style={{ background: 'rgba(0,0,0,0.2)', padding: 10, borderRadius: 6, border: '1px solid rgba(255,255,255,0.08)', display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: '#f59e0b', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span>📦 모듈 & 자원 사용 현황</span>
                <span style={{ fontSize: 11, color: '#9ca3af' }}>실시간 탐색</span>
              </div>
              <p className="mutedtext" style={{ fontSize: 11, margin: 0 }}>
                프로젝트 이용 가능 자원(API/DB/저장소)이 모듈 목록 패널로 흡수 통합 표시되며, 참조 시 <span style={{ color: '#10b981', fontWeight: 600 }}>☑ 사용 중</span>으로 자동 체크됩니다.
              </p>

              {mergedResources.length === 0 ? (
                <p className="mutedtext" style={{ fontSize: 12 }}>등록되거나 사용 가능한 자원이 없습니다.</p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto', maxHeight: 420 }}>
                  {mergedResources.map((item) => {
                    const { used, matches } = checkItemUsage(item);
                    return (
                      <div
                        key={item.id}
                        style={{
                          padding: 10,
                          borderRadius: 6,
                          backgroundColor: used ? 'rgba(16, 185, 129, 0.08)' : 'rgba(255,255,255,0.02)',
                          border: `1px solid ${used ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255,255,255,0.08)'}`,
                          transition: 'all 0.2s ease',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                          <span style={{ fontSize: 13, fontWeight: 600, color: used ? '#10b981' : '#e5e7eb' }}>
                            {used ? '☑' : '☐'} {item.name}
                          </span>
                          <StatusPill value={item.type} />
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11 }}>
                          <span className="mutedtext">{item.category || (item.env_prefix ? `ENV: ${item.env_prefix}_*` : '일반 자원')}</span>
                          {used ? (
                            <span style={{ color: '#10b981', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 2 }}>
                              ✓ 코드에서 사용 중
                            </span>
                          ) : (
                            <span style={{ color: '#6b7280' }}>☐ 미사용</span>
                          )}
                        </div>

                        {used && matches.length > 0 && (
                          <div style={{ marginTop: 6, paddingTop: 4, borderTop: '1px stroke rgba(255,255,255,0.05)', fontSize: 10, color: '#a7f3d0' }}>
                            감지 키워드: {matches.slice(0, 3).join(', ')}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </Async>
    </div>
  );
}
