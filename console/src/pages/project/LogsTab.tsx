import { useEffect, useRef, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { api } from '../../lib/api';
import { usePolling } from '../../lib/hooks';
import type { BuildProfile } from '../../lib/types';
import type { ProjectContext } from '../ProjectDetail';

export default function LogsTab() {
  const { project } = useOutletContext<ProjectContext>();
  const [profile, setProfile] = useState<BuildProfile>('release');
  const [tail, setTail] = useState(200);
  const [polling, setPolling] = useState(true);
  const [logs, setLogs] = useState('');
  const [error, setError] = useState('');
  const boxRef = useRef<HTMLPreElement>(null);

  usePolling(
    async () => {
      try {
        const res = await api.logs(project.id, profile, tail);
        setLogs(res.logs || '(로그 없음)');
        setError('');
      } catch (e) {
        setError((e as Error).message);
      }
    },
    3000,
    polling,
  );

  useEffect(() => {
    // 새 내용 도착 시 하단 고정
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [logs]);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>로그</h2>
        <div className="spacer" />
        <select value={profile} onChange={(e) => setProfile(e.target.value as BuildProfile)}>
          <option value="release">release</option>
          <option value="development">development</option>
        </select>
        <select value={tail} onChange={(e) => setTail(Number(e.target.value))}>
          <option value={100}>tail 100</option>
          <option value={200}>tail 200</option>
          <option value={500}>tail 500</option>
          <option value={1000}>tail 1000</option>
        </select>
        <button
          className={polling ? 'secondary small' : 'small'}
          onClick={() => setPolling((p) => !p)}
        >
          {polling ? '자동 갱신 중지' : '자동 갱신 시작 (3초)'}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <pre className="logbox" ref={boxRef}>
        {logs || '불러오는 중...'}
      </pre>
    </div>
  );
}
