import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { SourceKind, SourceOut } from '../lib/types';

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
 * 데이터 수집 — 출처(웹사이트·API·MCP)를 등록하면 스캔이 구조를 읽고, 어느 저장소에 넣을지
 * 제안한다. 저장은 상세 화면에서 사람이 결정한다.
 *
 * 요청 헤더(쿠키·토큰)는 여기서 한 번 받고 다시 보여 주지 않는다 — 서버가 암호화해 두고
 * 그 출처로 가는 요청에만 붙인다.
 */
export default function Sources() {
  const navigate = useNavigate();
  const [kind, setKind] = useState<SourceKind>('web');
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [headers, setHeaders] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const list = useApi(() => api.listSources(), []);

  const create = async () => {
    if (!name.trim() || !url.trim()) return;
    setBusy(true);
    setError('');
    try {
      const made = await api.createSource({ name: name.trim(), kind, url: url.trim(), headers, note });
      // 등록하면 바로 스캔한다 — 등록만 해 두고 다시 와서 누르는 일이 없게. 스캔 시작이
      // 거부돼도 등록은 됐다 — 상세 화면이 상태와 스캔 버튼을 보여 준다.
      await api.scanSource(made.id).catch(() => undefined);
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
        <p className="mutedtext" style={{ fontSize: 12 }}>
          정보 출처를 등록하면 스캔합니다 — 웹사이트는 메뉴 구성과 화면마다 조회할 수 있는 정보를,
          API는 엔드포인트를, MCP 서버는 도구 목록을 읽습니다. 스캔이 끝나면 기존 저장소에 넣을지
          새 저장소를 만들지 제안하고, 저장은 상세 화면에서 결정합니다.
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
          <textarea
            placeholder={'요청 헤더(선택) — 한 줄에 하나\nCookie: SESSION=...\nAuthorization: Bearer ...'}
            value={headers} rows={3} style={{ flex: 1, minWidth: 300 }} className="mono"
            onChange={(e) => setHeaders(e.target.value)} />
          <textarea placeholder="메모(선택)" value={note} rows={3} style={{ flex: 1, minWidth: 200 }}
                    onChange={(e) => setNote(e.target.value)} />
        </div>
        <p className="mutedtext" style={{ fontSize: 11, margin: '6px 0' }}>
          헤더 값은 암호화해 보관하고 화면·작업 로그·LLM 어디에도 보이지 않습니다. 등록한 주소와
          같은 출처(scheme·host·port)로 가는 요청에만 붙습니다.
        </p>
        <div className="row" style={{ gap: 8 }}>
          <button className="small" disabled={busy || !name.trim() || !url.trim()} onClick={create}>
            {busy ? '등록 중…' : '등록하고 스캔'}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </div>

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
