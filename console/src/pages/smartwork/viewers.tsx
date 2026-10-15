import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import type { WorkBook } from 'xlsx';
import { api } from '../../lib/api';
import {
  applyTextEdit, decodeText, editedName, encodeText, lookup, parseCell, remember,
} from '../../lib/localfiles';
import { parseCsv } from '../../lib/markdown';
import { bookFromRows, writeCell } from '../../lib/sheets';
import type {
  SmartworkFile, SmartworkFileFormat, SmartworkWeb,
} from '../../lib/types';

/**
 * 스마트워크 오른쪽 화면의 웹 페이지와 파일 뷰어.
 *
 * **둘 다 사람의 브라우저에서 일한다.** 웹 페이지는 그 사람의 로그인으로 열리고(서버가 대신
 * 열면 그 사람의 권한이 없다), PC 파일은 브라우저가 쥔 File로 연다(서버에는 원본이 없다).
 * 모델은 무엇을 열지와 무엇을 고칠지를 **제안**할 뿐이다 — 연 주소는 사람이 「열기」를 눌러야
 * 열리고, 수정안은 사람이 「적용」해야 버퍼에 들어가며, 저장은 내려받기다.
 */

// --- 웹 페이지 ---

// 콘솔 창을 다른 곳으로 보내지 못하게 top 이동만 막는다(allow-top-navigation 없음) —
// 로그인·폼·팝업·내려받기는 그 사이트가 하던 대로 된다.
const WEB_SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-popups '
  + 'allow-popups-to-escape-sandbox allow-downloads allow-modals';

export function WebView({ web }: { web: SmartworkWeb }) {
  const [address, setAddress] = useState(web.url);
  const [loaded, setLoaded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 모델이 새 주소를 올리면 **열지 않고** 주소 칸에만 둔다 — 사람의 로그인으로 열리는 주소라,
  // 문서에 심긴 지시에 넘어간 모델이 고른 주소가 저절로 열리면 그 사람의 권한으로 실행된다.
  useEffect(() => {
    setAddress(web.url);
    setLoaded(null);
    setError(null);
  }, [web]);

  const go = () => {
    const raw = address.trim();
    const url = /^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : `https://${raw}`;
    if (!/^https?:\/\/[^\s/]/i.test(url)) {
      setError('http·https 주소만 엽니다.');
      return;
    }
    setError(null);
    setAddress(url);
    setLoaded(url);
  };

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <input value={address} placeholder="주소 — 예: https://portal.example.com" style={{ flex: 1 }}
          onChange={(e) => setAddress(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') go(); }} />
        <button className="small" disabled={!address.trim()} onClick={go}>열기</button>
        {loaded && <a href={loaded} target="_blank" rel="noopener noreferrer">새 창으로 열기</a>}
      </div>
      {error && <p className="error">{error}</p>}
      {!loaded && web.url && (
        <p className="mutedtext" style={{ fontSize: 12 }}>
          대화가 올린 주소입니다{web.reason ? ` — ${web.reason}` : ''}. 내 로그인으로 열리므로 주소를
          확인하고 「열기」를 누르세요.
        </p>
      )}
      {loaded ? (
        <>
          {/* 사이트가 X-Frame-Options·frame-ancestors로 끼워 넣기를 막으면 빈 칸으로 보인다 —
              그때를 위해 새 창 링크를 함께 둔다(AgentView와 같은 이유).
              로그인이 안 되는 것은 다른 까닭이다: 브라우저는 **다른 사이트의 틀 안**에는 그 사이트의
              로그인 쿠키(SameSite 미지정 = Lax)를 싣지 않는다. 사이트는 스킴까지 본다 — 콘솔이 http면
              https://….lge.com도 다른 사이트라 틀 안에서는 로그인이 빠진다(콘솔을 https로 하면 같은 사이트). */}
          <iframe key={loaded} title={loaded} src={loaded} sandbox={WEB_SANDBOX}
            className="smartwork-frame" style={{ background: '#fff' }} />
          <p className="mutedtext" style={{ fontSize: 11 }}>
            빈 화면이면 그 사이트가 다른 화면 안에 띄우는 것을 막은 것입니다. 브라우저에는 로그인돼 있는데 여기서
            권한 없음·로그인 화면이 보이면, 브라우저가 이 창 안으로는 그 사이트의 로그인을 넘기지 않은 것입니다
            {location.protocol === 'http:' && '(콘솔이 http라 https 사이트의 로그인이 전달되지 않음)'} — 두 경우 모두
            「새 창으로 열기」를 쓰세요.
          </p>
        </>
      ) : !web.url && (
        <p className="mutedtext">주소를 넣고 「열기」를 누르면 이 브라우저의 로그인으로 엽니다.</p>
      )}
    </div>
  );
}

// --- 파일 ---

type Family = 'text' | 'sheet' | 'office';

function familyOf(format: SmartworkFileFormat): Family {
  if (format === 'txt' || format === 'csv') return 'text';
  if (format === 'xls' || format === 'xlsx') return 'sheet';
  return 'office';
}

function download(data: BlobPart, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([data], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * 파일 탭 — 대화(open_file·make_file)가 띄운 파일 하나를 연다. 고를 수 있는 문서 목록은 사람에게
 * 보이지 않는다 — 어느 문서를 열지는 스마트워크가 이 업무에 고른 폴더 안에서 정한다.
 */
export function FileView({ file }: { file: SmartworkFile }) {
  const personal = file.source === 'personal';
  // PC 파일은 동기화 때 고른 폴더의 것을 연다(lib/localfiles) — 없을 때만 그 폴더를 다시 고른다.
  const [blob, setBlob] = useState<File | null>(null);
  const [looking, setLooking] = useState(personal);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!personal) return;
    let live = true;
    void lookup(file.folder ?? '', file.path ?? '').then((found) => {
      if (!live) return;
      setBlob(found ?? null);
      setLooking(false);
    });
    return () => { live = false; };
  }, [personal, file.folder, file.path]);

  const pick = async (list: FileList) => {
    const all = Array.from(list);
    if (all.length === 0) return;
    const folder = all[0].webkitRelativePath.split('/')[0];
    if (folder !== file.folder) {
      setError(`'${file.folder}' 폴더를 골라야 합니다 — '${folder}'를 골랐습니다.`);
      return;
    }
    await remember(folder, all.map((f) => ({ path: f.webkitRelativePath.split('/').slice(1).join('/'), file: f })));
    const found = await lookup(folder, file.path ?? '');
    if (!found) {
      setError(`그 폴더에 ${file.path}가 없습니다 — 옮기거나 지웠다면 다시 동기화하세요.`);
      return;
    }
    setError(null);
    setBlob(found);
  };

  const family = familyOf(file.format);
  let body;
  if (personal && looking) {
    body = <p className="mutedtext">동기화한 파일을 찾는 중...</p>;
  } else if (personal && !blob) {
    body = (
      <>
        <p>
          이 파일은 내 PC에 있는데, 이 브라우저에는 동기화 때 고른 사본이 없습니다(다른 브라우저·PC에서
          동기화했거나 브라우저 저장소를 지웠을 때). <b>{file.folder}</b> 폴더를 한 번 골라 주세요
          (서버로 올라가지 않습니다).
        </p>
        <input type="file" multiple style={{ display: 'none' }}
          ref={(el) => {
            picker.current = el;
            if (el) el.setAttribute('webkitdirectory', '');
          }}
          onChange={(e) => {
            if (e.target.files) void pick(e.target.files);
            e.target.value = '';
          }} />
        <button onClick={() => picker.current?.click()}>‘{file.folder}’ 폴더 고르기</button>
      </>
    );
  } else if (family === 'text') {
    body = <TextEditor file={file} blob={blob} />;
  } else if (family === 'sheet') {
    body = <SheetEditor file={file} blob={blob} />;
  } else if (blob) {
    body = <OfficePreview name={file.name} blob={blob} />;
  } else {
    body = <p className="mutedtext">이 형식은 내 PC 문서만 열 수 있습니다.</p>;
  }

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>{file.name}</h2>
        <span className="status dim">{file.format}</span>
        <span className="mutedtext" style={{ fontSize: 12 }}>
          {personal ? `내 PC · ${file.folder}/${file.path}` : '대화가 만든 파일'}
        </span>
        <span className="spacer" />
      </div>
      {error && <p className="error">{error}</p>}
      {body}
    </div>
  );
}

// --- 글(txt·csv) ---

function TextEditor({ file, blob }: { file: SmartworkFile; blob: File | null }) {
  const generated = file.source === 'generated';
  const [text, setText] = useState<string | null>(generated ? file.content ?? '' : null);
  // 저장할 때 원본의 인코딩 사정을 따른다(lib/localfiles.encodeText). 만든 CSV는 한글 엑셀이
  // 바로 읽도록 BOM을 붙인다.
  const encoding = useRef({ bom: generated && file.format === 'csv', legacy: false });
  const [asTable, setAsTable] = useState(file.format === 'csv');
  const [applied, setApplied] = useState<Record<number, string>>({});
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!blob) return;
    let alive = true;
    void blob.arrayBuffer().then((bytes) => {
      if (!alive) return;
      const decoded = decodeText(bytes);
      encoding.current = { bom: decoded.bom, legacy: decoded.legacy };
      setText(decoded.text);
    });
    return () => { alive = false; };
  }, [blob]);

  if (text === null) return <p className="mutedtext">여는 중...</p>;

  const apply = (i: number, find: string, replace: string) => {
    const out = applyTextEdit(text, find, replace);
    // 못 찾았으면 그렇다고 적는다 — 조용히 넘어가면 고쳐진 줄 안다.
    setApplied((cur) => ({ ...cur, [i]: out.count === 0 ? '못 찾음' : `적용 ${out.count}곳` }));
    if (out.count > 0) {
      setText(out.text);
      setDirty(true);
    }
  };
  const save = () => {
    const name = generated ? file.name : editedName(file.name);
    download(encodeText(text, encoding.current), name,
      file.format === 'csv' ? 'text/csv;charset=utf-8' : 'text/plain;charset=utf-8');
    setDirty(false);
  };
  const rows = asTable ? parseCsv(text) : [];

  return (
    <>
      <EditList
        edits={(file.edits ?? []).filter((e) => e.find).map((e) => ({
          label: <><s>{e.find}</s> → <b>{e.replace}</b></>, why: e.why,
          onApply: () => apply(file.edits!.indexOf(e), e.find!, e.replace ?? ''),
          status: applied[file.edits!.indexOf(e)],
        }))} />
      <div className="row" style={{ marginBottom: 6 }}>
        {file.format === 'csv' && (
          <button className="small secondary" onClick={() => setAsTable(!asTable)}>
            {asTable ? '글로 고치기' : '표로 보기'}
          </button>
        )}
        <span className="spacer" />
        {dirty && <span className="mutedtext" style={{ fontSize: 12 }}>저장하지 않은 수정이 있습니다</span>}
        <button className="small" onClick={save}>{generated ? '내려받기' : '수정본 내려받기'}</button>
      </div>
      {asTable ? (
        <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 260px)' }}>
          <table>
            <tbody>
              {rows.slice(0, MAX_ROWS).map((r, i) => (
                <tr key={i}>{r.map((c, j) => (i === 0 ? <th key={j}>{c}</th> : <td key={j}>{c}</td>))}</tr>
              ))}
            </tbody>
          </table>
          {rows.length > MAX_ROWS && <p className="mutedtext">앞 {MAX_ROWS}행만 보입니다.</p>}
        </div>
      ) : (
        <textarea value={text} spellCheck={false} className="mono"
          style={{ width: '100%', height: 'calc(100vh - 260px)', minHeight: 360, fontSize: 12 }}
          onChange={(e) => {
            setText(e.target.value);
            setDirty(true);
          }} />
      )}
    </>
  );
}

// --- 시트(xls·xlsx) ---

// 화면에 그리는 범위. 저장은 통합 문서 전체를 쓴다 — 보이지 않는 행도 그대로 남는다.
const MAX_ROWS = 500;
const MAX_COLS = 50;

type Xlsx = typeof import('xlsx');

function SheetEditor({ file, blob }: { file: SmartworkFile; blob: File | null }) {
  const generated = file.source === 'generated';
  const lib = useRef<Xlsx | null>(null);
  const [wb, setWb] = useState<WorkBook | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sheetIndex, setSheetIndex] = useState(0);
  // 통합 문서는 제자리에서 바뀐다 — 셀을 고칠 때마다 늘려 다시 그린다.
  const [version, setVersion] = useState(0);
  const [editing, setEditing] = useState<{ addr: string; value: string } | null>(null);
  const [applied, setApplied] = useState<Record<number, string>>({});
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      // 엑셀 라이브러리는 이 화면을 열 때만 받는다 — 콘솔 첫 화면이 그만큼 무거워지지 않게.
      const X = await import('xlsx');
      lib.current = X;
      const book = generated ? bookFromRows(X, file.sheets ?? [])
        : X.read(await blob!.arrayBuffer(), { type: 'array' });
      if (alive) setWb(book);
    })().catch((e: Error) => { if (alive) setError(`열지 못했습니다: ${e.message}`); });
    return () => { alive = false; };
  }, [file, blob, generated]);

  const grid = useMemo(() => {
    const X = lib.current;
    if (!X || !wb) return null;
    const name = wb.SheetNames[sheetIndex];
    const ws = wb.Sheets[name];
    const range = X.utils.decode_range(ws['!ref'] ?? 'A1');
    const rows = X.utils.sheet_to_json<string[]>(ws, { header: 1, raw: false, defval: '', blankrows: true });
    return { name, range, rows };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wb, sheetIndex, version]);

  if (error) return <p className="error">{error}</p>;
  if (!wb || !grid || !lib.current) return <p className="mutedtext">여는 중...</p>;
  const X = lib.current;

  const setCell = (sheetName: string, addr: string, value: string) => {
    writeCell(X, wb.Sheets[sheetName], addr, value);
    setVersion((n) => n + 1);
    setDirty(true);
  };

  const commit = () => {
    if (editing) setCell(grid.name, editing.addr, editing.value);
    setEditing(null);
  };

  const proposals = (file.edits ?? []).map((e, i) => ({ e, i, ref: parseCell(e.cell ?? '') }))
    .filter((p) => p.e.cell);
  const save = () => {
    const out = X.write(wb, { bookType: file.format === 'xls' ? 'biff8' : 'xlsx', type: 'array' });
    download(out, generated ? file.name : editedName(file.name),
      'application/octet-stream');
    setDirty(false);
  };
  const cols = Math.min(grid.range.e.c - grid.range.s.c + 1, MAX_COLS);

  return (
    <>
      <EditList
        edits={proposals.map(({ e, i, ref }) => {
          const sheetName = ref?.sheet ?? grid.name;
          const ws = wb.Sheets[sheetName];
          // 현재 값을 함께 보인다 — 모델이 짚은 칸이 맞는지는 사람이 이것으로 확인한다.
          const now = ref && ws ? X.utils.format_cell(ws[ref.cell]) || '(빈 칸)' : null;
          return {
            label: ref && ws
              ? <><span className="mono">{sheetName}!{ref.cell}</span>: <s>{now}</s> → <b>{e.value}</b></>
              : <>{e.cell} — {ref ? `'${ref.sheet}' 시트가 없습니다` : '칸 주소를 읽지 못했습니다'}</>,
            why: e.why,
            onApply: ref && ws ? () => {
              setCell(sheetName, ref.cell, e.value ?? '');
              setApplied((cur) => ({ ...cur, [i]: '적용함' }));
            } : undefined,
            status: applied[i],
          };
        })} />
      <div className="row" style={{ marginBottom: 6, flexWrap: 'wrap' }}>
        {wb.SheetNames.map((n, i) => (
          <button key={n} className={i === sheetIndex ? 'small' : 'small secondary'}
            onClick={() => { commit(); setSheetIndex(i); }}>{n}</button>
        ))}
        <span className="spacer" />
        {dirty && <span className="mutedtext" style={{ fontSize: 12 }}>저장하지 않은 수정이 있습니다</span>}
        <button className="small" onClick={() => { commit(); save(); }}>
          {generated ? '내려받기' : '수정본 내려받기'}
        </button>
      </div>
      {!generated && (
        <p className="mutedtext" style={{ fontSize: 11, margin: '0 0 6px' }}>
          값과 표시 형식·병합·열 너비는 남지만, 글꼴·색·테두리·차트·그림은 내려받은 사본에 남지
          않습니다 — 그래서 원본을 덮지 않고 「(수정)」 사본으로 받습니다.
        </p>
      )}
      <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 280px)' }}>
        <table className="sheet-grid">
          <thead>
            <tr>
              <th />
              {Array.from({ length: cols }, (_, j) => (
                <th key={j} className="mono">{X.utils.encode_col(grid.range.s.c + j)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.slice(0, MAX_ROWS).map((r, i) => (
              <tr key={i}>
                <th className="mono">{grid.range.s.r + i + 1}</th>
                {Array.from({ length: cols }, (_, j) => {
                  const addr = X.utils.encode_cell({ r: grid.range.s.r + i, c: grid.range.s.c + j });
                  return editing?.addr === addr ? (
                    <td key={j}>
                      <input autoFocus value={editing.value} style={{ width: '100%', minWidth: 80 }}
                        onChange={(e) => setEditing({ addr, value: e.target.value })}
                        onBlur={commit}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') commit();
                          if (e.key === 'Escape') setEditing(null);
                        }} />
                    </td>
                  ) : (
                    <td key={j} title={addr} style={{ cursor: 'text' }}
                      onClick={() => {
                        const cell = wb.Sheets[grid.name][addr];
                        // 수식 칸은 수식이 아니라 값으로 고친다 — 고치면 그 칸의 수식은 사라진다.
                        setEditing({ addr, value: cell ? String(cell.v ?? '') : '' });
                      }}>
                      {r[j] ?? ''}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        {(grid.rows.length > MAX_ROWS || grid.range.e.c - grid.range.s.c + 1 > MAX_COLS) && (
          <p className="mutedtext">
            앞 {MAX_ROWS}행·{MAX_COLS}열만 보입니다(내려받는 사본에는 전부 남습니다).
          </p>
        )}
      </div>
    </>
  );
}

// --- 보기 전용(doc·docx·ppt·pptx) ---

function OfficePreview({ name, blob }: { name: string; blob: File }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let made: string | null = null;
    // 서버의 LibreOffice가 PDF로 바꾼다 — 원본은 그 요청 동안만 서버에 있다(api/smartwork.py).
    api.officePreview(name, blob).then((pdf) => {
      if (!alive) return;
      made = URL.createObjectURL(pdf);
      setUrl(made);
    }, (e: Error) => { if (alive) setError(e.message); });
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [name, blob]);

  if (error) return <p className="error">미리 보지 못했습니다: {error}</p>;
  if (!url) return <p className="mutedtext">PDF로 바꾸는 중... (큰 문서는 수십 초 걸립니다)</p>;
  return (
    <>
      <p className="mutedtext" style={{ fontSize: 11, margin: '0 0 6px' }}>
        보기 전용입니다 — 고치려면 PC의 원래 프로그램에서 여세요.
      </p>
      <iframe title={name} src={url} className="smartwork-frame" />
    </>
  );
}

// --- 수정안 ---

function EditList({ edits }: {
  edits: { label: ReactNode; why: string; onApply?: () => void; status?: string }[];
}) {
  if (edits.length === 0) return null;
  return (
    <div style={{ borderBottom: '1px solid var(--border-soft)', paddingBottom: 8, marginBottom: 8 }}>
      <div className="mutedtext" style={{ fontSize: 12, marginBottom: 4 }}>
        대화의 수정안 {edits.length}건 — 확인하고 「적용」하면 아래에 들어갑니다(저장은 내려받기).
      </div>
      {edits.map((e, i) => (
        <div key={i} className="row" style={{ gap: 8, fontSize: 12, marginBottom: 4 }}>
          <span style={{ flex: 1 }}>
            {e.label}
            {e.why && <span className="mutedtext"> · {e.why}</span>}
          </span>
          {e.status ? <span className="status dim">{e.status}</span> : e.onApply && (
            <button className="small secondary" onClick={e.onApply}>적용</button>
          )}
        </div>
      ))}
    </div>
  );
}
