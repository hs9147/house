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
  const me = useApi(() => api.me());
  const state = useApi(() => api.listProjects());
  const [showCreate, setShowCreate] = useState(false);
  const navigate = useNavigate();

  const userOrgs = me.data?.organizations ?? [];
  const userOrgIds = userOrgs.map((o) => o.id);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12, alignItems: 'center', gap: 12 }}>
        <h2 style={{ margin: 0 }}>프로젝트</h2>
        <div className="spacer" />
        <button onClick={() => setShowCreate(true)}>+ 새 프로젝트</button>
      </div>
      <Async state={state} empty="프로젝트가 없습니다.">
        {(projects) => {
          // 사용자가 소속 조직을 가지고 있는 경우, 속해 있는 모든 조직의 프로젝트들을 노출
          const filtered = (userOrgIds.length > 0 && !me.data?.is_admin)
            ? projects.filter((p) => p.organization_id !== null && p.organization_id !== undefined && userOrgIds.includes(p.organization_id))
            : projects;

          return (
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
                {filtered.map((p) => (
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
          );
        }}
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

export function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const me = useApi(() => api.me());
  const orgs = useApi(() => api.listOrgs());
  const [form, setForm] = useState({
    name: '',
    type: 'react' as ProjectType,
    organization_id: '',
    git_url: '',
    branch: 'main',
    domain: '',
    health_check_path: '/',
    default_profile: 'release' as BuildProfile,
  });
  const [sourceMode, setSourceMode] = useState<'upload' | 'git'>('upload'); // 기본값: 직접 업로드
  const [uploadKind, setUploadKind] = useState<'zip' | 'folder' | 'files'>('zip');
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [folderFiles, setFolderFiles] = useState<FileList | null>(null);
  const [singleFiles, setSingleFiles] = useState<FileList | null>(null);
  const [deployAfterUpload, setDeployAfterUpload] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));
  
  // 사용자의 소속 조직 목록 추출
  const userOrgs = me.data?.organizations && me.data.organizations.length > 0
    ? me.data.organizations
    : (me.data?.organization_id && me.data?.organization_name ? [{ id: me.data.organization_id, name: me.data.organization_name }] : []);

  const [selectedOrgId, setSelectedOrgId] = useState<string>(() => {
    return userOrgs.length > 0 ? String(userOrgs[0].id) : (orgs.data && orgs.data.length > 0 ? String(orgs.data[0].id) : '1');
  });

  const targetOrgId = selectedOrgId || (userOrgs.length > 0 ? String(userOrgs[0].id) : '1');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (sourceMode === 'upload') {
      let source: { kind: 'zip'; file: File } | { kind: 'folder'; files: FileList } | null = null;

      if (uploadKind === 'zip') {
        if (!zipFile) {
          setError('ZIP 압축 파일을 선택해 주세요.');
          return;
        }
        source = { kind: 'zip', file: zipFile };
      } else if (uploadKind === 'folder') {
        if (!folderFiles || folderFiles.length === 0) {
          setError('업로드할 폴더를 선택해 주세요.');
          return;
        }
        source = { kind: 'folder', files: folderFiles };
      } else {
        if (!singleFiles || singleFiles.length === 0) {
          setError('업로드할 소스 파일을 선택해 주세요.');
          return;
        }
        source = { kind: 'folder', files: singleFiles };
      }

      setBusy(true);
      try {
        await api.uploadProject(
          {
            name: form.name,
            type: form.type,
            organization_id: Number(targetOrgId),
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
      organization_id: Number(targetOrgId),
    };
    try {
      await api.createProject(payload);
      onCreated();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <Modal title="새 프로젝트 생성" onClose={onClose}>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {userOrgs.length > 1 ? (
          <label className="field">
            소속 조직 선택 (다수 소속 조직 중 프로젝트를 생성할 조직을 선택하세요)
            <select
              value={targetOrgId}
              onChange={(e) => setSelectedOrgId(e.target.value)}
              style={{ fontSize: 13, padding: '6px 8px' }}
            >
              {userOrgs.map((o) => (
                <option key={o.id} value={o.id}>
                  🏢 {o.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <div style={{ fontSize: 12, padding: '6px 10px', borderRadius: 4, background: 'rgba(56, 189, 248, 0.1)', color: '#38bdf8', border: '1px solid rgba(56, 189, 248, 0.2)' }}>
            🏢 소속 조직: <strong>{userOrgs[0]?.name || me.data?.organization_name || '기본 조직'}</strong> (사용자의 소속 조직으로 등록됩니다)
          </div>
        )}
        <label className="field">
          프로젝트 이름 — 빈칸 없이 소문자·숫자·하이픈만 사용 (예: my-web-app)
          <input
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            pattern="[a-z0-9][a-z0-9-]{1,40}"
            title="빈칸 없이 소문자·숫자·하이픈만 사용하세요 (예: my-web-app)"
            placeholder="my-web-app"
            required
            autoFocus
          />
        </label>
        
        <label className="field">
          프로젝트 타입
          <select value={form.type} onChange={(e) => set('type', e.target.value)}>
            <option value="react">react</option>
            <option value="python">python (FastAPI)</option>
            <option value="streamlit">streamlit</option>
            <option value="node">node</option>
            <option value="html">html (정적 웹)</option>
            <option value="llm">llm</option>
            <option value="composite">복합 — 백엔드+프론트엔드</option>
          </select>
        </label>

        {/* 소스 등록 방식 선택 탭 */}
        <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.03)' }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', marginBottom: 8, display: 'block' }}>
            소스 등록 방식 선택
          </span>
          <div className="row" style={{ gap: 16, marginBottom: 10 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 14 }}>
              <input
                type="radio"
                name="sourceMode"
                checked={sourceMode === 'upload'}
                onChange={() => setSourceMode('upload')}
              />
              📁 소스 직접 업로드 (파일 / ZIP / 폴더)
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 14 }}>
              <input
                type="radio"
                name="sourceMode"
                checked={sourceMode === 'git'}
                onChange={() => setSourceMode('git')}
              />
              🔗 외부 Git URL 연동
            </label>
          </div>

          {sourceMode === 'upload' ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 6 }}>
              <div className="row" style={{ gap: 12 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, cursor: 'pointer' }}>
                  <input
                    type="radio"
                    name="uploadKind"
                    checked={uploadKind === 'zip'}
                    onChange={() => setUploadKind('zip')}
                  />
                  ZIP 압축파일
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, cursor: 'pointer' }}>
                  <input
                    type="radio"
                    name="uploadKind"
                    checked={uploadKind === 'folder'}
                    onChange={() => setUploadKind('folder')}
                  />
                  전체 폴더
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, cursor: 'pointer' }}>
                  <input
                    type="radio"
                    name="uploadKind"
                    checked={uploadKind === 'files'}
                    onChange={() => setUploadKind('files')}
                  />
                  개별 파일
                </label>
              </div>

              {uploadKind === 'zip' && (
                <input
                  type="file"
                  accept=".zip,.tar.gz"
                  onChange={(e) => setZipFile(e.target.files?.[0] ?? null)}
                  required
                />
              )}
              {uploadKind === 'folder' && (
                <input
                  type="file"
                  multiple
                  ref={(el) => {
                    if (el) el.setAttribute('webkitdirectory', '');
                  }}
                  onChange={(e) => setFolderFiles(e.target.files)}
                  required
                />
              )}
              {uploadKind === 'files' && (
                <input
                  type="file"
                  multiple
                  onChange={(e) => setSingleFiles(e.target.files)}
                  required
                />
              )}

              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, marginTop: 4 }}>
                <input
                  type="checkbox"
                  checked={deployAfterUpload}
                  onChange={(e) => setDeployAfterUpload(e.target.checked)}
                />
                업로드 즉시 자동 빌드 및 배포
              </label>
            </div>
          ) : (
            <label className="field" style={{ marginTop: 6 }}>
              Git 주소 (HTTPS/SSH)
              <input
                value={form.git_url}
                onChange={(e) => set('git_url', e.target.value)}
                placeholder="https://github.com/user/repo.git"
                required={sourceMode === 'git'}
              />
            </label>
          )}
        </div>

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
            {busy ? (sourceMode === 'upload' ? '업로드 중...' : '생성 중...') : '생성'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
