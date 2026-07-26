import { useEffect, useState } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ProjectContext } from '../ProjectDetail';

export default function CodeTab() {
  const { project } = useOutletContext<ProjectContext>();
  const filesState = useApi(() => api.projectFiles(project.id), [project.id]);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!selected) return;
    setLoadingContent(true);
    setError('');
    api
      .projectFileContent(project.id, selected)
      .then((res) => setContent(res.content))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoadingContent(false));
  }, [project.id, selected]);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>코드 확인</h2>
        <div className="spacer" />
        <span className="mutedtext" style={{ fontSize: 12 }}>
          읽기 전용 — 코드 수정은 <Link to="/chat">채팅</Link>에서 diff 제안·승인으로 진행합니다.
        </span>
      </div>
      <Async state={filesState} empty="리포에 파일이 없습니다.">
        {(data) => (
          <div className="code-split">
            <ul className="filelist">
              {data.files.map((f) => (
                <li
                  key={f}
                  className={f === selected ? 'active' : ''}
                  onClick={() => setSelected(f)}
                >
                  {f}
                </li>
              ))}
            </ul>
            <div className="code-view">
              {!selected && <p className="mutedtext">왼쪽에서 파일을 선택하세요.</p>}
              {selected && error && <p className="error">{error}</p>}
              {selected && !error && (
                <pre className="logbox">
                  {loadingContent ? '불러오는 중...' : content}
                </pre>
              )}
            </div>
          </div>
        )}
      </Async>
    </div>
  );
}
