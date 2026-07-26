import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

export default function Git() {
  const health = useApi(() => api.health());
  const projects = useApi(() => api.listProjects());
  const giteaUrl = health.data?.gitea_url;

  return (
    <>
      <div className="panel">
        <h2>사내 Git 서버</h2>
        {giteaUrl ? (
          <div className="row">
            <span className="mono">{giteaUrl}</span>
            <div className="spacer" />
            <a className="btn" href={giteaUrl} target="_blank" rel="noreferrer">
              Gitea 열기 ↗
            </a>
          </div>
        ) : (
          <p className="mutedtext">
            사내 Git 서버가 설정되지 않았습니다. <span className="mono">PAAS_GITEA_URL</span>을
            지정하면 여기 표시됩니다 — 배포 안내는{' '}
            <span className="mono">platform/infra/gitea/README.md</span> 참고.
          </p>
        )}
      </div>

      <div className="panel">
        <h2>프로젝트 리포지토리</h2>
        <Async state={projects} empty="등록된 프로젝트가 없습니다.">
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>프로젝트</th>
                  <th>Git URL</th>
                  <th>브랜치</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    <td>{p.name}</td>
                    <td className="mono">{p.git_url}</td>
                    <td className="mono">{p.branch}</td>
                    <td>
                      <a href={p.git_url} target="_blank" rel="noreferrer">
                        리포 열기 ↗
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </div>
    </>
  );
}
