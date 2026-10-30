/* GPAX 브라우저 스캔 — 사용자가 로그인한 탭에서 북마크릿으로 돈다(pages/SourceDetail.tsx).
 *
 * 누르면 할 일을 고른다.
 *  - 스캔·수집: 화면에 그려진 메뉴 트리, 열려 있는 화면(같은 출처의 iframe 포함)의 제목·표 머리글·
 *    조회 조건, 메뉴의 GET 링크 몇 장, 같은 출처의 문서·표·발표 파일(GET)을 읽고, 스크립트로 여는 메뉴는
 *    숨긴 틀에서 눌러 조회 화면과 그리로 가는 길을 찾는다(「메뉴 탐색」). 수집 조건이 있으면 그 화면을
 *    조회해 결과 파일과 행마다 상세의 첨부를 받아(「수집」) JSON 파일로 내려받는다.
 *  - 녹화: 사람이 한 번 보여 주는 길(메뉴 → 조건 → 조회 → 첫 행의 상세 → 첨부)을 적어 JSON으로 내려받는다.
 *  - 스마트워크로: 지금 화면의 글자를 받는 창으로 넘겨 스마트워크 대화에 붙인다.
 * 하지 않는 일: gpax로 직접 보내기(운영 콘솔이 http라 https 페이지가 부를 수 없다 — 콘솔 창과 postMessage로만),
 * 쿠키 읽기, 입력값 읽기(이름·라벨만 — 수집 조건을 채울 뿐이다), 저장·삭제·승인 같은 버튼 누르기, 로그아웃·삭제
 * 링크 열기. 바깥 파일을 불러오지 않는다 — 북마크에 이 코드가 통째로 들어간다(외부 의존 금지).
 *
 * 북마크는 하나다(lib/agentCode.ts — 웹 Outlook에서는 mailAgent.js가 돈다). 이 코드는 식 하나
 * `(function (SOURCE) {…})`이고, SOURCE에는 콘솔 주소({console, receive})만 온다. 이 사이트의 출처 정보
 * (id·이름·수집 조건)는 시작할 때 받는다 — 콘솔이 연 탭이면 콘솔이, 아니면 이 코드가 연 받는 창(콘솔의
 * #/agent)이 로그인한 콘솔 탭에서 사이트 주소로 찾아 보낸다(start).
 */
(function (SOURCE, DATA) {
  var AGENT = 'gpax-scan/1';
  var MAX_FETCH = 25;
  /* 받는 파일 — 서버(services/infosource.FILE_TYPES)가 받는 형식과 같다. 크기는 올릴 파일이 너무
   * 커지지 않게 서버 상한보다 작게 잡는다(base64로 실려 4/3배가 된다). */
  var MAX_FILES = 60, MAX_FILE_BYTES = 10000000, MAX_TOTAL_BYTES = 50000000;
  var FILE_EXT = /\.(pdf|docx?|hwpx?|txt|xlsx?|csv|pptx?)$/i;
  var FILE_TYPE = /pdf|msword|ms-excel|ms-powerpoint|officedocument|hwp|text\/csv/i;
  var DANGER = /log-?out|sign-?out|logoff|로그아웃|delete|remove|삭제/i;
  var NAV = /(^|[-_ ])(nav|navbar|menu|gnb|lnb|snb|sidebar|sitemap|tree)([-_ ]|$)/i;
  var NAV_ROLES = { navigation: 1, menu: 1, menubar: 1, tree: 1, tablist: 1 };
  var origin = location.origin;

  /* 진행 창 — 처음에는 할 일(스캔·수집 / 녹화)을 고르고, 도는 동안에는 지금 하는 일·지나온 기록(실시간)과
   * 바꿀 수 있는 것: 상세를 열 행 수(조회 한도)와 멈추기. 한도는 다음 행부터 바로 반영되고, 멈추면 지금
   * 하던 일까지 하고 받은 것을 내려받는다. */
  var MAX_ROWS = 100;
  /* fresh — 봇 실행: 열려 있는 화면을 쓰지 않고 녹화·탐색한 길로 처음부터 연다(사람이 한 그대로 되는지). */
  var ctl = { rows: 20, stop: false, explore: true, fresh: false };
  /* 창의 모양은 요소마다 직접 못 박는다(all:initial 뒤에 색·글꼴) — 이 창은 남의 사이트 안에 뜬다. 사이트의
   * div·button·input 규칙(div{color:#333}, button{background:#fff;color:#fff} 같은)은 물려받은 색보다
   * 이겨서, 못 박지 않으면 어두운 바탕에 어두운 글자·흰 바탕에 흰 글자가 된다.
   * 대비(WCAG): 창 #f9fafb/#1f2937 ≈ 15:1, 기록 #d1d5db/#111827 ≈ 12:1, 버튼 #f9fafb/#374151 ≈ 9:1,
   * 주 버튼 #fff/#2563eb ≈ 5:1. */
  var FONT = 'font:13px/1.45 "Malgun Gothic","Apple SD Gothic Neo",sans-serif;';
  var INK = 'all:initial;box-sizing:border-box;' + FONT + 'color:#f9fafb;';
  function paint(el, css) { el.style.cssText = INK + css; return el; }
  var box = paint(document.createElement('div'), 'display:block;position:fixed;top:12px;right:12px;' +
    'z-index:2147483647;background:#1f2937;width:380px;padding:10px 14px;border-radius:6px;box-shadow:0 2px 8px #0006');
  var head = paint(document.createElement('div'), 'display:block;font-weight:700;color:#fff;margin-bottom:4px;' +
    'cursor:move;user-select:none;touch-action:none');
  var status = paint(document.createElement('div'), 'display:block;margin-bottom:6px;word-break:break-all');
  var logBox = paint(document.createElement('div'), 'display:block;max-height:180px;overflow:auto;' +
    'background:#111827;padding:4px 6px;border-radius:4px;scrollbar-color:#4b5563 #111827');
  var bar = paint(document.createElement('div'), 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;' +
    'margin-top:6px;font-size:12px');
  var rowsIn = paint(document.createElement('input'), 'appearance:auto;display:inline-block;width:56px;' +
    'font-size:12px;color:#111827;background:#fff;border:1px solid #9ca3af;border-radius:3px;padding:1px 4px');
  var stopBtn = button('멈추기', null);
  head.textContent = 'GPAX';
  head.title = '끌어서 옮기기';
  /* 머리글을 끌어 창을 옮긴다 — 사이트의 버튼·표를 가리면 비켜 놓는다. 머리글이 화면 밖으로 나가지 않게. */
  head.onpointerdown = function (e) {
    if (e.button !== 0) return;
    var r = box.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
    head.setPointerCapture(e.pointerId);
    head.onpointermove = function (m) {
      box.style.left = Math.max(0, Math.min(window.innerWidth - r.width, m.clientX - dx)) + 'px';
      box.style.top = Math.max(0, Math.min(window.innerHeight - head.offsetHeight - 20, m.clientY - dy)) + 'px';
      box.style.right = 'auto';
    };
    head.onpointerup = head.onpointercancel = function () { head.onpointermove = null; };
    e.preventDefault();
  };
  rowsIn.type = 'number';
  rowsIn.min = '1';
  rowsIn.max = String(MAX_ROWS);
  rowsIn.value = String(ctl.rows);
  rowsIn.setAttribute('aria-label', 'GPAX 상세 열 행 수');
  rowsIn.oninput = function () {
    var n = parseInt(rowsIn.value, 10);
    if (n >= 1 && n <= MAX_ROWS && n !== ctl.rows) { ctl.rows = n; log('조회 한도를 ' + n + '행으로 바꿨습니다'); }
  };
  stopBtn.onclick = function () {
    if (ctl.stop) return;
    ctl.stop = true;
    stopBtn.disabled = true;
    stopBtn.style.opacity = '0.5';
    log('멈추는 중 — 지금 하던 일까지 하고 받은 것을 내려받습니다');
  };
  /* primary — 그 자리에서 먼저 누를 것(스캔·수집, 녹화 끝) */
  function button(text, onclick, primary) {
    var b = paint(document.createElement('button'), 'display:inline-block;font-size:12px;cursor:pointer;' +
      'padding:3px 10px;border-radius:4px;' + (primary ? 'color:#fff;background:#2563eb;border:1px solid #1d4ed8'
        : 'color:#f9fafb;background:#374151;border:1px solid #4b5563'));
    b.type = 'button';
    b.textContent = text;
    b.onclick = onclick;
    return b;
  }
  function controls() {
    bar.textContent = '';
    bar.appendChild(document.createTextNode('상세 열 행'));
    bar.appendChild(rowsIn);
    bar.appendChild(stopBtn);
  }
  function choose() {
    var exp = paint(document.createElement('input'), 'appearance:auto;display:inline-block;width:14px;height:14px;' +
      'margin:0 4px 0 0;accent-color:#2563eb;cursor:pointer');
    var lab = paint(document.createElement('label'), 'display:inline-flex;align-items:center;font-size:12px;cursor:pointer');
    exp.type = 'checkbox';
    exp.checked = true;
    lab.appendChild(exp);
    lab.appendChild(document.createTextNode(' 메뉴 탐색'));
    lab.title = '주소 없이 스크립트로 여는 메뉴를 숨긴 틀에서 눌러 보고 조회 화면과 그리로 가는 길을 찾습니다(최대 40개)';
    bar.textContent = '';
    /* 등록한 출처면 데이터 수집 셋, 받는 창과 이어졌으면 스마트워크로 — 등록하지 않은 사이트는 그것만. */
    if (SOURCE.id) {
      bar.appendChild(button('스캔·수집', function () {
        ctl.explore = exp.checked;
        controls();
        run().catch(function (e) { running = false; say('실패 — ' + (e && e.message)); finish(); });
      }, true));
      bar.appendChild(button('녹화', startRecording));
      if ((SOURCE.queries || []).length) bar.appendChild(button('봇 실행', botRun));
    }
    if (viaReceiver()) bar.appendChild(button('스마트워크로', toSmartwork, !SOURCE.id));
    if (SOURCE.id) bar.appendChild(lab);
    say(SOURCE.id
      ? '무엇을 할까요? 스캔·수집 — 화면을 읽고 수집 조건대로 받기 · 녹화 — 조회 화면까지 가는 길을 한 번 보여 주기' +
        ((SOURCE.queries || []).length ? ' · 봇 실행 — 수집 조건대로 다시 받기' : '') + ' · 스마트워크로 — 이 화면을 대화에'
      : '등록하지 않은 사이트입니다 — 「스마트워크로」로 이 화면을 대화에 붙일 수 있습니다(데이터 수집은 콘솔의 데이터 ' +
        '수집에서 이 사이트를 출처로 등록하세요).');
  }
  function viaReceiver() { return !!link && link.win !== window.opener; }
  /* 스마트워크로 — 지금 화면의 글자(글이 가장 많은 같은 출처 틀)를 받는 창으로 넘긴다. 받는 창에서 업무를 고른다. */
  function toSmartwork() {
    box.remove();   /* 이 창의 글이 섞이지 않게 */
    var best = null;
    frames(window, 0, []).forEach(function (f) {
      if (f.win.frameElement && ours(f.win.frameElement)) return;
      var t = String((f.doc.body && f.doc.body.innerText) || '');
      if (!best || t.length > best.text.length) best = { text: t, doc: f.doc, url: f.url };
    });
    show();
    if (!best || !best.text.trim()) { say('이 화면에서 읽을 글이 없습니다'); return; }
    var heading = best.doc.querySelector('h1,h2,h3');
    var where = location.href.split('#')[0];
    relay({ gpax: 'page', item: {
      subject: clean((heading && textOf(heading)) || best.doc.title || document.title, 300),
      header: where + (best.url && best.url.split('#')[0] !== where ? ' · ' + best.url : ''),
      body: best.text.replace(/[ \t\u00a0]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 200000) } });
    say('받는 창으로 보냈습니다 — 그 창에서 업무를 고르고 「대화 시작」을 누르세요');
  }
  box.appendChild(head);
  box.appendChild(status);
  box.appendChild(logBox);
  box.appendChild(bar);
  /* 열린 화면을 다 읽은 뒤에 붙인다 — 먼저 붙이면 이 안내문이 화면 글에 섞인다. */
  function show() { if (!box.parentNode) document.body.appendChild(box); }
  function log(t) {
    var line = paint(document.createElement('div'), 'display:block;font:11px/1.5 Consolas,"D2Coding",monospace;' +
      'color:#d1d5db;white-space:pre-wrap;word-break:break-all');
    line.textContent = new Date().toTimeString().slice(0, 8) + ' ' + t;
    logBox.appendChild(line);
    while (logBox.childNodes.length > 300) logBox.removeChild(logBox.firstChild);
    logBox.scrollTop = logBox.scrollHeight;
    show();
    relay({ gpax: 'log', text: t });
  }
  function say(t) { status.textContent = t; log(t); }

  /* 통로 — 상대(peer)는 이 탭을 연 콘솔(콘솔의 「스캔·수집 시작」·「녹화 시작」) 또는 이 코드가 연 받는 창이다.
   * 인사(hello)에 상대가 답해야(start) 통로가 열린다: 보낸 쪽 출처(콘솔 주소)와 창을 둘 다 확인하고, 상대가 준
   * 표(nonce)를 이후 메시지마다 주고받는다. 진행 기록은 실시간으로, 결과는 파일 대신 상대에게 간다(받는 창은
   * 로그인한 콘솔 탭으로 넘겨 올린다). 수집 조건은 늘 상대가 보낸 지금 것이다 — 북마크를 다시 끌어다 놓지 않는다. */
  var link = null, peer = null, started = false, running = false;
  function relay(msg) {
    if (!link) return;
    msg.nonce = link.nonce;
    try { link.win.postMessage(msg, SOURCE.console); } catch (e) { link = null; }
  }
  function deliver(kind, out, file) {
    if (link) {
      relay({ gpax: 'result', kind: kind, payload: out });
      return true;
    }
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out)], { type: 'application/json' }));
    a.download = file + '-' + SOURCE.id + '-' + String(SOURCE.name).replace(/[\\/:*?"<>|\s]+/g, '_') + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    return false;
  }
  function listen() {
    window.addEventListener('message', function (e) {
      var m = e.data;
      if (!m || typeof m !== 'object' || e.origin !== SOURCE.console || !peer || e.source !== peer) return;
      if (m.gpax === 'start' && !link && typeof m.nonce === 'string' && m.nonce) {
        link = { win: e.source, nonce: m.nonce };
        /* 이 사이트의 출처 — 등록하지 않은 사이트면 없다(그때는 스마트워크로 보내기만). */
        var src = m.source && typeof m.source === 'object' ? m.source : null;
        if (src && src.origin === origin) {
          SOURCE.id = src.id;
          SOURCE.name = String(src.name || '');
          SOURCE.origin = src.origin;
          SOURCE.queries = Array.isArray(src.queries) ? src.queries : [];
          SOURCE.version = String(src.version || '');
        }
        if (Array.isArray(m.queries)) { SOURCE.queries = m.queries; SOURCE.version = String(m.version || ''); }
        head.textContent = 'GPAX' + (SOURCE.id ? ' · ' + SOURCE.name : '') + ' · 콘솔과 연결됨';
        log('콘솔과 연결했습니다 — 기록과 결과가 콘솔로 갑니다' +
            (SOURCE.id ? ' (수집 조건 ' + (SOURCE.queries || []).length + '개)' : ' (등록하지 않은 사이트)'));
        if (started) return;
        if (!m.mode) { choose(); return; }
        if (m.mode === 'record') startRecording();
        else if (m.mode === 'replay') botRun();
        else if (m.mode === 'scan') {
          ctl.explore = m.explore !== false;
          controls();
          run().catch(function (err) { running = false; say('실패 — ' + (err && err.message)); finish(); });
        }
        return;
      }
      if (!link || m.nonce !== link.nonce) return;
      if (m.gpax === 'stop' && started && !rec) stopBtn.onclick();
      if (m.gpax === 'end' && rec) finishRecording();
      /* 녹화를 끝낸 탭에서 바로 — 북마크를 다시 누르지 않아도 된다. */
      if (m.gpax === 'replay' && !rec && !running && Array.isArray(m.queries)) {
        SOURCE.queries = m.queries;
        SOURCE.version = String(m.version || '');
        botRun();
      }
    });
  }

  function clean(s, cap) { return String(s || '').replace(/\s+/g, ' ').trim().slice(0, cap || 200); }
  function textOf(el) { return clean(el ? (el.innerText || el.textContent) : '', 200); }
  function isNav(el) {
    if (!el || !el.getAttribute) return false;
    if (el.tagName === 'NAV' || NAV_ROLES[el.getAttribute('role')]) return true;
    return NAV.test((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : ''));
  }
  function inNav(el) {
    for (var cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) if (isNav(cur)) return true;
    return false;
  }
  function same(href) {
    if (!href || href.charAt(0) === '#') return '';  /* 스크립트가 여는 메뉴(href="#") — 주소가 없다 */
    try {
      var u = new URL(href, location.href);
      return u.origin === origin && /^https?:$/.test(u.protocol) ? u.href.split('#')[0] : '';
    } catch (e) { return ''; }
  }

  /* 전역에 있는 그리드 객체 — RealGrid·dhtmlx는 머리글을 DOM이 아니라 캔버스·스크립트로 그린다. */
  function globalsOf(win) {
    var out = [];
    try {
      var keys = Object.keys(win).slice(0, 5000);
      for (var i = 0; i < keys.length; i++) {
        try {
          var v = win[keys[i]];
          if (v && typeof v === 'object' && v !== win && v !== win.document) out.push(v);
        } catch (e) { /* 접근이 막힌 속성 */ }
      }
    } catch (e) { /* 다른 출처 창 */ }
    return out;
  }
  function gridHeaders(win) {
    var tables = [];
    globalsOf(win).forEach(function (g) {
      try {
        var cols = null;
        if (typeof g.getColumns === 'function' && typeof g.getDataSource === 'function') {
          cols = g.getColumns().map(function (c) {
            var h = c && c.header;
            return clean((h && (h.text || h)) || c.name || c.fieldName, 80);
          });
        } else if (g.config && Array.isArray(g.config.columns)) {
          cols = g.config.columns.map(function (c) {
            var h = c && c.header;
            return clean(Array.isArray(h) ? (h[0] && (h[0].text || h[0])) : (h || c.id), 80);
          });
        }
        cols = (cols || []).filter(function (c) { return c && typeof c === 'string'; });
        if (cols.length) tables.push(cols.slice(0, 40));
      } catch (e) { /* 그리드가 아니다 */ }
    });
    return tables.slice(0, 10);
  }

  /* 칸 이름 — 라벨, 그다음 표의 머리 칸(등록일 | [시작] ~ [끝]). 안내 글(YYYY.MM.DD)은 이름이 아니라 그 뒤다. */
  function fieldLabel(el) {
    if (el.labels && el.labels[0]) return textOf(el.labels[0]);
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'), 80);
    var cell = el.closest && el.closest('td');
    var head = cell && cell.previousElementSibling;
    if (head && /^(TH|TD)$/.test(head.tagName) && textOf(head)) return textOf(head);
    var named = el.getAttribute('placeholder') || el.getAttribute('title');
    if (named) return clean(named, 80);
    return clean(el.name || el.id, 80);
  }

  function readDoc(doc, url, win) {
    var body = doc.body;
    var page = {
      url: url, title: clean(doc.title, 200), headings: [], tables: [], fields: [], links: [],
      requires_login: false,
      text: clean(body ? (body.innerText || body.textContent) : '', 4000),
    };
    /* 열린 화면은 보이는 비밀번호 칸만 — 숨겨 둔 로그인 폼이 있는 화면이 많다(받아 온 화면은 배치가 없다). */
    doc.querySelectorAll('input[type=password]').forEach(function (el) {
      if (!win || el.offsetParent !== null) page.requires_login = true;
    });
    if (/로그인\s*되어\s*있지\s*않|not\s+logged\s+in/i.test(page.text)) page.requires_login = true;
    doc.querySelectorAll('h1,h2,h3').forEach(function (h) { var t = textOf(h); if (t) page.headings.push(t); });
    doc.querySelectorAll('table').forEach(function (t) {
      var ths = []; t.querySelectorAll('th').forEach(function (th) { var x = textOf(th); if (x) ths.push(x); });
      if (ths.length) page.tables.push(ths.slice(0, 40));
    });
    if (win) page.tables = page.tables.concat(gridHeaders(win));
    doc.querySelectorAll('input,select,textarea').forEach(function (el) {
      var type = (el.getAttribute('type') || '').toLowerCase();
      if (/^(hidden|password|submit|button|image|reset|file)$/.test(type)) return;
      var label = fieldLabel(el);
      if (label && page.fields.indexOf(label) < 0) page.fields.push(label);
    });
    doc.querySelectorAll('a[href]').forEach(function (a) {
      var u = same(a.getAttribute('href'));
      if (u) page.links.push({ url: u, text: textOf(a), nav: inNav(a) });
    });
    page.headings = page.headings.slice(0, 30);
    page.tables = page.tables.slice(0, 10);
    page.fields = page.fields.slice(0, 30);
    page.links = page.links.slice(0, 200);
    return page;
  }

  /* 메뉴 트리 — ul/li로 그린 메뉴(accordionmenu 등)와 dhtmlx 트리 중 큰 쪽. */
  function ownAnchor(li) {
    var as = li.querySelectorAll('a');
    for (var i = 0; i < as.length; i++) if (as[i].closest('li') === li) return as[i];
    return null;
  }
  function ownLabel(li) {
    var c = li.cloneNode(true);
    c.querySelectorAll('ul,ol').forEach(function (x) { x.remove(); });
    return clean(c.textContent, 80);
  }
  function fromList(list, depth) {
    var out = [];
    if (depth > 4) return out;
    for (var i = 0; i < list.children.length; i++) {
      var li = list.children[i];
      if (li.tagName !== 'LI') continue;
      var a = ownAnchor(li);
      var sub = li.querySelector('ul,ol');
      var href = a ? same(a.getAttribute('href')) : '';
      out.push({ label: ownLabel(li), url: href, children: sub ? fromList(sub, depth + 1) : [] });
    }
    return out;
  }
  function count(items) {
    return items.reduce(function (n, m) { return n + 1 + count(m.children || []); }, 0);
  }
  function domMenu(doc) {
    var best = [], bestN = 0;
    doc.querySelectorAll('nav,[role],[id],[class]').forEach(function (el) {
      if (!isNav(el)) return;
      var tops = [];
      el.querySelectorAll('ul,ol').forEach(function (l) {
        var parent = l.parentElement && l.parentElement.closest('li');
        if (!parent || !el.contains(parent)) tops.push(l);
      });
      var items = [];
      tops.forEach(function (l) { items = items.concat(fromList(l, 0)); });
      var n = count(items);
      if (n > bestN) { best = items; bestN = n; }
    });
    return best;
  }
  function dhxMenu(win) {
    var best = [], bestN = 0;
    function conv(list, depth) {
      return (Array.isArray(list) ? list : []).slice(0, 200).map(function (it) {
        return { label: clean(it.value || it.text || it.label || it.id, 80), url: same(it.url || it.href || ''),
                 children: depth < 4 ? conv(it.items || it.data || [], depth + 1) : [] };
      });
    }
    globalsOf(win).forEach(function (g) {
      try {
        /* 트리 모양 컬렉션만 — 그리드의 data.serialize()는 행 값(업무 데이터)이라 읽지 않는다. */
        if (!g.data || typeof g.data.serialize !== 'function' || typeof g.data.getRoot !== 'function') return;
        if (g.config && Array.isArray(g.config.columns)) return;
        var items = conv(g.data.serialize(), 0);
        var n = count(items);
        if (n > bestN) { best = items; bestN = n; }
      } catch (e) { /* 트리가 아니다 */ }
    });
    return best;
  }

  /* 열려 있는 화면 — 같은 출처의 iframe까지(탭으로 여는 사이트는 화면마다 iframe이다). */
  function frames(win, depth, out) {
    try {
      var doc = win.document;
      out.push({ win: win, doc: doc, url: same(win.location.href) || location.href.split('#')[0] });
    } catch (e) { return out; }  /* 다른 출처 */
    if (depth < 3) for (var i = 0; i < win.frames.length; i++) frames(win.frames[i], depth + 1, out);
    return out;
  }

  /* 내려받기 이름 — Content-Disposition의 filename*(RFC 5987)이 먼저, 없으면 filename. */
  function dispositionName(res) {
    var h = res.headers.get('content-disposition') || '';
    var m = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(h) || /filename\s*=\s*"?([^";]+)"?/i.exec(h);
    if (!m) return '';
    try { return clean(decodeURIComponent(m[1]), 200); } catch (e) { return clean(m[1], 200); }
  }
  function base64Of(blob) {
    return new Promise(function (ok, fail) {
      var r = new FileReader();
      r.onload = function () { ok(String(r.result).split(',')[1] || ''); };
      r.onerror = function () { fail(r.error); };
      r.readAsDataURL(blob);
    });
  }
  /* 받은 응답이 파일이면 싣는다 — 크기를 넘거나 화면(로그인)이 오면 싣지 않는다. */
  async function addFile(files, res, t, budget) {
    var type = res.headers.get('content-type') || '';
    if (/html/i.test(type) || files.length >= MAX_FILES) return false;
    return addBlob(files, await res.blob(), { url: t.fixed ? t.url : same(res.url) || t.url,
      name: t.name || dispositionName(res), text: t.text, page: t.page, type: type }, budget);
  }
  async function addBlob(files, blob, t, budget) {
    if (!blob.size || blob.size > MAX_FILE_BYTES || blob.size > budget.left || files.length >= MAX_FILES) return false;
    if (files.some(function (f) { return f.url === t.url; })) return false;
    budget.left -= blob.size;
    files.push({ url: t.url, name: clean(t.name, 200), text: clean(t.text, 120), page: t.page || '',
                 type: clean(t.type || blob.type, 200), data: await base64Of(blob) });
    return true;
  }

  /* ---------------- 수집 — 저장한 조건으로 조회하고, 결과 파일과 행마다 상세의 첨부를 받는다 ----------------
   * 화면을 여는 길은 셋이다(앞의 것부터):
   *  ① 사람이 열어 둔 화면 — 탭·프레임 안에 그 화면이 떠 있으면 그 자리에서 한다(메뉴로만 열리는 사이트).
   *  ② 녹화·탐색이 남긴 길 — 숨긴 틀에 시작 화면을 열고 메뉴 누름을 다시 밟는다(아래 「위치 찾기」).
   *  ③ 주소 — 숨긴 틀에 그 주소를 연다. 껍데기 화면이면 그 안의 틀까지 내려가 조건 칸을 찾는다.
   * 숨긴 틀은 sandbox다 — 사이트가 틀을 벗어나려 해도(top.location) 이 북마크와 사람의 화면은 그대로다.
   * 누르는 것은 화면으로 가는 메뉴와 조회·상세·첨부·엑셀 버튼뿐이다:
   *  - 이름(짧은 버튼 글자)이나 스크립트(onclick·href)에 저장·삭제·승인 같은 말이 걸리면 누르지 않는다.
   *  - 사이트가 묻는 창(confirm)에는 늘 '아니오', 알림(alert)은 띄우지 않고 메모로 남긴다. 사람의 화면에
   *    건 것은 끝나면 되돌린다(그대로 두면 사람이 누른 저장의 확인 창까지 '아니오'가 된다).
   *  - 첨부·엑셀을 누를 때는 이동·새 창·폼 전송을 막고 같은 요청으로 파일을 받는다(사람 PC에 내려받지 않는다). */
  var MAX_ATTACH = 10, SETTLE_MS = 700, WAIT_MS = 12000, CATCH_MS = 3000;
  var MUTATE_TEXT = /저장|삭제|등록|수정|승인|반려|결재|상신|전송|제출|발송|취소|확정|신청|반영|초기화|로그아웃|save|delete|remove|insert|update|modify|edit|approve|reject|submit|send|cancel|reset|logout|sign ?out/i;
  var MUTATE_CODE = /delete|remove|save|insert|update|modify|approve|reject|cancel|logout|signout|send|_del|del_|fn_?del|fn_?reg|fn_?upd|fn_?apr/i;
  var SEARCH = /^(조회|검색|찾기|조회하기|검색하기|search|find)$/i;
  /* 버튼 글자에 빈칸을 넣어 넓히는 사이트가 있다(「조 회」) — 빈칸을 빼고 본다. */
  function isSearch(t) { return SEARCH.test(String(t || '').replace(/\s+/g, '')); }
  var DETAIL = /상세|보기|detail|view|열기/i;
  var EXPORT = /엑셀|excel|xls|export|내보내기|다운로드|내려받기|download/i;
  var NOT_EXPORT = /양식|서식|템플릿|template|매뉴얼|manual|도움말|help|첨부/i;
  var ATTACH = /첨부|다운로드|내려받기|download|파일/i;
  var FILE_NAME = /\.(pdf|docx?|hwpx?|txt|xlsx?|csv|pptx?)(\s|\(|\[|$)/i;
  var CLOSE = /^(닫기|close|×|x|✕)$/i;
  var CLICKABLE = 'a,button,input[type=button],input[type=submit],input[type=image],[role=button],[onclick]';
  var mode = 'pass', caught = [], notes = null, armed = [];
  /* 길이 막힌 곳 — 무엇을 찾다 못 찾았고, 그 자리에 실제로 무엇이 있었는지. 결과 파일에 실려 콘솔이
   * LLM에게 고칠 길을 묻는다(services/infosource.suggest_repair — 고르는 것은 여기 실린 것 중에서만). */
  var repair = null;
  /* 고를 수 있는 것만 — 글자가 있고, 무언가를 바꾸지 않고, 같은 글자의 누를 것을 품은 껍데기(li)는 빼고. */
  function candidatesOf(doc) {
    return Array.prototype.slice.call(doc.querySelectorAll(CLICKABLE + ',li')).filter(function (el) {
      var t = labelOf(el);
      if (box.contains(el) || !t || risky(el)) return false;
      return !Array.prototype.some.call(el.querySelectorAll(CLICKABLE), function (c) { return labelOf(c) === t; });
    }).slice(0, 150).map(locOf);
  }
  /* 수집 메모 — 결과 파일에 실리고(콘솔의 스캔 메모), 진행 창에도 바로 보인다. */
  function note(t) { if (notes) notes.push(t); log('! ' + t); }

  function sleep(ms) { return new Promise(function (ok) { setTimeout(ok, ms); }); }
  function docOf(frame) { try { return frame.contentDocument; } catch (e) { return null; } }
  function winOf(frame) { try { return frame.contentWindow; } catch (e) { return null; } }
  function labelOf(el) {
    var img = el.querySelector && el.querySelector('img[alt]');
    return clean(el.innerText || el.textContent || el.value || el.getAttribute('title') ||
                 el.getAttribute('aria-label') || el.getAttribute('alt') || (img && img.getAttribute('alt')), 120);
  }
  function codeOf(el) { return (el.getAttribute('onclick') || '') + ' ' + (el.getAttribute('href') || '') + ' ' + (el.id || ''); }
  /* 버튼 글자만 따진다 — 긴 글자(목록의 제목)는 내용이라, 제목에 '수정'이 들어 있어도 상세 링크다. */
  function risky(el) {
    var t = labelOf(el);
    return (t.length <= 6 && MUTATE_TEXT.test(t)) || MUTATE_CODE.test(codeOf(el)) || DANGER.test(codeOf(el));
  }
  function clickables(doc) { return Array.prototype.slice.call(doc.querySelectorAll(CLICKABLE)); }
  function visible(el) { return el.offsetParent !== null || el.getClientRects().length > 0; }
  function ours(f) { return /^gpax-/.test(f.name || ''); }

  function makeFrame(name) {
    var f = document.createElement('iframe');
    f.name = name;
    /* allow-top-navigation이 없다 — 틀을 벗어나려는 사이트(if (top !== self) top.location = …)를 막는다. */
    f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
    f.style.cssText = 'position:fixed;left:-10000px;top:0;width:1280px;height:900px;border:0;opacity:0;pointer-events:none';
    document.body.appendChild(f);
    return f;
  }
  /* 화면이 가라앉을 때까지 — 틀의 문서가 바뀌는(이동) 동안, DOM이 바뀌는 동안은 기다린다. */
  async function settle(frame) {
    var start = Date.now(), quiet = Date.now(), doc = null, obs = null, n = 0, last = -1;
    while (Date.now() - start < WAIT_MS) {
      var d = docOf(frame);
      if (d !== doc) {
        doc = d; quiet = Date.now();
        if (obs) obs.disconnect();
        obs = null;
        try {
          obs = new frame.contentWindow.MutationObserver(function () { n++; });
          obs.observe(d, { subtree: true, childList: true, characterData: true });
        } catch (e) { obs = null; }
      }
      if (!d || d.readyState !== 'complete' || n !== last) { last = n; quiet = Date.now(); }
      else if (Date.now() - quiet >= SETTLE_MS) break;
      await sleep(120);
    }
    if (obs) obs.disconnect();
  }
  function loaded(frame) {
    return new Promise(function (ok) {
      var done = false;
      function fin() { if (!done) { done = true; ok(); } }
      frame.addEventListener('load', fin, { once: true });
      setTimeout(fin, WAIT_MS);
    });
  }
  async function load(frame, url) {
    var wait = loaded(frame);
    frame.src = url;
    await wait;
    await settle(frame);
  }
  async function until(fn, ms) {
    for (var t0 = Date.now(); Date.now() - t0 < (ms || WAIT_MS); await sleep(200)) {
      var v = fn();
      if (v) return v;
    }
    return null;
  }

  /* 틀 안 화면의 손잡이 — 새 문서가 뜰 때마다 다시 건다(이동하면 전역이 새로 생긴다). disarm이 되돌린다. */
  function arm(frame) {
    var w = winOf(frame);
    try { if (!w || !w.document || w.__gpaxArmed) return; } catch (e) { return; }
    var saved = { alert: w.alert, confirm: w.confirm, prompt: w.prompt, open: w.open,
                  submit: w.HTMLFormElement.prototype.submit, aclick: w.HTMLAnchorElement.prototype.click };
    w.__gpaxArmed = saved;
    armed.push(w);
    w.alert = function (m) { note('사이트 알림: ' + clean(m, 120)); };
    w.confirm = function () { return false; };
    w.prompt = function () { return null; };
    var popup = { closed: false, close: function () {}, focus: function () {}, blur: function () {},
                  document: { write: function () {}, writeln: function () {}, close: function () {} } };
    w.open = function (url) {
      if (url) caught.push({ kind: 'get', url: new URL(url, w.location.href).href, popup: true });
      return popup;
    };
    var submit = saved.submit;
    /* 이 화면 안의 틀 이름인가 — 탭 껍데기는 메뉴 폼을 화면 안의 탭(iframe)으로 보낸다(새 창이 아니다). */
    function inPage(name) {
      try {
        for (var x = w; x; x = x === x.parent ? null : x.parent) if (x.frames[name]) return true;
      } catch (e) { /* 다른 출처 */ }
      return false;
    }
    function take(form) {
      var raw = form.getAttribute('target') || '', target = raw.toLowerCase();
      /* 상세를 열 때는 같은 틀·화면 안의 틀로 가는 전송은 그대로 둔다 — 새 창으로 가는 것만 잡는다. */
      if (mode === 'detail' && (!target || target === '_self' || target === '_parent' || target === '_top' || inPage(raw))) return false;
      caught.push({ kind: 'form', form: form, submit: submit });
      return true;
    }
    w.HTMLFormElement.prototype.submit = function () {
      if (mode === 'pass' || !take(this)) return submit.call(this);
    };
    saved.onSubmit = function (e) { if (mode !== 'pass' && take(e.target)) e.preventDefault(); };
    w.document.addEventListener('submit', saved.onSubmit, true);
    /* 새 창으로 여는 링크(target=_blank·이름) — 상세·파일을 누를 때만 잡는다. */
    saved.onClick = function (e) {
      if (mode === 'pass') return;
      var a = e.target && e.target.closest && e.target.closest('a[href][target]');
      var t = a ? a.getAttribute('target').toLowerCase() : '';
      if (!a || !t || t === '_self' || t === '_parent' || t === '_top' || /^\s*javascript:/i.test(a.getAttribute('href'))) return;
      e.preventDefault();
      caught.push({ kind: 'get', url: a.href, popup: true });
    };
    w.document.addEventListener('click', saved.onClick, true);
    /* 파일을 누를 때는 이 틀의 이동도 막는다 — location.href로 내려받는 사이트가 많다(크롬·엣지). */
    if (w.navigation) {
      saved.onNav = function (e) {
        if (mode !== 'file' || !e.cancelable) return;
        e.preventDefault();
        var url = e.destination.url;
        caught.push(e.formData ? { kind: 'post', url: url, body: e.formData }
          : { kind: 'get', url: url, early: /^blob:/.test(url) ? fetch(url) : null });
      };
      w.navigation.addEventListener('navigate', saved.onNav);
    }
    /* 화면에 붙이지 않은 링크로 내려받는 경우(스크립트가 만든 blob) — 곧 지워지므로 바로 받는다. */
    var aclick = saved.aclick;
    w.HTMLAnchorElement.prototype.click = function () {
      if (mode === 'file' && this.href && (/^blob:/.test(this.href) || this.hasAttribute('download'))) {
        caught.push({ kind: 'get', url: this.href, name: this.getAttribute('download') || '',
                      early: /^blob:/.test(this.href) ? fetch(this.href) : null });
        return;
      }
      return aclick.call(this);
    };
  }
  function disarmAll() {
    armed.splice(0).forEach(function (w) {
      try {
        var s = w.__gpaxArmed;
        if (!s) return;
        w.alert = s.alert; w.confirm = s.confirm; w.prompt = s.prompt; w.open = s.open;
        w.HTMLFormElement.prototype.submit = s.submit;
        w.HTMLAnchorElement.prototype.click = s.aclick;
        w.document.removeEventListener('submit', s.onSubmit, true);
        w.document.removeEventListener('click', s.onClick, true);
        if (s.onNav) w.navigation.removeEventListener('navigate', s.onNav);
        delete w.__gpaxArmed;
      } catch (e) { /* 이미 다른 화면으로 간 창 */ }
    });
  }

  /* ---------------- 조회 응답 — 조회를 누르는 동안 화면이 보내는 요청(XHR·fetch)과 받은 응답을 적는다 ----------------
   * 화면의 표는 목록의 첫 쪽일 뿐이다. 화면이 받은 응답에는 목록과 전체 건수·페이지 인자가 있어, 그 요청을 쪽만 바꿔
   * 다시 보내면 같은 조건의 목록을 빠짐없이 받는다(어느 응답이 그 목록인지는 lib/scanData.js가 고른다).
   * 응답 본문은 이 탭의 기억에만 둔다 — 결과에는 고른 목록의 레코드와 요청의 모양(경로·인자 이름)만 실린다. 요청
   * 머리(CSRF 토큰 같은 것)는 같은 요청을 다시 보낼 때만 쓰고 결과에 싣지 않는다. 같은 출처의 요청만, 조회를 누른
   * 동안만 적는다. 다 하면 untapAll이 되돌린다(사람의 화면에 건 것도). */
  var MAX_RECORDS = 20000, MAX_PULL = 400, BIG_PAGE = 500, MAX_TEXT = 5000000;
  var realFetch = window.fetch.bind(window), calls = [], listening = false, tapped = [];
  function bodyOf(b) {
    var kind = Object.prototype.toString.call(b);
    if (b == null) return { body: null };
    if (typeof b === 'string') return { body: b };
    if (kind === '[object URLSearchParams]') return { body: b.toString() };
    if (kind === '[object FormData]') {
      var parts = [], plain = true;
      b.forEach(function (v, k) {
        if (typeof v === 'string') parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v)); else plain = false;
      });
      return plain ? { body: parts.join('&'), multipart: true } : { body: null, opaque: true };
    }
    return { body: null, opaque: true };   /* 파일·바이트 본문 — 다시 보낼 수 없다 */
  }
  function headersOf(h) {
    var out = {};
    if (!h) return out;
    if (Array.isArray(h)) h.forEach(function (kv) { out[kv[0]] = kv[1]; });
    else if (typeof h.forEach === 'function') h.forEach(function (v, k) { out[k] = v; });
    else Object.keys(h).forEach(function (k) { out[k] = h[k]; });
    return out;
  }
  function ctypeOf(h) { return h['Content-Type'] || h['content-type'] || ''; }
  function keep(c, text) {
    c.text = text && text.length <= MAX_TEXT ? text : '';
    if (!same(c.url)) return;
    calls.push(c);
    if (calls.length > 60) calls.shift();
  }
  function tap(w) {
    try { if (!w || w.__gpaxTap || !w.XMLHttpRequest) return; } catch (e) { return; }
    var X = w.XMLHttpRequest.prototype;
    var saved = { fetch: w.fetch, open: X.open, send: X.send, header: X.setRequestHeader };
    w.__gpaxTap = saved;
    tapped.push(w);
    if (saved.fetch) {
      w.fetch = function (input, init) {
        var p = saved.fetch.apply(this, arguments);
        if (!listening) return p;
        try {
          var req = input && typeof input === 'object' && input.url ? input : null;
          var heads = headersOf((init && init.headers) || (req && req.headers)), b = bodyOf(init && init.body);
          var c = { method: String((init && init.method) || (req && req.method) || 'GET').toUpperCase(),
                    url: new URL(req ? req.url : String(input), w.location.href).href, headers: heads,
                    body: b.body, multipart: !!b.multipart, opaque: !!b.opaque || (!!req && !init), ctype: ctypeOf(heads) };
          p.then(function (res) {
            c.status = res.status;
            c.rtype = res.headers.get('content-type') || '';
            if (/html|image|octet|pdf|excel|spreadsheet|zip/i.test(c.rtype)) return;
            return res.clone().text().then(function (t) { keep(c, t); });
          }).catch(function () { /* 실패한 요청 */ });
        } catch (e) { /* 적지 못한 요청은 그대로 보낸다 */ }
        return p;
      };
    }
    X.open = function (method, url) {
      this.__gpax = listening ? { method: String(method || 'GET').toUpperCase(), url: String(url), headers: {} } : null;
      return saved.open.apply(this, arguments);
    };
    X.setRequestHeader = function (k, v) {
      if (this.__gpax) this.__gpax.headers[k] = v;
      return saved.header.apply(this, arguments);
    };
    X.send = function (body) {
      var c = this.__gpax, x = this;
      if (c) {
        try {
          var b = bodyOf(body);
          c.url = new URL(c.url, w.location.href).href;
          c.body = b.body; c.multipart = !!b.multipart; c.opaque = !!b.opaque; c.ctype = ctypeOf(c.headers);
          x.addEventListener('loadend', function () {
            try {
              c.status = x.status;
              c.rtype = x.getResponseHeader('content-type') || '';
              keep(c, x.responseType === '' || x.responseType === 'text' ? x.responseText
                : x.responseType === 'json' ? JSON.stringify(x.response) : '');
            } catch (e) { /* 읽지 못한 응답 */ }
          });
        } catch (e) { /* 적지 못한 요청은 그대로 보낸다 */ }
      }
      return saved.send.apply(this, arguments);
    };
  }
  function untapAll() {
    tapped.splice(0).forEach(function (w) {
      try {
        var s = w.__gpaxTap, X = w.XMLHttpRequest.prototype;
        if (s.fetch) w.fetch = s.fetch;
        X.open = s.open; X.send = s.send; X.setRequestHeader = s.header;
        delete w.__gpaxTap;
      } catch (e) { /* 이미 다른 화면으로 간 창 */ }
    });
  }
  /* 잡은 요청을 인자만 바꿔 다시 — 머리는 같게(쿠키·길이처럼 브라우저가 정하는 것은 빼고). JSON이 아니면 undefined. */
  var NO_HEAD = /^(cookie|content-length|host|origin|referer|connection|accept-encoding|user-agent)$/i;
  async function resend(c) {
    var heads = {}, body = c.body;
    Object.keys(c.headers || {}).forEach(function (k) { if (!NO_HEAD.test(k)) heads[k] = c.headers[k]; });
    if (c.multipart && body != null) {
      var fd = new FormData();
      new URLSearchParams(body).forEach(function (v, k) { fd.append(k, v); });
      body = fd;
      Object.keys(heads).forEach(function (k) { if (/^content-type$/i.test(k)) delete heads[k]; });
    }
    var res = await realFetch(c.url, { method: c.method, headers: heads, credentials: 'same-origin',
                                       body: /^(GET|HEAD)$/.test(c.method) ? undefined : body });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return DATA.json(await res.text());
  }
  /* 목록 전체 — 잡은 요청의 쪽만 바꿔 끝까지 받는다. 먼저 쪽 크기를 키워 받아 보고(BIG_PAGE) 서버가 더 주면 **실제로
   * 준 건수**를 쪽 크기로 다시 센다(서버가 상한으로 자르면 그 상한 — 요청한 크기로 세면 다음 쪽이 어긋난다). 멈추는 때(end): total 전체 건수만큼 · short 덜 찬 쪽 · empty 빈 쪽 · repeat 같은 쪽이 또 옴(쪽
   * 인자를 무시하는 서버) · cap 상한 · error 요청 실패 · stop 사람이 멈춤. 쪽 인자가 없으면 one(한 번에 다 온 목록). */
  async function pullAll(found, label) {
    var recs = found.records.slice(), pg = found.paging, total = found.total;
    if (!pg || found.call.opaque) return { records: recs, end: 'one' };
    var size = 0, per = pg.size ? pg.size.value : pg.len, prev = recs, end = '';
    if (pg.size && (total == null || total > recs.length)) {
      var want = total ? Math.min(total, BIG_PAGE) : BIG_PAGE;
      if (want > per) {
        try {
          var big = DATA.at(await resend(DATA.pageCall(found.call, pg, 0, want)), found.path);
          if (Array.isArray(big) && big.length > recs.length) { recs = big.slice(); prev = big; size = per = big.length; }
        } catch (e) { /* 쪽 크기를 못 바꾸는 서버 — 원래 크기로 받는다 */ }
      }
    }
    for (var n = 1; !end; n++) {
      if (ctl.stop) end = 'stop';
      else if (total != null && recs.length >= total) end = 'total';
      else if (prev.length < per) end = 'short';
      else if (recs.length >= MAX_RECORDS || n >= MAX_PULL) end = 'cap';
      if (end) break;
      say(label + ' — 목록 받는 중 ' + recs.length + (total != null ? '/' + total : '') + '건');
      var got;
      try { got = DATA.at(await resend(DATA.pageCall(found.call, pg, n, size)), found.path); }
      catch (e) { note('목록 ' + (n + 1) + '쪽을 받지 못했습니다: ' + clean(e && e.message, 80)); end = 'error'; break; }
      if (!Array.isArray(got) || !got.length) end = 'empty';
      else if (DATA.sameHead(got, prev)) end = 'repeat';
      else { recs = recs.concat(got); prev = got; }
    }
    return { records: recs.slice(0, MAX_RECORDS), end: end };
  }

  function press(el) {
    var w = el.ownerDocument.defaultView;
    if (el.tagName === 'TR' && el.getAttribute('ondblclick')) el.dispatchEvent(new w.MouseEvent('dblclick', { bubbles: true }));
    else el.click();
  }

  /* ---------------- 위치 찾기 — 녹화·탐색이 남긴 요소와 틀을 다시 찾는다 ----------------
   * 요소 하나를 여러 특징(태그·글자·id·name·title·onclick·href·class·XPath)으로 적어 두고, 다시 찾을 때는
   * 같은 태그의 후보마다 맞는 특징의 점수를 더해 가장 비슷한 것을 고른다(Similo·ROBULA+의 생각 — 화면이
   * 조금 바뀌어도 한 특징에 매달리지 않는다). 틀(iframe)은 위에서부터 차례로 이름·id·주소로 고른다. */
  function pathOnly(u) {
    if (!u || /^\s*(javascript:|#)/i.test(u)) return '';
    try { var x = new URL(u, location.href); return x.origin === origin ? x.pathname + x.search : ''; } catch (e) { return ''; }
  }
  function xpathOf(el) {
    var parts = [];
    for (var cur = el; cur && cur.nodeType === 1 && cur.tagName !== 'HTML'; cur = cur.parentElement) {
      var i = 1;
      for (var s = cur.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === cur.tagName) i++;
      parts.unshift(cur.tagName.toLowerCase() + '[' + i + ']');
    }
    return '/html/' + parts.join('/');
  }
  function clsOf(el) { return clean(typeof el.className === 'string' ? el.className : '', 120); }
  function locOf(el) {
    return { tag: el.tagName.toLowerCase(), text: clean(labelOf(el), 120), id: clean(el.id, 120),
             name: clean(el.getAttribute('name'), 120), title: clean(el.getAttribute('title'), 120),
             code: clean(el.getAttribute('onclick'), 200), href: pathOnly(el.getAttribute('href')),
             cls: clsOf(el), xpath: xpathOf(el) };
  }
  function scoreOf(el, loc) {
    return (loc.id && el.id === loc.id ? 5 : 0) + (loc.text && labelOf(el) === loc.text ? 4 : 0) +
      (loc.name && el.getAttribute('name') === loc.name ? 3 : 0) +
      (loc.code && clean(el.getAttribute('onclick'), 200) === loc.code ? 3 : 0) +
      (loc.title && el.getAttribute('title') === loc.title ? 2 : 0) +
      (loc.href && pathOnly(el.getAttribute('href')) === loc.href ? 2 : 0) +
      (loc.xpath && xpathOf(el) === loc.xpath ? 2 : 0) + (loc.cls && clsOf(el) === loc.cls ? 1 : 0);
  }
  /* 점수 4 이상(글자 하나가 같거나 id가 같거나…)만 — 그보다 못하면 엉뚱한 것을 누르느니 못 찾았다고 한다. */
  function locate(doc, loc) {
    var best = null, top = 3;
    Array.prototype.forEach.call(doc.getElementsByTagName(loc.tag || '*'), function (el) {
      if (box.contains(el)) return;
      var s = scoreOf(el, loc);
      if (s > top) { top = s; best = el; }
    });
    return best;
  }
  function frameLoc(f) {
    return { id: clean(f.id, 120), name: clean(f.name, 120), src: pathOnly(f.getAttribute('src')),
             index: Array.prototype.indexOf.call(f.ownerDocument.querySelectorAll('iframe,frame'), f) };
  }
  /* win이 root 창에서 몇 겹 아래 틀인가 — 위에서부터의 틀 특징 목록. */
  function framePathOf(win, rootWin) {
    var out = [];
    try {
      for (var w = win; w && w !== rootWin && w.frameElement; w = w.parent) out.unshift(frameLoc(w.frameElement));
    } catch (e) { /* 다른 출처 */ }
    return out;
  }
  function resolveFrame(root, path) {
    var cur = root;
    for (var i = 0; i < (path || []).length; i++) {
      var doc = docOf(cur), best = null, top = 0, p = path[i];
      if (!doc) return null;
      Array.prototype.forEach.call(doc.querySelectorAll('iframe,frame'), function (f, k) {
        var s = (p.id && f.id === p.id ? 5 : 0) + (p.name && f.name === p.name ? 4 : 0) +
          (p.src && pathOnly(f.getAttribute('src')) === p.src ? 3 : 0) + (k === p.index ? 1 : 0);
        if (s > top) { top = s; best = f; }
      });
      if (!best) return null;
      cur = best;
    }
    return cur;
  }
  /* 틀 아래의 모든 틀(자기 포함) — 탐색이 누르기 전과 뒤를 비교한다. */
  function frameTree(frame, depth, out) {
    out = out || [];
    out.push(frame);
    var d = docOf(frame);
    if (d && (depth || 0) < 4) {
      Array.prototype.forEach.call(d.querySelectorAll('iframe,frame'), function (f) { frameTree(f, (depth || 0) + 1, out); });
    }
    return out;
  }
  /* 새 창으로 연 것(window.open·새 창 폼)을 그 틀에 연다 — 메뉴·상세가 팝업인 사이트. */
  async function follow(c, frame) {
    if (c.kind === 'form') {
      var wait = loaded(frame);
      c.form.setAttribute('target', frame.name);
      c.submit.call(c.form);
      await wait;
      await settle(frame);
    } else if (same(c.url)) await load(frame, c.url);
  }
  /* 녹화·탐색이 남긴 길을 밟는다 — 단계마다 그 틀에서 요소를 찾아(뜰 때까지 기다린다) 누른다. */
  async function walkPath(root, steps) {
    for (var i = 0; i < steps.length; i++) {
      var s = steps[i], fr = null;
      var el = await until(function () {
        fr = s.frame && s.frame.length ? resolveFrame(root, s.frame) : root;
        var d = fr && docOf(fr);
        return d && d.body ? locate(d, s.target) : null;
      });
      if (!el) {
        var fd = fr && docOf(fr);
        repair = { kind: 'step', step: i, target: s.target, frame: s.frame || [], candidates: fd ? candidatesOf(fd) : [] };
        return '화면으로 가는 ' + (i + 1) + '번째 단계(' + (s.target.text || s.target.tag) + ')를 찾지 못했습니다' +
          (fd ? '' : ' — 그 단계의 틀도 보이지 않습니다');
      }
      if (risky(el)) return (i + 1) + '번째 단계는 무언가를 바꾸는 버튼이라 누르지 않았습니다: ' + labelOf(el);
      log('화면으로 가는 길 ' + (i + 1) + '/' + steps.length + ' · ' + (labelOf(el) || s.target.tag));
      arm(fr);
      mode = 'detail';
      caught = [];
      press(el);
      await settle(fr);
      mode = 'pass';
      if (caught.length) await follow(caught[0], root);
      caught = [];
    }
    return '';
  }

  /* 상대 날짜(@today-1m)를 그 칸의 모양으로 — 지금 값·안내 글이 2024.01.01이면 점으로, 20240101이면 붙여서. */
  function dateValue(el, value) {
    var m = /^@today(?:([+-]\d+)([dmy]))?$/i.exec(value), iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value), d;
    if (iso) d = new Date(+iso[1], +iso[2] - 1, +iso[3]);   /* 지난 수집일(@last) — 그 칸의 모양으로 */
    else if (!m) return value;
    else {
      var n = m[1] ? parseInt(m[1], 10) : 0, unit = (m[2] || '').toLowerCase();
      d = new Date();
      if (unit === 'd') d.setDate(d.getDate() + n);
      if (unit === 'm') d.setMonth(d.getMonth() + n);
      if (unit === 'y') d.setFullYear(d.getFullYear() + n);
    }
    var y = String(d.getFullYear()), mo = ('0' + (d.getMonth() + 1)).slice(-2), da = ('0' + d.getDate()).slice(-2);
    if (el.type === 'date') return y + '-' + mo + '-' + da;
    var cur = clean(el.value || el.getAttribute('placeholder') || '', 40);
    var sep = /\d{4}([.\-\/])\d{1,2}/.exec(cur) || /[yY]{4}([.\-\/])[mM]{2}/.exec(cur);
    if (sep) return y + sep[1] + mo + sep[1] + da;
    if (/^\d{8}$/.test(cur) || /^[yY]{4}[mM]{2}[dD]{2}$/.test(cur)) return y + mo + da;
    return y + '-' + mo + '-' + da;
  }
  /* 녹화한 날짜 값을 상대 날짜로 — 다시 돌 때 그날만큼 함께 옮겨 간다(오늘 → @today, 한 달 전 → @today-1m). */
  function relDate(v) {
    var m = /^(\d{4})[.\-\/]?(\d{2})[.\-\/]?(\d{2})$/.exec(String(v).trim());
    if (!m) return v;
    var d = new Date(+m[1], +m[2] - 1, +m[3]), t = new Date();
    t.setHours(0, 0, 0, 0);
    var days = Math.round((t - d) / 86400000);
    if (days === 0) return '@today';
    if (days < 0) return '@today+' + (-days) + 'd';
    var months = Math.round(days / 30.44), years = Math.round(days / 365.25), back = new Date(t);
    back.setFullYear(t.getFullYear() - years);
    if (years > 0 && back.getTime() === d.getTime()) return '@today-' + years + 'y';
    back = new Date(t);
    back.setMonth(t.getMonth() - months);
    if (months > 0 && back.getTime() === d.getTime()) return '@today-' + months + 'm';
    return '@today-' + days + 'd';
  }
  function setValue(el, value) {
    var w = el.ownerDocument.defaultView;
    if (el.tagName === 'SELECT') {
      var hit = -1, i;
      for (i = 0; i < el.options.length && hit < 0; i++) {
        if (clean(el.options[i].text, 80) === value || el.options[i].value === value) hit = i;
      }
      for (i = 0; i < el.options.length && hit < 0; i++) if (clean(el.options[i].text, 80).indexOf(value) >= 0) hit = i;
      if (hit < 0) return false;
      el.selectedIndex = hit;
    } else {
      var proto = el.tagName === 'TEXTAREA' ? w.HTMLTextAreaElement.prototype : w.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, dateValue(el, value));
    }
    ['input', 'change', 'blur'].forEach(function (t) { el.dispatchEvent(new w.Event(t, { bubbles: true })); });
    return true;
  }
  /* 조회 칸 — 이름(라벨)마다 화면 순서대로. 기간처럼 한 이름에 칸이 둘이면 조건도 두 번 온다. */
  function fieldMap(doc) {
    var map = {};
    doc.querySelectorAll('input,select,textarea').forEach(function (el) {
      var type = (el.getAttribute('type') || '').toLowerCase();
      if (/^(hidden|password|submit|button|image|reset|file|checkbox|radio)$/.test(type) || box.contains(el)) return;
      var label = fieldLabel(el);
      if (label) (map[label] = map[label] || []).push(el);
    });
    return map;
  }
  function hasFields(doc, q) {
    var map = fieldMap(doc);
    return q.conditions.length > 0 && q.conditions.every(function (c) { return map[c.field]; });
  }
  /* 이 틀에 그 화면이 떠 있는가 — 조건이 있으면 조건 칸이 다 있는지, 없으면 주소가 같은지. */
  function matches(frame, q) {
    var d = docOf(frame);
    if (!d || !d.body) return false;
    if (q.conditions.length) return hasFields(d, q);
    try { return winOf(frame).location.href.split('#')[0] === q.url.split('#')[0]; } catch (e) { return false; }
  }
  /* 껍데기 화면(탭·프레임셋) 안까지 — 그 화면이 떠 있는 틀. */
  function frameWithFields(frame, q) {
    var all = frameTree(frame, 0);
    for (var i = 0; i < all.length; i++) if (matches(all[i], q)) return all[i];
    return null;
  }
  /* 사람이 열어 둔 화면 — 이 창의 틀 중 그 화면이 떠 있는 것(맨 위 창은 뺀다: 상세로 이동하면 북마크가 끊긴다). */
  function liveFrame(q) {
    var tops = Array.prototype.filter.call(document.querySelectorAll('iframe,frame'), function (f) { return !ours(f); });
    for (var i = 0; i < tops.length; i++) {
      var hit = frameWithFields(tops[i], q);
      if (hit) return hit;
    }
    return null;
  }
  /* 조건 칸을 못 찾았을 때 무엇이 열렸는지 — 다음 시도에서 원인을 바로 안다. */
  function describe(frame) {
    var d = docOf(frame), names = d ? Object.keys(fieldMap(d)).slice(0, 8) : [], where = '?';
    try { where = winOf(frame).location.pathname; } catch (e) { /* 다른 출처 */ }
    return '열린 화면 ' + where + ' "' + clean(d && d.title, 40) + '" · 칸 ' + (names.join(', ') || '없음') +
      ' · 안의 틀 ' + (d ? d.querySelectorAll('iframe,frame').length : 0) + '개';
  }
  function findSearch(doc) {
    var hits = clickables(doc).filter(function (el) { return isSearch(labelOf(el)) && !risky(el); });
    return hits.filter(visible)[0] || hits[0] || null;
  }
  function resultRows(doc) {
    var best = [];
    doc.querySelectorAll('table').forEach(function (t) {
      var rows = [];
      t.querySelectorAll('tr').forEach(function (tr) {
        if (tr.closest('table') !== t || tr.querySelectorAll('td').length < 2) return;
        if (tr.querySelector('input:not([type=checkbox]):not([type=radio]):not([type=hidden]),select,textarea')) return;
        rows.push(tr);
      });
      if (rows.length > best.length) best = rows;
    });
    return best;
  }
  function rowLabel(tr) {
    return Array.prototype.slice.call(tr.querySelectorAll('td'), 0, 4).map(textOf)
      .filter(function (t) { return t; }).join(' · ').slice(0, 120);
  }
  function isAttachment(el) {
    var t = labelOf(el), href = el.getAttribute('href') || '';
    return FILE_NAME.test(t) || FILE_EXT.test(href.split('?')[0]) || /down|file|attach/i.test(codeOf(el)) ||
      (t.length <= 12 && ATTACH.test(t));
  }
  function attachments(doc) {
    return clickables(doc).filter(function (el) { return isAttachment(el) && !risky(el); });
  }
  /* 행의 상세 — 녹화가 보여 준 칸(몇 번째 칸의 어떤 요소)이 먼저, 없으면 글자·모양으로 고른다. */
  function detailOf(tr, hint) {
    var ok = function (el) { return !risky(el) && !isAttachment(el); };
    if (hint && hint.col >= 0) {
      var cell = tr.children[hint.col];
      var pick = cell && (cell.matches && cell.matches(hint.tag) ? cell : cell.querySelector(hint.tag));
      if (pick && ok(pick)) return pick;
    }
    var els = clickables(tr).filter(ok);
    var named = els.filter(function (el) { return DETAIL.test(labelOf(el)); })[0];
    if (named) return named;
    var title = els.filter(function (el) { return el.tagName === 'A' && labelOf(el).length > 1; })[0];
    if (title) return title;
    if (els.length) return els[0];
    if (tr.getAttribute('onclick') || tr.getAttribute('ondblclick')) return risky(tr) ? null : tr;
    /* 행 클릭을 스크립트로 단 그리드 — 제목 칸을 눌러 본다(아무 일도 없으면 그뿐이다). */
    return tr.querySelectorAll('td')[1] || null;
  }
  /* LLM 분석이 본문에서 읽은 버튼 글자(q.hints) — 녹화가 남긴 위치 다음, 모양으로 고르기 전에 쓴다. */
  function byLabel(doc, text) {
    var want = String(text || '').replace(/\s+/g, '');
    if (!want) return null;
    return clickables(doc).filter(function (el) {
      return labelOf(el).replace(/\s+/g, '') === want && !risky(el);
    })[0] || null;
  }
  function exportButton(doc, rows, q) {
    if (q.export) {
      var known = locate(doc, q.export);
      if (known && !risky(known)) return known;
    }
    var hinted = byLabel(doc, (q.hints || {}).export);
    if (hinted && !rows.some(function (tr) { return tr.contains(hinted); })) return hinted;
    return clickables(doc).filter(function (el) {
      var t = labelOf(el);
      return t.length <= 20 && EXPORT.test(t) && !NOT_EXPORT.test(t) && !risky(el) &&
        !rows.some(function (tr) { return tr.contains(el); });
    })[0] || null;
  }

  /* 잡은 요청을 같은 모양으로 다시 보낸다 — 파일만 싣는다(화면이 오면 버린다). */
  async function replay(c) {
    if (c.early) return c.early;
    if (c.kind === 'get') return /^https?:/.test(c.url) && !same(c.url) ? null : fetch(c.url, { credentials: 'same-origin' });
    if (c.kind === 'post') return same(c.url) ? fetch(c.url, { method: 'POST', credentials: 'same-origin', body: c.body }) : null;
    var form = c.form, w = form.ownerDocument.defaultView;
    var action = new URL(form.getAttribute('action') || w.location.href, w.location.href).href;
    if (!same(action)) return null;
    var data = new w.FormData(form);
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') {
      var u = new URL(action);
      data.forEach(function (v, k) { if (typeof v === 'string') u.searchParams.append(k, v); });
      return fetch(u.href, { credentials: 'same-origin' });
    }
    var body = /multipart/i.test(form.enctype || '') ? data : new URLSearchParams(data);
    return fetch(action, { method: 'POST', credentials: 'same-origin', body: body });
  }
  /* 첨부·엑셀 하나를 누르고 잡은 것을 받는다. 같은 주소로 여러 파일이 오므로(폼 전송) 이름으로 가른다. */
  async function grab(el, frame, files, budget, meta) {
    var list = [], href = el.tagName === 'A' ? (el.getAttribute('href') || '') : '';
    if (href && !/^\s*(javascript:|#)/i.test(href) && same(el.href)) list.push({ kind: 'get', url: el.href });
    else {
      arm(frame);
      mode = 'file';
      caught = [];
      press(el);
      for (var t0 = Date.now(); !caught.length && Date.now() - t0 < CATCH_MS;) await sleep(100);
      mode = 'pass';
      list = caught;
      caught = [];
    }
    var got = 0;
    for (var i = 0; i < list.length && i < 3; i++) {
      try {
        var res = await replay(list[i]);
        if (!res || !res.ok) continue;
        var name = list[i].name || dispositionName(res) || (FILE_NAME.test(labelOf(el)) ? labelOf(el).replace(/\s*[(\[].*$/, '') : '');
        /* 내려받기 주소 하나(download.do)로 행마다 다른 파일이 온다 — 행과 이름을 붙여 가른다. */
        var base = same(res.url || '') || same(list[i].url || '') || meta.page;
        var url = base + '#' + encodeURIComponent(meta.text + ' / ' + (name || String(files.length)));
        if (await addFile(files, res, { url: url, fixed: true, name: name, text: meta.text, page: meta.page }, budget)) got++;
      } catch (e) { /* 받지 못한 파일은 건너뛴다 */ }
    }
    if (!got) note('파일을 받지 못함: ' + clean(labelOf(el), 60));
    else log('받음 ' + files.slice(-got).map(function (f) { return f.name || f.text; }).join(', '));
    return got;
  }

  /* 그 화면을 연다(위 ①②③). 이미 떠 있으면 그대로, 상세로 옮겨 갔으면 뒤로 한 번 — 그래도 아니면 다시 연다. */
  async function openScreen(q, t) {
    if (t.frame && matches(t.frame, q)) return '';
    if (t.frame) {
      try { winOf(t.frame).history.back(); } catch (e) { /* 뒤로 갈 곳이 없다 */ }
      await settle(t.frame);
      if (matches(t.frame, q)) return '';
      if (t.live) return '상세에서 원래 화면으로 돌아오지 못했습니다 — 화면을 다시 열어 두고 북마크를 누르세요';
    }
    if (q.path && q.path.length) {
      log(q.screen + ' — 녹화·탐색한 길로 화면을 여는 중');
      await load(t.root, q.start || q.url.split('#')[0]);
      var why = await walkPath(t.root, q.path);
      if (why) return why;
      t.frame = await until(function () {
        var f = q.frame && q.frame.length ? resolveFrame(t.root, q.frame) : null;
        return f && matches(f, q) ? f : frameWithFields(t.root, q);
      });
      if (!t.frame) {
        var last = (q.frame && q.frame.length && resolveFrame(t.root, q.frame)) || t.root;
        return '길을 밟았지만 조건 칸이 보이지 않습니다 — ' + describe(last);
      }
      return '';
    }
    await load(t.root, t.url);
    t.frame = frameWithFields(t.root, q);
    if (!t.frame) {
      if (!docOf(t.root) || !docOf(t.root).body) return '화면을 틀 안에 열지 못했습니다(사이트가 막아 두었을 수 있음)';
      return (q.conditions.length ? '조건 칸을 찾지 못했습니다: ' + q.conditions.map(function (c) { return c.field; }).join(', ') + ' — '
        : '그 화면이 아닙니다 — ') + describe(t.root) +
        '. 메뉴로만 열리는 화면이면 화면을 열어 둔 채 북마크를 누르거나, 「녹화」로 가는 길을 보여 주세요';
    }
    return '';
  }

  /* 조건을 채우고 조회한다 — 실패하면 그 까닭을 돌려준다. */
  async function search(q, t) {
    var why = await openScreen(q, t);
    if (why) return why;
    var doc = docOf(t.frame);
    var map = fieldMap(doc), used = {}, missing = [];
    q.conditions.forEach(function (c) {
      var n = used[c.field] || 0, el = (map[c.field] || [])[n], value = since(c.value, q, t);
      used[c.field] = n + 1;
      if (!el || !value) { if (!el) missing.push(c.field); return; }
      if (!setValue(el, value)) missing.push(c.field + '=' + value);
    });
    if (missing.length) {
      repair = { kind: 'fields', missing: missing.filter(function (m) { return m.indexOf('=') < 0; }),
                 seen: Object.keys(map).slice(0, 40) };
    }
    if (missing.length && missing.length === q.conditions.length) {
      return '조건 칸을 찾지 못했습니다: ' + missing.join(', ') + ' — ' + describe(t.frame);
    }
    if (missing.length) note('채우지 못한 조건: ' + missing.join(', '));
    var btn = (q.search && locate(doc, q.search)) || byLabel(doc, (q.hints || {}).search) || findSearch(doc);
    if (!btn || risky(btn)) return '조회 버튼을 찾지 못했습니다 — ' + describe(t.frame);
    arm(t.frame);
    mode = 'pass';
    /* 조회가 보내는 요청을 적는다 — 버튼이 있는 틀이 아니라 바깥 틀의 함수로 보내는 사이트도 있어 틀마다 건다. */
    frames(window, 0, []).forEach(function (f) { tap(f.win); });
    calls = [];
    listening = true;
    press(btn);
    await settle(t.frame);
    await sleep(300);   /* 화면이 다 그려진 뒤에 오는 응답 */
    listening = false;
    t.calls = calls.splice(0);
    return '';
  }
  /* 증분 — @last는 이 화면을 지난번에 빠짐없이 받은 날(서버가 수집 조건에 실어 보낸다), 처음이면 | 뒤의 값
   * (@last|@today-3y). 그날부터 받아 겹치는 것은 서버가 키로 합친다. */
  function since(value, q, t) {
    var m = /^@last(?:\|(.*))?$/i.exec(String(value || '').trim());
    if (!m) return value;
    if (q.last) { t.since = q.last; return q.last; }
    return (m[1] || '').trim();
  }

  /* 결과 파일 — 엑셀 버튼이 있으면 그것으로, 없거나 받지 못하면 결과 표를 CSV로. */
  async function exportResult(q, work, files, budget) {
    var doc = docOf(work), rows = resultRows(doc), btn = exportButton(doc, rows, q);
    var meta = { text: q.screen + ' — 결과', page: q.url.split('#')[0] };
    if (btn && await grab(btn, work, files, budget, meta)) return;
    if (!rows.length) return;
    var table = rows[0].closest('table'), head = [];
    table.querySelectorAll('th').forEach(function (th) { head.push(textOf(th)); });
    var lines = (head.length ? [head] : []).concat(rows.map(function (tr) {
      return Array.prototype.map.call(tr.querySelectorAll('td'), textOf);
    })).map(function (cells) {
      return cells.map(function (v) { return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }).join(',');
    });
    var name = clean(q.screen, 60).replace(/[\\/:*?"<>|]+/g, '_') + ' 결과.csv';
    var blob = new Blob(['\uFEFF' + lines.join('\r\n')], { type: 'text/csv' });
    if (await addBlob(files, blob, { url: meta.page + '#' + encodeURIComponent(name), name: name, text: meta.text,
                                     page: meta.page, type: 'text/csv' }, budget)) log('받음 ' + name + '(결과 표)');
  }

  /* 한 행의 상세 — 새 창으로 열면 상세 틀에, 같은 틀이 바뀌면 그 화면에서, 같은 화면에 뜨면(팝업 레이어)
   * 누르기 전에 없던 첨부만 받는다. 같은 틀이 바뀌었으면 돌아와 다시 조회해야 한다(navigated). */
  async function openDetail(tr, q, work, detail, files, budget) {
    var trigger = detailOf(tr, q.detail);
    if (!trigger) return { opened: false, navigated: false };
    var doc = tr.ownerDocument, label = rowLabel(tr), before = attachments(doc);
    arm(work);
    mode = 'detail';
    caught = [];
    press(trigger);
    await settle(work);
    mode = 'pass';
    var popups = caught;
    caught = [];
    var navigated = docOf(work) !== doc, target = null, frame = work;
    if (popups.length) {
      await follow(popups[0], detail);
      frame = detail;
      target = docOf(detail);
    } else target = docOf(work);
    if (!target || !target.body) return { opened: false, navigated: navigated };
    var atts = attachments(target).filter(function (el) { return frame !== work || navigated || before.indexOf(el) < 0; });
    var opened = popups.length > 0 || navigated || atts.length > 0;
    var meta = { text: label, page: q.url.split('#')[0] };
    for (var i = 0; i < atts.length && i < MAX_ATTACH && files.length < MAX_FILES && budget.left > 0 && !ctl.stop; i++) {
      say(q.screen + ' — 첨부 받는 중 ' + (files.length + 1) + '…');
      await grab(atts[i], frame, files, budget, meta);
      if (frame === work && docOf(work) !== target) navigated = true;
    }
    if (frame === detail) detail.src = 'about:blank';
    else if (!navigated) {
      var close = clickables(target).filter(function (el) {
        return CLOSE.test(labelOf(el)) && visible(el) && before.indexOf(el) < 0 && !risky(el);
      })[0];
      if (close) press(close);
      else target.dispatchEvent(new target.defaultView.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    }
    return { opened: opened, navigated: navigated };
  }

  /* 결과 목록 — 조회 응답에서 화면 표의 목록을 찾아(lib/scanData.js) 전체를 받는다. 찾지 못하면 null — 그때는
   * 엑셀·표로 첫 쪽을 받는다. 다 받았는지(complete)는 전체 건수와 키로 센다(건수가 없으면 끝 쪽까지 갔는지로). */
  async function listData(q, t, rows) {
    var table = rows.length ? rows[0].closest('table') : null, head = [];
    var grid = rows.slice(0, 50).map(function (tr) { return Array.prototype.map.call(tr.querySelectorAll('td'), textOf); });
    if (table) table.querySelectorAll('th').forEach(function (th) { head.push(textOf(th)); });
    var found = DATA.findList(t.calls || [], [].concat.apply([], grid));
    if (!found) {
      note('조회 응답에서 결과 목록을 찾지 못했습니다(요청 ' + (t.calls || []).length + '개) — 엑셀·화면 표로 받습니다');
      return null;
    }
    var path = new URL(found.call.url).pathname;
    if (MUTATE_CODE.test(path)) {
      note('목록 요청 주소가 무언가를 바꾸는 이름이라(' + path + ') 다시 보내지 않고 첫 쪽만 씁니다');
      found.paging = null;
    }
    var got = await pullAll(found, q.screen), cols = DATA.columns(got.records, head, grid);
    var want = q.key ? cols.filter(function (c) { return c.label === q.key || c.key === q.key; })[0] : null;
    var key = DATA.keyOf(got.records, want && want.key), recs = got.records.map(DATA.flat), seen = {}, uniq = 0;
    recs.forEach(function (r) {
      var k = key ? String(r[key]) : JSON.stringify(r);
      if (!seen[k]) { seen[k] = 1; uniq++; }
    });
    var complete = found.total != null ? uniq >= found.total : /^(short|empty|one)$/.test(got.end);
    var pg = found.paging;
    var rep = { via: 'api', got: uniq, total: found.total, complete: complete, end: got.end, key: key,
                since: t.since || '',
                api: { method: found.call.method, path: path, list: found.path.join('.'),
                       page: pg ? pg.page.path.join('.') : '', size: pg && pg.size ? pg.size.path.join('.') : '',
                       params: DATA.params(found.call).map(function (x) { return x.path.join('.'); }).slice(0, 40) } };
    log(q.screen + ' — 조회 응답으로 목록 ' + uniq + '건' + (found.total != null ? ' / 전체 ' + found.total + '건' : '') +
        (complete ? ' (빠짐없음)' : ' (덜 받음: ' + got.end + ')'));
    if (!complete) note('목록을 다 받지 못했습니다 — ' + uniq + '건' + (found.total != null ? ' / 전체 ' + found.total : '') +
                        ' · 멈춘 까닭 ' + got.end);
    return { rep: rep, set: { screen: q.screen, url: q.url, key: key, columns: cols, records: recs,
                              total: found.total, complete: complete, since: t.since || '' } };
  }

  async function collectScreen(q, work, detail, files, budget, sets) {
    var rep = { screen: q.screen, url: q.url, rows: 0, details: 0, files: 0, notes: [], via: '' };
    notes = rep.notes;
    repair = null;
    var live = ctl.fresh ? null : liveFrame(q), start = files.length;
    /* 맨 위 창에 그 화면이 떠 있으면(틀 없는 사이트) 그 주소를 숨긴 틀에 연다. */
    var t = { root: work, live: !!live, frame: live,
              url: !live && hasFieldsTop(q) ? location.href.split('#')[0] : q.url.split('#')[0] };
    rep.via = live ? 'open' : q.path && q.path.length ? 'path' : 'url';
    log(q.screen + ' — ' + (live ? '열려 있는 화면에서' : rep.via === 'path' ? '녹화·탐색한 길로' : '주소로') + ' 수집합니다');
    say(q.screen + ' — 조건을 채우고 조회하는 중');
    var why = await search(q, t);
    if (repair) rep.repair = repair;
    if (why) { note(why); notes = null; return rep; }
    var rows = resultRows(docOf(t.frame));
    rep.rows = rows.length;
    log(q.screen + ' — 결과 ' + rows.length + '행');
    var list = await listData(q, t, rows);
    if (list) { rep.data = list.rep; sets.push(list.set); }
    /* 목록을 빠짐없이 받았으면 엑셀은 받지 않는다 — 서버가 누적한 목록으로 전체 표를 만든다. */
    if (!list || !list.rep.complete) await exportResult(q, t.frame, files, budget);
    /* 한도(ctl.rows)는 도는 동안 바뀔 수 있다 — 행마다 다시 본다. */
    var i = 0;
    for (; i < Math.min(rep.rows, ctl.rows) && !ctl.stop; i++) {
      if (files.length >= MAX_FILES || budget.left <= 0) { note('파일 상한에 닿아 멈췄습니다'); break; }
      rows = docOf(t.frame) && matches(t.frame, q) ? resultRows(docOf(t.frame)) : [];
      if (rows.length <= i) {
        why = await search(q, t);
        rows = why ? [] : resultRows(docOf(t.frame));
        if (why) note(why);
      }
      if (rows.length <= i) { note((i + 1) + '번째 행을 다시 찾지 못해 멈췄습니다'); break; }
      say(q.screen + ' — 상세 ' + (i + 1) + '/' + Math.min(rep.rows, ctl.rows) + ' · ' + rowLabel(rows[i]));
      var got = await openDetail(rows[i], q, t.frame, detail, files, budget);
      if (got.opened) rep.details++;
    }
    if (ctl.stop) note('사람이 멈췄습니다 — 결과 ' + rep.rows + '행 중 ' + i + '행까지 봤습니다');
    else if (rep.rows > i) note('결과 ' + rep.rows + '행 중 앞 ' + i + '행만 상세를 열었습니다(조회 한도)');
    /* 사람의 화면에서 했으면 상세에 머물지 않게 원래 화면으로 — 마지막 행이 화면을 옮겼을 수 있다. */
    if (t.live && !matches(t.frame, q)) {
      try { winOf(t.frame).history.back(); } catch (e) { /* 뒤로 갈 곳이 없다 */ }
    }
    rep.files = files.length - start;
    notes = null;
    return rep;
  }
  function hasFieldsTop(q) { return q.conditions.length > 0 && hasFields(document, q); }

  async function collect(files, budget, sets) {
    var queries = SOURCE.queries || [], out = [];
    if (!queries.length) return out;
    var work = makeFrame('gpax-work'), detail = makeFrame('gpax-detail');
    try {
      for (var i = 0; i < queries.length && !ctl.stop; i++) {
        out.push(await collectScreen(queries[i], work, detail, files, budget, sets));
      }
    } finally {
      disarmAll();
      untapAll();
      work.remove();
      detail.remove();
    }
    return out;
  }

  /* ---------------- 메뉴 탐색 — 스크립트로 여는 메뉴를 숨긴 틀에서 눌러 보고, 조회 화면과 그리로 가는 길을 적는다 ----------------
   * 상태 탐색 크롤링(Crawljax·crawlergo)의 생각을 좁게 쓴다: 누르는 것은 메뉴 트리의 잎 항목 중 주소가 없는 것
   * (스크립트·POST로 여는 메뉴 — 주소가 있는 메뉴는 위에서 GET으로 이미 읽는다)이고, 무언가를 바꾸는 글자는 뺀다.
   * 누른 뒤 새로 뜨거나 바뀐 틀에 조회 칸·표가 있으면 그 화면을 길(누른 메뉴·그 화면의 틀)과 함께 남긴다.
   * 한 번에 MAX_EXPLORE개, 하나씩 가라앉기를 기다리며. */
  var MAX_EXPLORE = 40;
  function menuLeaves(doc) {
    var out = [];
    doc.querySelectorAll('nav,[role],[id],[class]').forEach(function (nav) {
      if (!isNav(nav) || inNav(nav.parentElement)) return;
      nav.querySelectorAll('li').forEach(function (li) {
        if (li.querySelector('ul,ol')) return;
        var a = ownAnchor(li), el = a || li;
        var href = a ? a.getAttribute('href') || '' : '';
        if (same(href) && !/^\s*#/.test(href)) return;
        if (!el.getAttribute('onclick') && !(a && /^\s*(javascript:|#)/i.test(href)) && !li.getAttribute('onclick')) return;
        if (!labelOf(el) || risky(el) || out.indexOf(el) >= 0) return;
        out.push(el);
      });
    });
    return out;
  }
  async function explore(pages, seen) {
    var root = makeFrame('gpax-explore'), probe = null;
    try {
      await load(root, location.href.split('#')[0]);
      var leaves = [];
      frameTree(root, 0).forEach(function (f) {
        var d = docOf(f);
        if (d) menuLeaves(d).forEach(function (el) { leaves.push({ el: el, frame: framePathOf(d.defaultView, winOf(root)) }); });
      });
      if (!leaves.length) { log('탐색 · 스크립트로 여는 메뉴가 없습니다'); return 0; }
      var found = 0, n = Math.min(leaves.length, MAX_EXPLORE);
      for (var i = 0; i < n && !ctl.stop; i++) {
        var leaf = leaves[i], fr = leaf.frame.length ? resolveFrame(root, leaf.frame) : root;
        var d = fr && docOf(fr);
        var el = d && locate(d, locOf(leaf.el));
        if (!el) continue;
        var step = { frame: leaf.frame, target: locOf(el) };
        say('메뉴 탐색 ' + (i + 1) + '/' + n + ' · ' + labelOf(el));
        var before = frameTree(root, 0).map(function (f) { return { f: f, d: docOf(f) }; });
        arm(fr);
        mode = 'detail';
        caught = [];
        press(el);
        await settle(fr);
        mode = 'pass';
        var opened = caught.splice(0), changed;
        if (opened.length) {
          /* 새 창으로 여는 메뉴 — 따로 둔 틀에 열어 읽는다. 다시 밟을 때도 그 창을 숨긴 틀에 연다(walkPath). */
          probe = probe || makeFrame('gpax-probe');
          await follow(opened[0], probe);
          changed = [probe];
        } else {
          await sleep(300);
          changed = frameTree(root, 0).filter(function (f) {
            var old = before.filter(function (b) { return b.f === f; })[0];
            return !old || old.d !== docOf(f);
          });
        }
        var screen = changed.filter(function (f) { var x = docOf(f); return x && x.body && Object.keys(fieldMap(x)).length; })[0] ||
          changed.filter(function (f) { var x = docOf(f); return x && x.body && x.querySelector('table th'); })[0];
        if (!screen) continue;
        var sw = winOf(screen), p = readDoc(docOf(screen), same(sw.location.href) || location.href.split('#')[0], sw);
        var name = p.title || p.headings[0] || labelOf(el);
        if (!p.title || p.title === document.title) p.title = labelOf(el);
        if (seen[p.url + '|' + p.title]) continue;
        seen[p.url + '|' + p.title] = 1;
        if (seen[p.url]) p.url += '#' + encodeURIComponent(p.title || name);
        seen[p.url] = 1;
        p.path = [step];
        p.start = location.href.split('#')[0];
        p.frame = screen === root || screen === probe ? [] : framePathOf(sw, winOf(root));
        pages.push(p);
        found++;
        log('탐색 · ' + labelOf(el) + ' → ' + p.title + ' (조회 칸 ' + p.fields.length + '개)');
      }
      return found;
    } finally {
      disarmAll();
      root.remove();
      if (probe) probe.remove();
    }
  }

  /* ---------------- 녹화 — 사람이 한 번 보여 준 길(메뉴 → 조건 → 조회 → 첫 행의 상세 → 첨부)을 적는다 ----------------
   * 프로그래밍 바이 데모(Rousillon·WebRobot)의 생각: 사람이 한 번 하면 첫 행에 한 일을 '모든 행'으로 넓힌다.
   * 적는 것은 누른 요소의 특징·그 요소가 있던 틀의 경로, 그리고 조회를 누른 순간의 조건 칸 값뿐이다(비밀번호
   * 칸은 읽지 않는다). 녹화 중에는 아무것도 막지 않는다 — 사람이 실제로 하는 일이다. 다시 밟을 때 무언가를
   * 바꾸는 버튼은 빼고 밟는다. 결과는 JSON 파일로 내려받아(콘솔이 연 탭이면 콘솔로 바로) 수집 조건에 올린다. */
  var RECORD_AGENT = 'gpax-record/1';
  var rec = null;
  function recAttach() {
    if (!rec) return;
    frames(window, 0, []).forEach(function (f) {
      try {
        if (f.doc.__gpaxRec || (f.win.frameElement && ours(f.win.frameElement))) return;
        f.doc.__gpaxRec = true;
        f.doc.addEventListener('click', function (e) { recClick(f.win, e); }, true);
      } catch (e) { /* 다른 출처 */ }
    });
  }
  function snapshot(doc) {
    var out = [], map = fieldMap(doc);
    Object.keys(map).forEach(function (label) {
      map[label].forEach(function (el) {
        var v = el.tagName === 'SELECT' ? clean(el.options[el.selectedIndex] && el.options[el.selectedIndex].text, 80)
          : clean(el.value, 200);
        if (v) out.push({ field: label, value: relDate(v) });
      });
    });
    return out;
  }
  function recClick(win, e) {
    if (!rec || box.contains(e.target)) return;
    var el = (e.target.closest && e.target.closest(CLICKABLE + ',li,td')) || e.target;
    var cell = el.closest && el.closest('td,th'), tr = el.closest && el.closest('tr');
    var inRow = !!(tr && tr.querySelectorAll('td').length >= 2 && resultRows(win.document).indexOf(tr) >= 0);
    var t = labelOf(el);
    rec.steps.push({
      frame: framePathOf(win, window), target: locOf(el), label: t, url: win.location.href.split('#')[0],
      title: clean(win.document.title, 120), risky: risky(el), attach: isAttachment(el),
      search: isSearch(t), export: t.length <= 20 && EXPORT.test(t) && !NOT_EXPORT.test(t),
      row: inRow ? { col: Array.prototype.indexOf.call(tr.children, cell), tag: el.tagName.toLowerCase() } : null,
      fields: Object.keys(fieldMap(win.document)).length ? snapshot(win.document) : null,
    });
    log('녹화 · ' + (t || el.tagName.toLowerCase()) + (risky(el) ? ' (다시 밟을 때 뺌)' : ''));
  }
  function sameFrame(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  /* 녹화를 수집 조건 하나로 — 조회를 누른 곳이 그 화면, 그 앞의 다른 틀 누름이 화면으로 가는 길. */
  function recipeOf(steps) {
    var detailAt = -1, si = -1, i;
    for (i = 0; i < steps.length; i++) {
      if (steps[i].search && !steps[i].risky && detailAt < 0) si = i;
      if (si >= 0 && i > si && steps[i].row && !steps[i].attach && !steps[i].search && !steps[i].export) { detailAt = i; break; }
    }
    if (si < 0) {
      /* 조회 글자가 없는 버튼(아이콘) — 상세를 누르기 바로 전에 조건 칸이 있던 화면에서 누른 것. */
      for (i = 0; i < steps.length; i++) if (steps[i].row && !steps[i].attach) { detailAt = i; break; }
      /* 상세까지 누르지 않고 끝냈으면 — 마지막으로 조건 칸이 있던 화면에서 누른 것. */
      for (i = (detailAt < 0 ? steps.length : detailAt) - 1; i >= 0 && si < 0; i--) {
        if (steps[i].fields && !steps[i].row && !steps[i].risky) si = i;
      }
    }
    if (si < 0) return null;
    var s = steps[si];
    var nav = steps.slice(0, si).filter(function (x) { return !x.risky && !sameFrame(x.frame, s.frame); });
    var path = nav.map(function (x) { return { frame: x.frame, target: x.target }; });
    var after = steps.slice(si + 1);
    var exp = after.filter(function (x) { return x.export && !x.row && !x.risky && sameFrame(x.frame, s.frame); })[0];
    return {
      /* 화면 이름은 마지막으로 누른 메뉴 — 껍데기 사이트는 화면마다 제목(title)이 같다. */
      screen: (nav.length && nav[nav.length - 1].label) || s.title || s.url, url: s.url, start: rec.start, frame: s.frame, path: path,
      conditions: s.fields || [], search: s.target,
      detail: detailAt >= 0 ? steps[detailAt].row : null, export: exp ? exp.target : null,
      shown: { detail: detailAt >= 0, attach: after.some(function (x) { return x.attach; }) },
    };
  }
  function startRecording() {
    started = true;
    rec = { start: location.href.split('#')[0], steps: [], timer: setInterval(recAttach, 500) };
    recAttach();
    bar.textContent = '';
    var end = button('녹화 끝', finishRecording, true);
    var cancel = button('취소', function () { clearInterval(rec.timer); rec = null; say('녹화를 취소했습니다'); finish(); });
    bar.appendChild(end);
    bar.appendChild(cancel);
    say('녹화 중 — 메뉴를 눌러 화면을 열고, 조건을 넣고, 조회 → 첫 행의 상세 → 첨부를 누른 뒤 「녹화 끝」');
  }
  function finishRecording() {
    clearInterval(rec.timer);
    var recipe = recipeOf(rec.steps), n = rec.steps.length;
    var labels = rec.steps.map(function (x) { return clean(x.label, 30) || '(글자 없음)'; });
    rec = null;
    if (!recipe) {
      say('조회를 누른 화면을 찾지 못했습니다(누름 ' + n + '번: ' + labels.join(' → ') + ') — 조건 칸이 있는 화면에서 ' +
          '조회 버튼을 눌렀는지 보고 다시 녹화하세요');
      finish();
      return;
    }
    var out = { agent: RECORD_AGENT, source: { id: SOURCE.id, name: SOURCE.name }, origin: origin,
                at: new Date().toISOString(), recipe: recipe };
    var sent = deliver('record', out, 'gpax-record');
    say('녹화 끝 — 「' + recipe.screen + '」 가는 길 ' + recipe.path.length + '단계, 조건 ' + recipe.conditions.length +
        '개' + (recipe.shown.detail ? ', 상세' : '') + (recipe.shown.attach ? '·첨부' : '') +
        (sent ? '. 콘솔로 보냈습니다 — 콘솔이 수집 조건에 올립니다.'
          : '. 내려받은 파일을 GPAX 콘솔 → ' + SOURCE.name + ' → ① 북마크 추가 → 「콘솔 없이 쓸 때」에 올리세요(수집 조건에 더해집니다).'));
    finish();
  }

  /* 봇 실행 — 화면을 다시 읽지 않고 수집 조건(녹화한 길)대로만 다시 한다. 서버는 화면 목록을 지난 스캔
   * 그대로 두고 받은 파일·수집 결과만 바꾼다(replay). */
  async function botRun() {
    started = running = true;
    ctl.stop = false;
    ctl.fresh = true;
    stopBtn.disabled = false;
    stopBtn.style.opacity = '';
    controls();
    say('봇 실행 — 수집 조건 ' + (SOURCE.queries || []).length + '개를 녹화한 길대로 처음부터 다시 합니다');
    try {
      var files = [], sets = [], collected = await collect(files, { left: MAX_TOTAL_BYTES }, sets);
      var out = { agent: AGENT, replay: true, source: { id: SOURCE.id, name: SOURCE.name }, origin: origin,
                  url: location.href.split('#')[0], at: new Date().toISOString(), menu: [], pages: [],
                  files: files, collected: collected, datasets: sets, queries_version: SOURCE.version || '' };
      var sent = deliver('scan', out, 'gpax-scan');
      say('봇 끝 — ' + collected.map(function (c) {
        return c.screen + (c.data ? ' 목록 ' + c.data.got + '건' + (c.data.complete ? '(빠짐없음)' : '(덜 받음)')
          : ' 결과 ' + c.rows + '행') + ' · 상세 ' + c.details + ' · 파일 ' + c.files;
      }).join(' / ') + (sent ? '. 콘솔로 보냈습니다.' : '. 내려받은 파일을 콘솔에 올리세요.'));
    } catch (err) {
      say('봇 실패 — ' + (err && err.message));
    } finally {
      ctl.fresh = running = false;
      finish();
    }
  }

  async function run() {
    started = running = true;
    var pages = [], seen = {}, menu = [], menuN = 0;
    box.remove();   /* 열린 화면을 읽는 동안 이 창의 글이 섞이지 않게 — 다음 say가 다시 붙인다 */
    frames(window, 0, []).forEach(function (f) {
      if (f.win.frameElement && ours(f.win.frameElement)) return;
      var m = domMenu(f.doc); var d = dhxMenu(f.win);
      if (count(d) > count(m)) m = d;
      if (count(m) > menuN) { menu = m; menuN = count(m); }
      var p = readDoc(f.doc, f.url, f.win);
      var name = p.title || p.headings[0] || '';
      if (!p.text && !p.fields.length || seen[p.url + '|' + name]) return;
      seen[p.url + '|' + name] = 1;
      /* 메뉴를 POST로 여는 사이트는 탭마다 주소가 같다 — 화면 이름을 붙여 구분한다. */
      if (seen[p.url]) p.url += '#' + encodeURIComponent(name || String(pages.length));
      seen[p.url] = 1;
      pages.push(p);
    });
    /* 다 읽었으면 바로 창을 다시 붙인다 — 다음 일(메뉴 탐색 등)이 화면을 여는 몇 초 동안 창이 비면 멈춘 줄 안다. */
    say('열린 화면 ' + pages.length + '장을 읽었습니다' + (menuN ? ' · 메뉴 ' + menuN + '개' : ''));
    var targets = [];
    (function walk(items) {
      items.forEach(function (m) { if (m.url) targets.push({ url: m.url, text: m.label }); walk(m.children || []); });
    })(menu);
    pages.forEach(function (p) { p.links.forEach(function (l) { if (l.nav) targets.push(l); }); });
    var fetched = 0, files = [], links = [], budget = { left: MAX_TOTAL_BYTES };
    for (var i = 0; i < targets.length && fetched < MAX_FETCH && !ctl.stop; i++) {
      var t = targets[i];
      if (FILE_EXT.test(new URL(t.url).pathname)) { links.push(t); continue; }
      if (seen[t.url] || DANGER.test(t.url + ' ' + t.text)) continue;
      seen[t.url] = 1;
      fetched++;
      say('메뉴 화면 읽는 중 ' + fetched + '/' + MAX_FETCH + ' · ' + clean(t.text || t.url, 60));
      try {
        var res = await fetch(t.url, { credentials: 'same-origin' });
        if (!res.ok) continue;
        var type = res.headers.get('content-type') || '';
        /* 확장자 없이 파일을 주는 메뉴(download.do?id=…) */
        if (FILE_TYPE.test(type)) { await addFile(files, res, t, budget); continue; }
        if (!/html/.test(type)) continue;
        var doc = new DOMParser().parseFromString(await res.text(), 'text/html');
        pages.push(readDoc(doc, same(res.url) || t.url, null));
      } catch (e) { /* 열리지 않는 화면은 건너뛴다 */ }
    }
    /* 읽은 화면에 걸린 파일 링크 — 깊이와 상관없이 모은다(서버 스캔과 같다). */
    pages.forEach(function (p) {
      p.links.forEach(function (l) {
        if (FILE_EXT.test(new URL(l.url).pathname)) links.push({ url: l.url, text: l.text, page: p.url.split('#')[0] });
      });
    });
    for (var j = 0; j < links.length && files.length < MAX_FILES && !ctl.stop; j++) {
      var f = links[j];
      if (seen[f.url] || DANGER.test(f.url + ' ' + f.text)) continue;
      seen[f.url] = 1;
      say('파일 받는 중 ' + (files.length + 1) + ' · ' + clean(f.text || f.url, 60));
      try {
        var got = await fetch(f.url, { credentials: 'same-origin' });
        if (got.ok) await addFile(files, got, f, budget);
      } catch (e) { /* 받지 못한 파일은 건너뛴다 */ }
    }
    if (ctl.explore && !ctl.stop) {
      var explored = await explore(pages, seen);
      log('메뉴 탐색 — 조회 화면 ' + explored + '개를 찾았습니다');
    }
    var sets = [], collected = await collect(files, budget, sets);
    var out = { agent: AGENT, source: { id: SOURCE.id, name: SOURCE.name }, origin: origin,
                url: location.href.split('#')[0], at: new Date().toISOString(), menu: menu, pages: pages,
                files: files, collected: collected, datasets: sets, queries_version: SOURCE.version || '' };
    var sent = deliver('scan', out, 'gpax-scan');
    running = false;
    say('끝 — 메뉴 ' + menuN + '개 · 화면 ' + pages.length + '장 · 파일 ' + files.length + '개. ' +
        (sent ? '콘솔로 보냈습니다 — 콘솔이 결과를 올립니다.'
          : '내려받은 파일을 GPAX 콘솔 데이터 수집 → ' + SOURCE.name + ' → ① 북마크 추가 → 「콘솔 없이 쓸 때」에 올리세요.'));
    finish();
  }
  /* 끝나면 기록은 남겨 둔다(읽을 수 있게) — 닫기로 치운다. */
  function finish() {
    bar.textContent = '';
    bar.appendChild(button('닫기', function () { box.remove(); }));
  }

  /* 받는 창 — 콘솔이 연 탭이 아니면 이 코드가 콘솔의 받는 창을 팝업으로 연다(자기가 연 팝업과는 연결이 남는다).
   * 받는 창이 「준비됨」을 알리면 인사한다. */
  function hello(win) {
    peer = win;
    try { win.postMessage({ gpax: 'hello', origin: origin, title: clean(document.title, 120) }, SOURCE.console); }
    catch (e) { /* 끊긴 창 */ }
  }
  /* 사이트가 window.open을 바꿔 둔 경우가 있다 — GPS4(MSUIT4)는 빈 창을 열고 주소의 ? 뒤를 떼어 POST 폼으로
   * 보낸다. 그러면 받는 창이 해시(#/agent) 없이 POST로 열려 콘솔이 404였다(실측). 브라우저 원래의 open을 쓴다 —
   * 창의 메서드는 창 객체 자신에 있어(Window.prototype이 아니다) 사이트가 덮으면 남지 않으므로, 새 틀(iframe)의
   * 깨끗한 open을 이 창에 대고 부른다(열린 창의 opener는 이 창이다). */
  function nativeOpen(url, name, features) {
    var frame = document.createElement('iframe');
    frame.style.display = 'none';
    document.documentElement.appendChild(frame);
    try { return frame.contentWindow.open.call(window, url, name, features); }
    catch (e) { return window.open(url, name, features); }
    finally { frame.remove(); }
  }

  function openReceiver() {
    if (link) return;
    /* 열려 있던 받는 창도 새로 읽게 — 같은 주소면 브라우저가 다시 읽지 않아 「준비됨」이 오지 않는다. */
    var w = nativeOpen(SOURCE.receive.replace('#', '?r=' + Date.now() + '#'), 'gpax-agent', 'width=560,height=680');
    if (!w) {
      say('콘솔 받는 창이 막혔습니다 — 이 사이트의 팝업을 허용하고 북마크를 다시 누르세요.');
      finish();
      return;
    }
    say('콘솔 받는 창을 여는 중… (이 브라우저의 다른 탭에 GPAX 콘솔이 로그인돼 있어야 합니다)');
    window.addEventListener('message', function ready(e) {
      if (e.source !== w || e.origin !== SOURCE.console || !e.data || e.data.gpax !== 'ready') return;
      window.removeEventListener('message', ready);
      hello(w);
    });
  }
  listen();
  /* 콘솔이 연 탭이면 콘솔이 곧 답한다 — 답이 없으면(다른 페이지가 연 탭) 받는 창으로. */
  if (window.opener && SOURCE.console) {
    hello(window.opener);
    say('콘솔과 잇는 중…');
    setTimeout(function () { if (!link) openReceiver(); }, 1500);
  } else {
    openReceiver();
  }
})
