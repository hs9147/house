import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import GpaxBookmark from '../components/GpaxBookmark';
import { api } from '../lib/api';
import type { SkillRunResult, SourceQueryScreen } from '../lib/types';
import { SkillResult } from './SourceSkills';

type Prepared = Awaited<ReturnType<typeof api.prepareSkill>>;

// 창에 보내는 표 — http 콘솔에서는 crypto.randomUUID가 없다(보안 맥락에서만).
const nonceOf = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');

/**
 * 사람의 브라우저에서 스킬 실행 — 서버 브라우저가 그 사이트에 로그인돼 있지 않을 때(skills.LoginRequired) 에이전트·스킬 탭이
 * 이 주소(#/skill-run?skill=…&params=…)로 사람에게 로그인을 요청한다. 업무 사이트를 새 탭으로 열고, 사람이 로그인한 뒤 GPAX
 * 북마크를 누르면 북마크가 이 창에 인사한다 — 이 창이 입력을 채운 화면을 봇 실행(replay)으로 보내고, 결과를 서버에 올린다.
 * 서버는 같은 입력의 결과로 보관한다(에이전트가 다시 부르면 받는다). 세션은 사람의 브라우저 밖으로 나가지 않는다.
 */
export default function SkillRun() {
  const [search] = useSearchParams();
  const skill = search.get('skill') ?? '';
  const params = (() => {
    try { return JSON.parse(search.get('params') || '{}') as Record<string, string>; } catch { return {}; }
  })();
  const [prep, setPrep] = useState<Prepared | null>(null);
  const [state, setState] = useState<'' | 'waiting' | 'linked' | 'uploading' | 'done'>('');
  const [lines, setLines] = useState<string[]>([]);
  const [out, setOut] = useState<SkillRunResult | null>(null);
  const [error, setError] = useState('');
  const win = useRef<Window | null>(null);
  const nonce = useRef('');
  const push = (t: string) => setLines((prev) => [...prev, `${new Date().toTimeString().slice(0, 8)} ${t}`].slice(-200));

  useEffect(() => {
    if (!skill) return;
    api.prepareSkill(skill, params).then(setPrep, (e) => setError((e as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skill]);

  useEffect(() => {
    if (!prep) return;
    const site = prep.source.origin;
    const onMessage = (e: MessageEvent) => {
      const m = e.data;
      if (!m || typeof m !== 'object' || e.origin !== site || !win.current || e.source !== win.current) return;
      if (m.gpax === 'hello') {
        nonce.current = nonceOf();
        const screen: SourceQueryScreen = prep.screen;
        win.current.postMessage({
          gpax: 'start', nonce: nonce.current, mode: 'replay',
          source: { id: prep.source.id, name: prep.source.name, origin: site, queries: [screen], version: prep.version },
          queries: [screen], version: prep.version,
        }, site);
        setState('linked');
        push('북마크와 연결했습니다 — 로그인한 세션으로 조회합니다');
        return;
      }
      if (!nonce.current || m.nonce !== nonce.current) return;
      if (m.gpax === 'log' && typeof m.text === 'string') push(m.text.slice(0, 300));
      if (m.gpax === 'result' && m.kind === 'scan') {
        setState('uploading');
        const file = new File([JSON.stringify(m.payload)], 'gpax-skill.json', { type: 'application/json' });
        api.skillBrowserResult(prep.skill, prep.params, file).then(
          (r) => { setOut(r); setState('done'); push('결과를 올렸습니다 — 에이전트가 같은 입력으로 다시 부르면 이 결과를 받습니다'); },
          (err) => { setError((err as Error).message); setState('linked'); });
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [prep]);

  const open = () => {
    if (!prep) return;
    setError('');
    setLines([]);
    const w = window.open(prep.source.url, `gpax-skill-${prep.source.id}`);
    if (!w) {
      setError('새 탭이 막혔습니다 — 브라우저의 팝업 차단을 이 콘솔에 대해 풀어 주세요.');
      return;
    }
    win.current = w;
    setState('waiting');
    push(`${prep.source.url}을 새 탭으로 열었습니다 — 로그인한 뒤 그 탭에서 GPAX 북마크를 누르세요`);
  };

  if (!skill) return <div className="panel"><p className="error">실행할 스킬이 없습니다(주소에 skill=…).</p></div>;
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>🔐 로그인이 필요한 스킬 — 내 브라우저에서 실행</h2>
      <p style={{ fontSize: 13 }}>
        <b className="mono">{skill}</b>{prep && ` — ${prep.description}`}
      </p>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        서버는 이 사이트에 로그인할 수 없어서(사내 SSO) 내 브라우저에서 돌립니다. ① 아래 버튼으로 업무 사이트를 열고 로그인 →
        ② 그 탭에서 <GpaxBookmark /> 북마크를 한 번 누르면 → 북마크가 이 스킬 화면을 입력대로 조회해 결과를 여기로 보냅니다.
        세션은 내 브라우저 밖으로 나가지 않습니다. 북마크가 없으면 그 버튼을 북마크바로 끌어다 놓으세요.
      </p>
      {prep && Object.keys(prep.params).length > 0 && (
        <p className="mono mutedtext" style={{ fontSize: 11 }}>
          입력: {Object.entries(prep.params).map(([k, v]) => `${k}=${v || '(빈 값)'}`).join(', ')}
        </p>
      )}
      <button disabled={!prep || state === 'waiting' || state === 'linked' || state === 'uploading'} onClick={open}>
        {state === 'done' ? '다시 실행' : '① 업무 사이트 열기(로그인)'}
      </button>
      {state && <span className="mutedtext" style={{ marginLeft: 8, fontSize: 12 }}>
        {{ waiting: '탭에서 로그인하고 북마크를 누르기를 기다립니다…', linked: '조회 중…', uploading: '올리는 중…', done: '끝' }[state]}
      </span>}
      {error && <p className="error">{error}</p>}
      {lines.length > 0 && (
        <pre className="mono" style={{ fontSize: 11, maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{lines.join('\n')}</pre>
      )}
      {out && <SkillResult out={out} />}
    </div>
  );
}
