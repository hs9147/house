import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import Modal from '../components/Modal';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { fmtDate } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { BuildProfile, ProjectCreate, ProjectType } from '../lib/types';

export default function Projects() {
  const state = useApi(() => api.listProjects());
  const [showCreate, setShowCreate] = useState(false);
  const navigate = useNavigate();

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>프로젝트</h2>
        <div className="spacer" />
        <button onClick={() => setShowCreate(true)}>+ 새 프로젝트</button>
      </div>
      <Async state={state} empty="프로젝트가 없습니다.">
        {(projects) => (
          <table>
            <thead>
              <tr>
                <th>이름</th>
                <th>타입</th>
                <th>Git</th>
                <th>브랜치</th>
                <th>기본 프로필</th>
                <th>생성일</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((p) => (
                <tr key={p.id} className="clickable" onClick={() => navigate(`/projects/${p.id}`)}>
                  <td>{p.name}</td>
                  <td><StatusPill value={p.type} /></td>
                  <td className="mono">{p.git_url}</td>
                  <td className="mono">{p.branch}</td>
                  <td><StatusPill value={p.default_profile} /></td>
                  <td className="mono">{fmtDate(p.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      {showCreate && (
        <CreateModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            state.reload();
          }}
        />
      )}
    </div>
  );
}

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const orgs = useApi(() => api.listOrgs());
  const [form, setForm] = useState({
    name: '',
    type: 'react' as ProjectType,
    organization_id: '', // 빈 문자열 = 직접 Git URL 입력(레거시 경로)
    git_url: '',
    branch: 'main',
    domain: '',
    health_check_path: '/',
    default_profile: 'release' as BuildProfile,
  });
  const [uploadMode, setUploadMode] = useState(false); // zip/폴더 업로드 (조직 소속 시에만)
  const [uploadKind, setUploadKind] = useState<'zip' | 'folder'>('zip');
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [folderFiles, setFolderFiles] = useState<FileList | null>(null);
  const [deployAfterUpload, setDeployAfterUpload] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const usingOrg = form.organization_id !== '';

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (usingOrg && uploadMode) {
      const source: { kind: 'zip'; file: File } | { kind: 'folder'; files: FileList } | null =
        uploadKind === 'zip'
          ? zipFile
            ? { kind: 'zip', file: zipFile }
            : null
          : folderFiles && folderFiles.length > 0
            ? { kind: 'folder', files: folderFiles }
            : null;
      if (!source) {
        setError(uploadKind === 'zip' ? 'zip 파일을 선택하세요.' : '폴더를 선택하세요.');
        return;
      }
      setBusy(true);
      try {
        await api.uploadProject(
          {
            name: form.name,
            type: form.type,
            organization_id: Number(form.organization_id),
            branch: form.branch,
            domain: form.domain || undefined,
            health_check_path: form.health_check_path,
            default_profile: form.default_profile,
            deploy_after_upload: deployAfterUpload,
          },
          source,
        );
        onCreated();
      } catch (err) {
        setError((err as Error).message);
        setBusy(false);
      }
      return;
    }

    setBusy(true);
    const payload: ProjectCreate = {
      name: form.name,
      type: form.type,
      branch: form.branch,
      domain: form.domain || null,
      health_check_path: form.health_check_path,
      default_profile: form.default_profile,
    };
    if (usingOrg) {
      payload.organization_id = Number(form.organization_id);
    } else {
      payload.git_url = form.git_url;
    }
    try {
      await api.createProject(payload);
      onCreated();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <Modal title="새 프로젝트" onClose={onClose}>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <label className="field">
          이름 — 빈칸 없이 소문자·숫자·하이픈만 사용하세요 (예: portal-web)
          <input
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            pattern="[a-z0-9][a-z0-9-]{1,40}"
            title="빈칸 없이 소문자·숫자·하이픈만 사용하세요 (예: portal-web)"
            placeholder="portal-web"
            required
          />
        </label>
        <label className="field">
          타입
          <select value={form.type} onChange={(e) => set('type', e.target.value)}>
            <option value="react">react</option>
            <option value="python">python (FastAPI)</option>
            <option value="streamlit">streamlit</option>
            <option value="node">node</option>
            <option value="html">html (정적)</option>
            <option value="llm">llm</option>
            <option value="composite">복합 — 백엔드+프론트엔드</option>
          </select>
        </label>
        {form.type === 'composite' && (
          <p className="mutedtext">
            리포 루트에 <code>backend/</code>, <code>frontend/</code> 서브폴더가 모두 있어야
            합니다 — 배포 시 각 폴더의 타입(python/node/react/html)을 자동 감지해 따로
            빌드하고, 같은 도메인에서 <code>/api/*</code>는 백엔드로, <code>/*</code>는
            프론트엔드로 자동 라우팅합니다.
          </p>
        )}

        {orgs.data && orgs.data.length > 0 && (
          <label className="field">
            조직 (선택 시 사내 Gitea 리포를 내부에서 자동 생성 — Git 주소는 노출되지 않음)
            <select
              value={form.organization_id}
              onChange={(e) => set('organization_id', e.target.value)}
            >
              <option value="">직접 Git URL 입력</option>
              {orgs.data.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {!usingOrg && (
          <label className="field">
            Git URL
            <input value={form.git_url} onChange={(e) => set('git_url', e.target.value)} required />
          </label>
        )}
        {usingOrg && (
          <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <input
              type="checkbox"
              checked={uploadMode}
              onChange={(e) => setUploadMode(e.target.checked)}
            />
            git 리포 대신 zip/폴더를 직접 업로드
          </label>
        )}
        {usingOrg && uploadMode && (
          <div className="panel" style={{ padding: 12, margin: 0 }}>
            <div className="row" style={{ marginBottom: 8 }}>
              <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <input
                  type="radio"
                  name="uploadKind"
                  checked={uploadKind === 'zip'}
                  onChange={() => setUploadKind('zip')}
                />
                zip 파일
              </label>
              <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <input
                  type="radio"
                  name="uploadKind"
                  checked={uploadKind === 'folder'}
                  onChange={() => setUploadKind('folder')}
                />
                폴더
              </label>
            </div>
            {uploadKind === 'zip' ? (
              <input
                type="file"
                accept=".zip"
                onChange={(e) => setZipFile(e.target.files?.[0] ?? null)}
              />
            ) : (
              <input
                type="file"
                multiple
                ref={(el) => {
                  if (el) el.setAttribute('webkitdirectory', '');
                }}
                onChange={(e) => setFolderFiles(e.target.files)}
              />
            )}
            <label
              className="field"
              style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 10 }}
            >
              <input
                type="checkbox"
                checked={deployAfterUpload}
                onChange={(e) => setDeployAfterUpload(e.target.checked)}
              />
              업로드 완료 후 바로 배포 (원클릭)
            </label>
          </div>
        )}

        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            브랜치
            <input value={form.branch} onChange={(e) => set('branch', e.target.value)} />
          </label>
          <label className="field" style={{ flex: 1 }}>
            기본 프로필
            <select
              value={form.default_profile}
              onChange={(e) => set('default_profile', e.target.value)}
            >
              <option value="release">release</option>
              <option value="development">development</option>
            </select>
          </label>
        </div>
        <label className="field">
          도메인 (선택 — 비우면 {'{이름}.{기본도메인}'})
          <input value={form.domain} onChange={(e) => set('domain', e.target.value)} />
        </label>
        <label className="field">
          헬스체크 경로
          <input
            value={form.health_check_path}
            onChange={(e) => set('health_check_path', e.target.value)}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="secondary" onClick={onClose}>
            취소
          </button>
          <button type="submit" disabled={busy}>
            {busy ? (usingOrg && uploadMode ? '업로드 중...' : '생성 중...') : '생성'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
