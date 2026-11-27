#!/usr/bin/env node
// GPAX MCP 로그인 도우미 — 토큰을 복사해 붙여 넣는 대신 GPAX 계정으로 로그인해 MCP에 붙는다.
//
//   node paas-mcp.mjs login     브라우저에 GPAX 로그인·승인 화면을 열고, 갱신 토큰을 이 PC에 저장한다
//   node paas-mcp.mjs install   Claude Code에 MCP 서버(paas)를 등록한다 — 헤더는 이 도우미가 만든다
//   node paas-mcp.mjs headers   (Claude Code가 부른다) {"Authorization": "Bearer …"} 한 줄을 낸다
//   node paas-mcp.mjs status    누구로 로그인돼 있는지·언제 만료되는지
//   node paas-mcp.mjs logout    연결을 끊고(서버에서도 폐기) 저장한 값을 지운다
//
// 왜 도우미인가: MCP 클라이언트가 스스로 OAuth를 하면 토큰 주소가 https여야 한다(Claude Code는 http면 거부한다).
// 이 플랫폼은 아직 http라, 로그인·갱신은 이 도우미가 하고 Claude Code에는 헤더만 건넨다(headersHelper).
// 요청마다 실리는 값은 1시간짜리 액세스 토큰이고, 갱신 토큰은 쓸 때마다 바뀐다. 콘솔 「MCP 연결설정 → 승인된 앱」
// 에서 끊으면 다음 연결부터 막힌다.
//
// 필요한 것: Node 18 이상(내장 fetch). 저장 위치: ~/.paas-mcp/ (PAAS_MCP_HOME으로 바꿀 수 있다).
import { spawnSync, spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = (process.env.PAAS_MCP_BASE || '__PAAS_BASE__').replace(/\/+$/, '');
const API = `${BASE}/paas/api/v1`;
const HUB = `${API}/mcp/hub`;
const SELF = fileURLToPath(import.meta.url);
const DIR = process.env.PAAS_MCP_HOME || path.join(os.homedir(), '.paas-mcp');
const HOST = (() => { try { return new URL(BASE).host; } catch { return 'unset'; } })();
const STATE = path.join(DIR, `${HOST.replace(/[^\w.-]/g, '_')}.json`);
const LOCK = `${STATE}.lock`;
const EARLY_SECONDS = 5 * 60; // 만료 5분 전이면 미리 갱신한다
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;

const slash = (p) => p.replace(/\\/g, '/');
const helperCommand = () => {
  // Claude Code는 이 문자열을 셸로 실행한다(Windows에서는 어느 셸인지 정해져 있지 않다). 그래서 첫 낱말은 따옴표 없이
  // 두고(cmd는 첫 글자가 따옴표면 바깥 따옴표를 벗긴다), 경로는 슬래시로 쓴다 — cmd·bash·PowerShell 모두 그대로 읽는다.
  const node = /\s/.test(process.execPath) ? 'node' : slash(process.execPath);
  return `${node} "${slash(SELF)}" headers`;
};
const loginHint = () => `node "${slash(SELF)}" login`;

function fail(message) {
  process.stderr.write(`[paas-mcp] ${message}\n`);
  process.exit(1);
}

const b64url = (buf) => Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE, 'utf8'));
  } catch {
    return null;
  }
}

function writeState(state) {
  fs.mkdirSync(DIR, { recursive: true });
  const tmp = `${STATE}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, STATE); // 반쯤 쓴 파일을 다른 창이 읽지 않게
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 여러 Claude Code 창이 동시에 연결하면 같은 갱신 토큰으로 동시에 갱신하게 된다. 갱신 토큰은 쓸 때마다 바뀌므로
// 둘째는 실패한다 — 잠금으로 한 줄로 세우고, 잠금을 얻은 뒤 다시 읽어 이미 갱신됐으면 그 값을 쓴다.
async function withLock(fn) {
  fs.mkdirSync(DIR, { recursive: true });
  const deadline = Date.now() + 8000; // headersHelper는 10초 안에 끝나야 한다
  for (;;) {
    try {
      fs.closeSync(fs.openSync(LOCK, 'wx'));
      break;
    } catch {
      try {
        if (Date.now() - fs.statSync(LOCK).mtimeMs > 30000) fs.rmSync(LOCK, { force: true }); // 죽은 잠금
      } catch { /* 방금 풀렸다 */ }
      if (Date.now() > deadline) throw new Error('다른 창이 갱신 중입니다 — 잠시 뒤 다시 연결하세요.');
      await sleep(150);
    }
  }
  try {
    return await fn();
  } finally {
    fs.rmSync(LOCK, { force: true });
  }
}

async function postForm(url, fields) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(fields),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error_description || body.detail || `HTTP ${res.status}`);
    err.code = body.error || '';
    throw err;
  }
  return body;
}

async function metadata() {
  const url = `${BASE}/paas/oauth/.well-known/oauth-authorization-server`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`발급자 정보를 못 읽었습니다(${res.status}) — ${url}`);
  return res.json();
}

function subjectOf(accessToken) {
  try {
    return JSON.parse(Buffer.from(accessToken.split('.')[1], 'base64url').toString()).sub || '';
  } catch {
    return '';
  }
}

function openBrowser(url) {
  if (process.env.PAAS_MCP_NO_BROWSER) return;
  const [cmd, args] = process.platform === 'win32'
    ? ['rundll32', ['url.dll,FileProtocolHandler', url]] // start는 &가 든 주소를 자른다
    : process.platform === 'darwin' ? ['open', [url]] : ['xdg-open', [url]];
  try {
    spawn(cmd, args, { stdio: 'ignore', detached: true }).unref();
  } catch { /* 주소는 이미 화면에 찍혀 있다 */ }
}

const DONE_PAGE = (ok, text) => `<!doctype html><meta charset="utf-8"><title>GPAX MCP</title>
<body style="font-family:'Malgun Gothic',sans-serif;background:#12131f;color:#e4e6f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="background:#1a1b2a;border:1px solid #2c2e44;border-radius:12px;padding:28px 32px;max-width:460px">
<h2 style="margin:0 0 8px;font-size:18px">${ok ? 'MCP 로그인 완료' : 'MCP 로그인 실패'}</h2><p style="color:#9aa0b8;margin:0">${text}</p></div>`;

async function login() {
  const meta = await metadata();
  const previous = readState();
  // 예전 연결이 있으면 끊고 새로 시작한다 — 「승인된 앱」에 같은 PC가 두 줄로 남지 않게.
  if (previous?.refresh_token && previous.revocation_endpoint) {
    await postForm(previous.revocation_endpoint, { token: previous.refresh_token }).catch(() => {});
  }

  const reg = await fetch(meta.registration_endpoint, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      client_name: `GPAX MCP 로그인 도우미 (${os.hostname()})`,
      redirect_uris: ['http://127.0.0.1/callback'],
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
    }),
  }).then(async (r) => {
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(`클라이언트 등록 실패: ${body.error_description || r.status}`);
    return body;
  });

  const verifier = b64url(crypto.randomBytes(48));
  const challenge = b64url(crypto.createHash('sha256').update(verifier).digest());
  const state = b64url(crypto.randomBytes(16));

  const server = http.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const redirectUri = `http://127.0.0.1:${server.address().port}/callback`;

  const code = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('5분 안에 로그인이 끝나지 않았습니다.')), LOGIN_TIMEOUT_MS);
    server.on('request', (req, res) => {
      const url = new URL(req.url, redirectUri);
      if (url.pathname !== '/callback') {
        res.writeHead(404).end();
        return;
      }
      const error = url.searchParams.get('error');
      const ok = !error && url.searchParams.get('state') === state && url.searchParams.get('code');
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(DONE_PAGE(Boolean(ok), ok ? '이 창을 닫고 터미널로 돌아가세요.'
        : error === 'access_denied' ? '승인을 거부했습니다.' : '응답이 올바르지 않습니다 — 다시 시도하세요.'));
      clearTimeout(timer);
      if (ok) resolve(url.searchParams.get('code'));
      else reject(new Error(error === 'access_denied' ? '승인을 거부했습니다.' : '로그인 응답이 올바르지 않습니다.'));
    });
    const authorize = new URL(meta.authorization_endpoint);
    authorize.search = new URLSearchParams({
      response_type: 'code', client_id: reg.client_id, redirect_uri: redirectUri,
      code_challenge: challenge, code_challenge_method: 'S256', state, scope: 'mcp', resource: API,
    }).toString();
    process.stderr.write(`브라우저에서 GPAX 로그인·승인을 진행하세요. 창이 안 열리면 이 주소를 여세요:\n${authorize}\n`);
    openBrowser(authorize.toString());
  }).finally(() => server.close());

  const tok = await postForm(meta.token_endpoint, {
    grant_type: 'authorization_code', code, redirect_uri: redirectUri, client_id: reg.client_id,
    code_verifier: verifier, resource: API,
  });
  writeState({
    base: BASE, client_id: reg.client_id, token_endpoint: meta.token_endpoint,
    revocation_endpoint: meta.revocation_endpoint, refresh_token: tok.refresh_token,
    access_token: tok.access_token, expires_at: Math.floor(Date.now() / 1000) + tok.expires_in,
    email: subjectOf(tok.access_token),
  });
  process.stderr.write(`로그인했습니다: ${subjectOf(tok.access_token)}\n`
    + `다음: Claude Code에 등록 —  node "${slash(SELF)}" install\n`);
}

async function headers() {
  let st = readState();
  if (!st?.refresh_token) fail(`로그인이 필요합니다 — ${loginHint()}`);
  const fresh = (s) => s.access_token && s.expires_at - EARLY_SECONDS > Date.now() / 1000;
  if (!fresh(st)) {
    st = await withLock(async () => {
      const cur = readState();
      if (cur && fresh(cur)) return cur; // 다른 창이 방금 갱신했다
      try {
        const tok = await postForm(cur.token_endpoint, {
          grant_type: 'refresh_token', refresh_token: cur.refresh_token, client_id: cur.client_id,
        });
        const next = { ...cur, refresh_token: tok.refresh_token, access_token: tok.access_token,
                       expires_at: Math.floor(Date.now() / 1000) + tok.expires_in };
        writeState(next);
        return next;
      } catch (e) {
        if (e.code === 'invalid_grant' || e.code === 'invalid_client') {
          writeState({ ...cur, refresh_token: '', access_token: '', expires_at: 0 });
          fail(`연결이 끊겼습니다(${e.message}) — 다시 로그인: ${loginHint()}`);
        }
        throw e;
      }
    });
  }
  process.stdout.write(`${JSON.stringify({ Authorization: `Bearer ${st.access_token}` })}\n`);
}

function findClaude() {
  // .cmd는 셸 없이 실행할 수 없고(Node 보안 정책), 셸을 거치면 JSON의 따옴표가 깨진다 — 실행 파일을 직접 찾는다.
  const dirs = (process.env.PATH || '').split(path.delimiter).filter(Boolean);
  const names = process.platform === 'win32' ? ['claude.exe'] : ['claude'];
  for (const dir of dirs) {
    for (const name of names) {
      const p = path.join(dir, name);
      if (fs.existsSync(p)) return p;
    }
    if (process.platform === 'win32' && fs.existsSync(path.join(dir, 'claude.cmd'))) {
      const exe = path.join(dir, 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.exe');
      if (fs.existsSync(exe)) return exe;
    }
  }
  return null;
}

function install(name = 'paas') {
  const st = readState();
  if (!st?.refresh_token) fail(`먼저 로그인하세요 — ${loginHint()}`);
  const config = { type: 'http', url: HUB, headersHelper: helperCommand() };
  const claude = findClaude();
  if (!claude) {
    process.stderr.write('claude 실행 파일을 찾지 못했습니다. ~/.claude.json의 mcpServers에 직접 넣으세요:\n');
    process.stdout.write(`${JSON.stringify({ [name]: config }, null, 2)}\n`);
    return;
  }
  // 같은 이름이 사용자 범위에 있으면 바꾼다 — 토큰 방식으로 등록해 둔 것일 수 있으니 바꿨다고 알린다.
  const removed = spawnSync(claude, ['mcp', 'remove', '--scope', 'user', name], { stdio: 'ignore' });
  if (removed.status === 0) process.stderr.write(`기존 '${name}' 등록(사용자 범위)을 이 도우미 방식으로 바꿉니다.\n`);
  const out = spawnSync(claude, ['mcp', 'add-json', '--scope', 'user', name, JSON.stringify(config)], { stdio: 'inherit' });
  if (out.status !== 0) fail('Claude Code 등록에 실패했습니다 — 위 메시지를 확인하세요.');
  process.stderr.write(`등록했습니다: ${name} → ${HUB}\nClaude Code를 새로 열면 붙습니다(/mcp에서 확인).\n`);
}

function status() {
  const st = readState();
  if (!st?.refresh_token) {
    process.stdout.write(`로그인돼 있지 않습니다 — ${loginHint()}\n`);
    return;
  }
  const left = st.expires_at - Math.floor(Date.now() / 1000);
  process.stdout.write(`계정: ${st.email || '(알 수 없음)'}\n서버: ${st.base}\n`
    + `액세스 토큰: ${left > 0 ? `${Math.floor(left / 60)}분 남음` : '만료(다음 연결 때 갱신)'}\n저장: ${STATE}\n`);
}

async function logout() {
  const st = readState();
  if (st?.refresh_token && st.revocation_endpoint) {
    await postForm(st.revocation_endpoint, { token: st.refresh_token }).catch(() => {});
  }
  fs.rmSync(STATE, { force: true });
  process.stdout.write('로그아웃했습니다 — 서버의 승인도 끊었습니다.\n');
}

const [cmd, arg] = process.argv.slice(2);
try {
  if (!/^https?:\/\//.test(BASE)) fail('서버 주소가 비었습니다 — 콘솔에서 이 파일을 다시 내려받으세요.');
  if (cmd === 'login') await login();
  else if (cmd === 'headers') await headers();
  else if (cmd === 'install') install(arg);
  else if (cmd === 'status') status();
  else if (cmd === 'logout') await logout();
  else process.stdout.write('사용법: node paas-mcp.mjs login | install [이름] | status | logout | headers\n');
} catch (e) {
  fail(e.message || String(e));
}
