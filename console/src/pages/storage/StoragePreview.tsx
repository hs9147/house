import { useEffect, useState } from 'react';
import type { WorkBook } from 'xlsx';
import { api } from '../../lib/api';
import { decodeText } from '../../lib/localfiles';
import { renderMarkdown } from '../../lib/markdown';
import type { StorageFile } from '../../lib/types';

// 형식마다 그리는 방법 — 브라우저가 직접 그릴 수 있으면 원본을 받아 그리고(그림·PDF·글·표),
// 못 그리면 서버가 바꿔 준다(워드·파워포인트 → PDF, 한글 등 → 본문 텍스트).
const IMAGE = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;
const PDF = /\.pdf$/i;
const SHEET = /\.(xlsx|xls|csv)$/i;
const OFFICE = /\.(docx?|pptx?)$/i;
const MARKDOWN = /\.(md|markdown)$/i;
const TEXT = /\.(txt|md|markdown|json|log|py|js|jsx|ts|tsx|java|c|cpp|h|cs|go|rs|sql|xml|ya?ml|ini|cfg|conf|toml|sh|ps1|bat|html?|css)$/i;
type Kind = 'image' | 'pdf' | 'sheet' | 'office' | 'text' | 'extract';

// 원본을 받아 브라우저에서 그리는 상한 — 넘으면 내려받기만 둔다(큰 파일을 미리보기로 받지 않게).
const MAX_INLINE = 20 * 1024 * 1024;
const MAX_ROWS = 300;
const MAX_COLS = 40;

export function kindOf(path: string): Kind {
  if (IMAGE.test(path)) return 'image';
  if (PDF.test(path)) return 'pdf';
  if (SHEET.test(path)) return 'sheet';
  if (OFFICE.test(path)) return 'office';
  if (TEXT.test(path)) return 'text';
  return 'extract';
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * 고른 파일 하나의 미리보기 — 보기 전용이다(고치려면 내려받아 원래 프로그램에서 연다).
 * 「본문 텍스트」는 어느 형식이든 서버가 추출한 글로 본다(검색 색인과 같은 추출이다).
 */
export default function StoragePreview({ store, file }: { store: string; file: StorageFile | null }) {
  const [asText, setAsText] = useState(false);
  useEffect(() => setAsText(false), [store, file?.path]);

  if (!file) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>미리보기</h3>
        <p className="mutedtext" style={{ fontSize: 12 }}>왼쪽 목록에서 파일을 고르면 여기에 보입니다.</p>
      </div>
    );
  }
  const name = file.path.split('/').pop() ?? file.path;
  const kind = kindOf(file.path);
  const download = async () => save(await api.storageFileBlob(store, file.path), name);

  return (
    <div className="panel">
      <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 6 }}>
        <h3 style={{ margin: 0, overflowWrap: 'anywhere' }}>{name}</h3>
        <span className="spacer" />
        {kind !== 'extract' && (
          <button className={asText ? 'small' : 'small secondary'} onClick={() => setAsText((v) => !v)}>
            본문 텍스트
          </button>
        )}
        <button className="small secondary" onClick={() => void download()}>내려받기</button>
      </div>
      <div className="mono mutedtext" style={{ fontSize: 11, marginBottom: 8, overflowWrap: 'anywhere' }}>
        {store}/{file.path} · {fmtSize(file.size)}
        {file.mtime ? ` · ${new Date(file.mtime * 1000).toLocaleString()}` : ''}
      </div>
      {asText || kind === 'extract' ? <ExtractedText store={store} path={file.path} />
        : kind === 'office' ? <PdfFrame load={() => api.storagePreviewPdf(store, file.path)} keyOf={`${store}/${file.path}`}
            note="서버가 PDF로 바꿔 보여 줍니다(큰 문서는 수십 초 걸립니다)."
            fallback={<ExtractedText store={store} path={file.path} />} />
        : file.size > MAX_INLINE ? (
          <p className="mutedtext" style={{ fontSize: 12 }}>
            {fmtSize(MAX_INLINE)}보다 커서 미리 보지 않습니다 — 내려받거나 「본문 텍스트」로 보세요.
          </p>
        )
        : kind === 'pdf' ? <PdfFrame load={() => api.storageFileBlob(store, file.path)} keyOf={`${store}/${file.path}`}
            fallback={<ExtractedText store={store} path={file.path} />} />
        : <Inline store={store} path={file.path} kind={kind} />}
    </div>
  );
}

/** 원본을 받아 그리는 형식 — 그림·글·표. */
function Inline({ store, path, kind }: { store: string; path: string; kind: Kind }) {
  const [blob, setBlob] = useState<Blob | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    setBlob(null);
    setError('');
    api.storageFileBlob(store, path).then((b) => { if (alive) setBlob(b); },
      (e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [store, path]);
  if (error) return <p className="error">열지 못했습니다: {error}</p>;
  if (!blob) return <p className="mutedtext">여는 중...</p>;
  if (kind === 'image') return <ImageView blob={blob} />;
  if (kind === 'sheet') return <SheetView blob={blob} csv={/\.csv$/i.test(path)} />;
  return <TextView blob={blob} markdown={MARKDOWN.test(path)} />;
}

function ImageView({ blob }: { blob: Blob }) {
  // 주소는 효과 안에서 만들고 지운다 — 렌더 중에 만들면 개발 모드의 두 번 실행에서 지운 주소가 남는다.
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const made = URL.createObjectURL(blob);
    setUrl(made);
    return () => URL.revokeObjectURL(made);
  }, [blob]);
  return url ? <img src={url} alt="" style={{ maxWidth: '100%', maxHeight: '70vh', display: 'block' }} /> : null;
}

/** 글 — UTF-8이 깨지면 EUC-KR로(한글 윈도우의 메모장·엑셀이 저장한 파일). 마크다운은 그려서. */
function TextView({ blob, markdown }: { blob: Blob; markdown: boolean }) {
  const [text, setText] = useState<{ text: string; legacy: boolean } | null>(null);
  useEffect(() => {
    let alive = true;
    blob.arrayBuffer().then((buf) => { if (alive) setText(decodeText(buf)); });
    return () => { alive = false; };
  }, [blob]);
  if (!text) return <p className="mutedtext">여는 중...</p>;
  return (
    <>
      {text.legacy && <p className="mutedtext" style={{ fontSize: 11, margin: '0 0 4px' }}>EUC-KR로 읽었습니다.</p>}
      {markdown
        ? <div className="report-md" style={{ maxHeight: '70vh', overflow: 'auto' }}
            dangerouslySetInnerHTML={{ __html: renderMarkdown(text.text) }} />
        : <pre className="mono" style={{ fontSize: 12, maxHeight: '70vh', overflow: 'auto', whiteSpace: 'pre-wrap' }}>
            {text.text}
          </pre>}
    </>
  );
}

/** 표 — 보기 전용. 시트를 고르고 앞 MAX_ROWS행·MAX_COLS열만 그린다. */
function SheetView({ blob, csv }: { blob: Blob; csv: boolean }) {
  const [book, setBook] = useState<{ X: typeof import('xlsx'); wb: WorkBook } | null>(null);
  const [error, setError] = useState('');
  const [at, setAt] = useState(0);
  useEffect(() => {
    let alive = true;
    (async () => {
      // 엑셀 라이브러리는 표를 열 때만 받는다 — 콘솔 첫 화면이 그만큼 무거워지지 않게.
      const X = await import('xlsx');
      const buf = await blob.arrayBuffer();
      const wb = csv ? X.read(decodeText(buf).text, { type: 'string' }) : X.read(buf, { type: 'array' });
      if (alive) setBook({ X, wb });
    })().catch((e: Error) => { if (alive) setError(`열지 못했습니다: ${e.message}`); });
    return () => { alive = false; };
  }, [blob, csv]);
  if (error) return <p className="error">{error}</p>;
  if (!book) return <p className="mutedtext">여는 중...</p>;
  const { X, wb } = book;
  const name = wb.SheetNames[Math.min(at, wb.SheetNames.length - 1)];
  const rows = X.utils.sheet_to_json<string[]>(wb.Sheets[name], { header: 1, raw: false, defval: '' });
  return (
    <>
      {wb.SheetNames.length > 1 && (
        <div className="row" style={{ gap: 4, flexWrap: 'wrap', marginBottom: 6 }}>
          {wb.SheetNames.map((s, i) => (
            <button key={s} className={i === at ? 'small' : 'small secondary'} onClick={() => setAt(i)}>{s}</button>
          ))}
        </div>
      )}
      <div style={{ maxHeight: '70vh', overflow: 'auto' }}>
        <table style={{ fontSize: 12 }}>
          <tbody>
            {rows.slice(0, MAX_ROWS).map((r, i) => (
              <tr key={i}>{r.slice(0, MAX_COLS).map((v, j) => <td key={j}>{v}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {(rows.length > MAX_ROWS || rows.some((r) => r.length > MAX_COLS)) && (
        <p className="mutedtext" style={{ fontSize: 11 }}>
          앞 {MAX_ROWS}행·{MAX_COLS}열만 보입니다(전체 {rows.length}행) — 전부 보려면 내려받으세요.
        </p>
      )}
    </>
  );
}

/** PDF — 브라우저 내장 뷰어가 그린다. 못 그리면(서버에 LibreOffice가 없는 등) 본문 텍스트로 대신 보인다. */
function PdfFrame({ load, keyOf, note, fallback }: {
  load: () => Promise<Blob>; keyOf: string; note?: string; fallback?: React.ReactNode;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    let made: string | null = null;
    setUrl(null);
    setError('');
    load().then((b) => {
      if (!alive) return;
      made = URL.createObjectURL(new Blob([b], { type: 'application/pdf' }));
      setUrl(made);
    }, (e: Error) => { if (alive) setError(e.message); });
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyOf]);
  if (error) {
    return (
      <>
        <p className="mutedtext" style={{ fontSize: 11, margin: '0 0 6px' }}>
          모양 그대로는 미리 보지 못해 본문 텍스트로 보입니다 — {error}
        </p>
        {fallback}
      </>
    );
  }
  if (!url) return <p className="mutedtext">{note ?? '여는 중...'}</p>;
  return <iframe title={keyOf} src={url} style={{ width: '100%', height: '70vh', border: 0, background: '#fff' }} />;
}

/** 본문 텍스트 — 서버가 추출한 마크다운(검색 색인과 같은 추출). */
function ExtractedText({ store, path }: { store: string; path: string }) {
  const [out, setOut] = useState<{ text: string; truncated: boolean } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    setOut(null);
    setError('');
    api.storageFileText(store, path).then((r) => { if (alive) setOut(r); },
      (e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [store, path]);
  if (error) return <p className="error">본문을 읽지 못했습니다: {error} — 내려받아 원래 프로그램에서 여세요.</p>;
  if (!out) return <p className="mutedtext">본문을 추출하는 중...</p>;
  return (
    <>
      <p className="mutedtext" style={{ fontSize: 11, margin: '0 0 4px' }}>
        서버가 추출한 본문입니다(모양은 원본과 다릅니다).{out.truncated && ' 길어서 앞부분만 보입니다.'}
      </p>
      {out.text.trim()
        ? <div className="report-md" style={{ maxHeight: '70vh', overflow: 'auto' }}
            dangerouslySetInnerHTML={{ __html: renderMarkdown(out.text) }} />
        : <p className="mutedtext">추출된 본문이 없습니다(그림만 있는 문서일 수 있습니다).</p>}
    </>
  );
}
