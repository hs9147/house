// 북마크 수집 e2e — 가짜 업무 사이트와 가짜 콘솔을 띄우고, 헤드리스 Chrome(CDP)에서 북마크를 실제로 돌린다.
//
//   node e2e/scanAgent.e2e.mjs            (console 폴더에서. Chrome·Edge 중 설치된 것을 쓴다)
//
// 가짜 사이트는 흔한 사내 조회 화면의 모양이다: 날짜 칸 둘(등록일 ~), 부서 고르기, 「조회」 버튼이 XHR POST
// (폼 본문 pageIndex·pageUnit·조건, CSRF 머리 필수)로 목록을 받아 표에 10행만 그린다. 서버는 쪽 크기를 100으로
// 자른다. 콘솔은 북마크를 연 창(opener)이 되어 수집 조건을 보내고 결과를 받는다(scanAgent.js 「통로」).
// 보는 것: 화면에 보인 10행이 아니라 조건에 맞는 목록 전체를 받는다 · @last면 그날부터만 · 다 받았다고 센다.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const strip = (code) => code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\n\s+/g, '\n').trim().replace(/;$/, '');
const scanAgent = strip(readFileSync(join(here, '../src/lib/scanAgent.js'), 'utf8'));
const scanData = strip(readFileSync(join(here, '../src/lib/scanData.js'), 'utf8'));

// ---------------------------------------------------------------- 가짜 업무 사이트
const DEPTS = ['구매1팀', '구매2팀', '구매3팀'];
const day = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return d.toISOString().slice(0, 10); };
const ALL = Array.from({ length: 700 }, (_, i) => ({
  reqNo: `PR-${String(i + 1).padStart(5, '0')}`, title: `구매요청 ${i + 1}`, dept: DEPTS[i % 3],
  amount: (i + 1) * 1500, regDate: day(i * 1.5 | 0), // 0일 전 ~ 약 3년 전
}));
const stats = { list: 0, noToken: 0, sizes: new Set() };

const PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>구매요청 조회</title></head><body>
<h2>구매요청 조회</h2>
<table class="search"><tr><td>등록일</td><td><input id="from" value="2026.01.01"> ~ <input id="to" value="2026.01.31"></td>
<td>부서</td><td><select id="dept"><option value="">전체</option>${DEPTS.map((d) => `<option>${d}</option>`).join('')}</select></td>
<td><button type="button" onclick="search(1)">조 회</button></td></tr></table>
<div id="count"></div>
<table id="grid"><thead><tr><th>요청번호</th><th>제목</th><th>부서</th><th>금액</th><th>등록일</th></tr></thead><tbody></tbody></table>
<script>
var TOKEN = 'csrf-' + Math.random().toString(36).slice(2);
document.cookie = 'tok=' + TOKEN;
function search(page) {
  var x = new XMLHttpRequest();
  x.open('POST', '/pr/selectList.do');
  x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8');
  x.setRequestHeader('X-CSRF-TOKEN', TOKEN);
  x.onload = function () {
    var r = JSON.parse(x.responseText).result;
    document.getElementById('count').textContent = '총 ' + r.totalCnt.toLocaleString() + '건';
    document.querySelector('#grid tbody').innerHTML = r.list.map(function (v) {
      return '<tr><td>' + v.reqNo + '</td><td>' + v.title + '</td><td>' + v.dept + '</td><td>' +
        v.amount.toLocaleString() + '</td><td>' + v.regDate.replace(/-/g, '.') + '</td></tr>';
    }).join('');
  };
  x.send('searchVO.pageIndex=' + page + '&searchVO.pageUnit=10&fromDt=' + document.getElementById('from').value +
         '&toDt=' + document.getElementById('to').value + '&dept=' + encodeURIComponent(document.getElementById('dept').value));
}
</script></body></html>`;

function site(req, res) {
  if (req.url.startsWith('/screen')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    return res.end(PAGE);
  }
  if (req.url === '/pr/selectList.do' && req.method === 'POST') {
    let body = '';
    req.on('data', (c) => { body += c; });
    return req.on('end', () => {
      const tok = /tok=([^;]+)/.exec(req.headers.cookie || '');
      if (!tok || req.headers['x-csrf-token'] !== tok[1]) {
        stats.noToken++;
        res.writeHead(403);
        return res.end('csrf');
      }
      stats.list++;
      const q = new URLSearchParams(body);
      const page = +q.get('searchVO.pageIndex') || 1, size = Math.min(+q.get('searchVO.pageUnit') || 10, 100);
      stats.sizes.add(size);
      const iso = (v) => String(v || '').replace(/\D/g, '').replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3');
      const from = iso(q.get('fromDt')), to = iso(q.get('toDt')), dept = q.get('dept') || '';
      const hit = ALL.filter((r) => (!from || r.regDate >= from) && (!to || r.regDate <= to) && (!dept || r.dept === dept));
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
      return res.end(JSON.stringify({ status: 'OK', result: { totalCnt: hit.length,
        list: hit.slice((page - 1) * size, page * size) } }));
    });
  }
  res.writeHead(404);
  res.end();
}

// ---------------------------------------------------------------- 가짜 콘솔(북마크를 연 창)
const CONSOLE = `<!doctype html><html><body><script>
window.__logs = []; window.__result = null;
window.openSite = function (url, start) {
  var w = window.open(url, 'site');
  window.addEventListener('message', function (e) {
    var m = e.data;
    if (!m || e.source !== w) return;
    if (m.gpax === 'hello') w.postMessage(Object.assign({ gpax: 'start', nonce: 'n1' }, start), '*');
    if (m.gpax === 'log') window.__logs.push(m.text);
    if (m.gpax === 'result') window.__result = m.payload;
  });
  return true;
};
</script></body></html>`;

function serve(handler) {
  return new Promise((ok) => {
    const s = http.createServer(handler).listen(0, '127.0.0.1', () => ok(s));
  });
}

// ---------------------------------------------------------------- CDP
function chromePath() {
  const list = [process.env.CHROME, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome', '/usr/bin/chromium'];
  const hit = list.find((p) => p && existsSync(p));
  if (!hit) throw new Error('Chrome·Edge를 찾지 못했습니다 — CHROME 환경변수로 경로를 주세요');
  return hit;
}
async function launch() {
  const profile = mkdtempSync(join(tmpdir(), 'gpax-e2e-'));
  const proc = spawn(chromePath(), ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-popup-blocking', 'about:blank'],
  { stdio: ['ignore', 'ignore', 'pipe'] });
  const ws = await new Promise((ok, fail) => {
    let err = '';
    proc.stderr.on('data', (c) => {
      err += c;
      const m = /DevTools listening on (ws:\/\/\S+)/.exec(err);
      if (m) ok(m[1]);
    });
    setTimeout(() => fail(new Error('Chrome이 뜨지 않았습니다: ' + err.slice(0, 300))), 20000);
  });
  const sock = new WebSocket(ws);
  await new Promise((ok) => sock.addEventListener('open', ok, { once: true }));
  let id = 0;
  const waiting = new Map();
  sock.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && waiting.has(m.id)) {
      const { ok, fail } = waiting.get(m.id);
      waiting.delete(m.id);
      if (m.error) fail(new Error(m.error.message)); else ok(m.result);
    }
  });
  const send = (method, params = {}, sessionId) => new Promise((ok, fail) => {
    const n = ++id;
    waiting.set(n, { ok, fail });
    sock.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const close = () => {
    sock.close();
    proc.kill();
    setTimeout(() => rmSync(profile, { recursive: true, force: true }), 500);
  };
  return { send, close };
}
async function evaluate(cdp, session, expression) {
  const r = await cdp.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }, session);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

async function run(cdp, siteBase, consoleBase, screen) {
  const { targetId } = await cdp.send('Target.createTarget', { url: consoleBase + '/console' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  for (let i = 0; i < 50 && !(await evaluate(cdp, sessionId, "typeof openSite === 'function'")); i++) await sleep(100);
  const start = { mode: 'replay', source: { id: 7, name: '가짜 ERP', origin: siteBase, queries: [screen], version: '' } };
  await evaluate(cdp, sessionId, `openSite(${JSON.stringify(siteBase + '/screen')}, ${JSON.stringify(start)})`);
  let site = null;
  for (let i = 0; i < 50 && !site; i++) {
    await sleep(200);
    site = (await cdp.send('Target.getTargets')).targetInfos.find((t) => t.type === 'page' && t.url.startsWith(siteBase));
  }
  if (!site) throw new Error('사이트 창이 열리지 않았습니다');
  const s2 = (await cdp.send('Target.attachToTarget', { targetId: site.targetId, flatten: true })).sessionId;
  for (let i = 0; i < 50 && !(await evaluate(cdp, s2, "document.readyState === 'complete' && !!window.search")); i++) await sleep(100);
  const code = `(${scanAgent})(${JSON.stringify({ console: consoleBase, receive: consoleBase + '/#/agent' })}, ${scanData})`;
  await evaluate(cdp, s2, code + '; true');
  for (let i = 0; i < 300; i++) {
    await sleep(300);
    const out = await evaluate(cdp, sessionId, 'JSON.stringify({ result: window.__result, logs: window.__logs })');
    const got = JSON.parse(out);
    if (got.result) {
      await cdp.send('Target.closeTarget', { targetId: site.targetId });
      await cdp.send('Target.closeTarget', { targetId });
      return got;
    }
  }
  throw new Error('결과가 오지 않았습니다 — ' + JSON.parse(await evaluate(cdp, sessionId, 'JSON.stringify(window.__logs)')).slice(-8).join(' | '));
}

// ---------------------------------------------------------------- 시험
const failures = [];
function check(name, cond, detail) {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${cond ? '' : ' — ' + detail}`);
  if (!cond) failures.push(name);
}

const siteServer = await serve(site);
const consoleServer = await serve((req, res) => { res.writeHead(200, { 'content-type': 'text/html' }); res.end(CONSOLE); });
// 사이트와 콘솔은 출처가 달라야 한다(실제로도 다르다) — 같은 127.0.0.1의 다른 포트
const siteBase = `http://127.0.0.1:${siteServer.address().port}`;
const consoleBase = `http://localhost:${consoleServer.address().port}`;
const cdp = await launch();
try {
  const conditions = [{ field: '등록일', value: '@last|@today-3y' }, { field: '등록일', value: '@today' },
    { field: '부서', value: '구매3팀' }];
  const want = (from) => ALL.filter((r) => r.regDate >= from && r.dept === '구매3팀');

  // 1) 처음 — 3년 전부터 전체
  const full = await run(cdp, siteBase, consoleBase, { screen: '구매요청 조회', url: siteBase + '/screen', conditions });
  const ds = full.result.datasets[0] || {};
  const expect = want(day(365 * 3 + (new Date().getMonth() === 1 && new Date().getDate() === 29 ? 1 : 0)));
  console.log(`     (목록 요청 ${stats.list}번 · 쪽 크기 ${[...stats.sizes].join(',')} · 기대 ${expect.length}건)`);
  check('화면의 10행이 아니라 조건에 맞는 목록 전체를 받는다', expect.length > 100 && ds.records?.length === expect.length,
    `받음 ${ds.records?.length} / 기대 ${expect.length} · ${full.logs.slice(-6).join(' | ')}`);
  check('전체 건수로 빠짐없음을 센다', ds.complete === true && ds.total === expect.length, JSON.stringify(full.result.collected[0]?.data));
  check('키는 요청번호', ds.key === 'reqNo', ds.key);
  check('열 이름은 화면 머리글', JSON.stringify(ds.columns?.slice(0, 2)) ===
    JSON.stringify([{ key: 'reqNo', label: '요청번호' }, { key: 'title', label: '제목' }]), JSON.stringify(ds.columns));
  check('CSRF 머리를 그대로 실어 다시 보낸다', stats.noToken === 0, `거절 ${stats.noToken}번`);
  check('쪽 크기를 키워 받는다(서버 상한 100)', stats.sizes.has(100), [...stats.sizes].join(','));
  check('다 받았으면 엑셀·표 파일은 받지 않는다', full.result.files.length === 0, full.result.files.map((f) => f.name).join(','));
  check('요청 머리·본문 값은 결과에 없다', !JSON.stringify(full.result).includes('csrf-'), 'CSRF 값이 실렸다');

  // 2) 증분 — 지난 수집일(last)부터만
  const last = day(30);
  const inc = await run(cdp, siteBase, consoleBase, { screen: '구매요청 조회', url: siteBase + '/screen', conditions, last });
  const ids = (inc.result.datasets[0]?.records || []).map((r) => r.reqNo).sort();
  check('@last면 그날부터만 받는다', ids.length > 0 && ids.length < expect.length && JSON.stringify(ids) === JSON.stringify(want(last).map((r) => r.reqNo).sort()),
    `받음 ${ids.length} / 기대 ${want(last).length}`);
  check('증분의 시작일이 결과에 실린다', inc.result.datasets[0]?.since === last, inc.result.datasets[0]?.since);
} finally {
  cdp.close();
  siteServer.close();
  consoleServer.close();
}
console.log(failures.length ? `\n실패 ${failures.length}개` : '\n모두 통과');
process.exit(failures.length ? 1 : 0);
