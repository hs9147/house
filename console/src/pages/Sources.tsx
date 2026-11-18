import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import Tabs from '../components/Tabs';
import { api } from '../lib/api';
import { useApi, type AsyncState } from '../lib/hooks';
import type { SourceKind, SourceOut } from '../lib/types';
import { InventoryTab } from './SourceCoverage';
import { SkillCatalog } from './SourceSkills';

export const SOURCE_KINDS: { key: SourceKind; label: string; hint: string }[] = [
  { key: 'web', label: '웹사이트', hint: 'https://intra.example.com/ — 시작 페이지' },
  { key: 'api', label: 'API', hint: 'https://api.example.com/ — OpenAPI 문서나 JSON 응답 주소' },
  { key: 'mcp', label: 'MCP 서버', hint: 'https://mcp.example.com/mcp — Streamable HTTP 엔드포인트' },
];

const STATUS: Record<SourceOut['status'], { label: string; cls: string }> = {
  new: { label: '등록됨', cls: 'dim' },
  scanning: { label: '스캔 중', cls: 'warn' },
  scanned: { label: '저장 대기', cls: 'info' },
  failed: { label: '실패', cls: 'bad' },
  saved: { label: '저장됨', cls: 'ok' },
};

export function SourceStatus({ value }: { value: SourceOut['status'] }) {
  const s = STATUS[value] ?? { label: value, cls: 'dim' };
  return <span className={`status ${s.cls}`}>{s.label}</span>;
}

export function kindLabel(kind: SourceKind): string {
  return SOURCE_KINDS.find((k) => k.key === kind)?.label ?? kind;
}

/**
 * 데이터 수집 — ① 출처(웹사이트·API·MCP)를 등록하고 크롤링(스캔)해 구조를 읽고, ② 조회 화면마다 수집 조건과
 * 스킬(이름·용도·입력·출력)을 정해 에이전트가 부르는 일로 만들고, ③ 저장소로 수집한다(저장 위치는 사람이 정하고,
 * 그다음부터는 서버 봇·스케줄러가 같은 자리에). 「스킬」 탭은 모든 출처의 스킬을 모아 보고 지금 돌려 본다.
 */
export default function Sources() {
  const navigate = useNavigate();
  const [kind, setKind] = useState<SourceKind>('web');
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const list = useApi(() => api.listSources(), []);

  const create = async () => {
    if (!name.trim() || !url.trim()) return;
    setBusy(true);
    setError('');
    try {
      // 등록만 한다 — 스캔은 상세 화면에서 고른다(서버 스캔 · 북마크로 브라우저에서).
      const made = await api.createSource({ name: name.trim(), kind, url: url.trim(), note });
      navigate(`/sources/${made.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: SourceOut) => {
    if (!window.confirm(`'${row.name}'을 지웁니까? 이미 저장한 문서는 그대로 남습니다.`)) return;
    setError('');
    try {
      await api.deleteSource(row.id);
      list.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const hint = SOURCE_KINDS.find((k) => k.key === kind)?.hint ?? '';

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>데이터 수집</h2>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap', fontSize: 12, margin: '4px 0 8px' }}>
          <span className="status info">① 출처 등록 · 크롤링</span>→
          <span className="status info">② 수집 조건 · 스킬 만들기</span>→
          <span className="status info">③ 저장소 수집</span>
        </div>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          ① 정보 출처를 등록하고 상세 화면에서 크롤링(스캔)합니다 — 웹사이트는 메뉴 구성과 화면마다 조회할 수 있는
          정보를, API는 엔드포인트를, MCP 서버는 도구 목록을 읽습니다. ② 조회 화면마다 수집 조건(여는 길·조건·받을 것)을
          정하고 <b>스킬</b>(이름·용도·입력·결과 열·공개 범위)을 붙이면, 에이전트가 입력을 넣어 그 화면을 지금 조회하는
          일이 됩니다(스마트워크 · MCP <span className="mono">/mcp/skills</span>). ③ 저장 위치를 정해 저장소로 수집하면,
          그다음부터는 서버 봇·무인 수집이 같은 자리에 받습니다.
        </p>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <select value={kind} onChange={(e) => setKind(e.target.value as SourceKind)}>
            {SOURCE_KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
          </select>
          <input placeholder="이름 — 예: 인사 포털" value={name} style={{ minWidth: 180 }}
                 onChange={(e) => setName(e.target.value)} />
          <input placeholder={hint} value={url} style={{ minWidth: 360, flex: 1 }}
                 onChange={(e) => setUrl(e.target.value)} />
        </div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8, alignItems: 'flex-start' }}>
          <textarea placeholder="메모(선택)" value={note} rows={3} style={{ flex: 1, minWidth: 200 }}
                    onChange={(e) => setNote(e.target.value)} />
        </div>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <button className="small" disabled={busy || !name.trim() || !url.trim()} onClick={create}>
            {busy ? '등록 중…' : '등록'}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </div>

      <Tabs tabs={[
        { key: 'sources', label: '출처', content: <SourceList list={list} navigate={navigate} remove={remove} /> },
        { key: 'skills', label: '스킬', content: <SkillCatalog /> },
        { key: 'inventory', label: '정보 현황', content: <InventoryTab /> },
      ]} />
    </>
  );
}

function SourceList({ list, navigate, remove }: {
  list: AsyncState<SourceOut[]>;
  navigate: ReturnType<typeof useNavigate>;
  remove: (row: SourceOut) => void;
}) {
  return (
    <>
      <Async state={list}>
        {(rows: SourceOut[]) => (
          <div className="panel">
            {rows.length === 0 ? (
              <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>아직 등록한 출처가 없습니다.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>이름</th><th>종류</th><th>주소</th><th>상태</th><th>스캔 결과</th>
                    <th>저장소</th><th>스캔</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <a href={`#/sources/${row.id}`}
                           onClick={(e) => { e.preventDefault(); navigate(`/sources/${row.id}`); }}>
                          {row.name}
                        </a>
                        {row.note && <div className="mutedtext" style={{ fontSize: 11 }}>{row.note}</div>}
                      </td>
                      <td>{kindLabel(row.kind)}</td>
                      <td className="mono" style={{ fontSize: 11, wordBreak: 'break-all' }}>{row.url}</td>
                      <td><SourceStatus value={row.status} /></td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {row.kind === 'web' && `${row.counts.pages}쪽${row.counts.files ? ` · 파일 ${row.counts.files}` : ''}`}
                        {row.kind === 'api' && `${row.counts.endpoints}개 엔드포인트`}
                        {row.kind === 'mcp' && `${row.counts.tools}개 도구`}
                      </td>
                      <td className="mono">{row.target_store || '—'}</td>
                      <td className="mutedtext" style={{ fontSize: 11 }}>
                        {row.scanned_at ? new Date(row.scanned_at).toLocaleString('ko-KR') : '—'}
                      </td>
                      <td>
                        <button className="small secondary" onClick={() => remove(row)}>삭제</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </Async>
    </>
  );
}
