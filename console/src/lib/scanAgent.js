/* GPAX 브라우저 스캔 — 사용자가 로그인한 탭에서 북마크릿으로 돈다(pages/SourceDetail.tsx).
 *
 * 하는 일: 화면에 그려진 메뉴 트리, 열려 있는 화면(같은 출처의 iframe 포함)의 제목·표 머리글·
 * 조회 조건, 메뉴의 GET 링크 몇 장, 같은 출처의 문서·표·발표 파일(GET)을 읽고, 수집 조건이 있으면
 * 그 화면을 조회해 결과 파일과 행마다 상세의 첨부를 받아(아래 「수집」) JSON 파일로 내려받는다.
 * 하지 않는 일: gpax로 보내기(운영 콘솔이 http라 https 페이지가 부를 수 없다), 쿠키 읽기,
 * 입력값 읽기(이름·라벨만 — 수집 조건을 채울 뿐이다), 저장·삭제·승인 같은 버튼 누르기, 로그아웃·삭제 링크 열기.
 * 바깥 파일을 불러오지 않는다 — 북마크에 이 코드가 통째로 들어간다(외부 의존 금지).
 * 북마크는 출처마다 하나다 — 콘솔이 __GPAX_SOURCE__ 자리에 {id, name, origin}을 넣는다.
 */
(function (SOURCE) {
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

  /* 진행 창 — 지금 하는 일, 지나온 기록(실시간), 그리고 도는 동안 바꿀 수 있는 것: 상세를 열 행 수(조회
   * 한도)와 멈추기. 한도는 다음 행부터 바로 반영되고, 멈추면 지금 하던 일까지 하고 받은 것을 내려받는다. */
  var MAX_ROWS = 100;
  var ctl = { rows: 20, stop: false };
  var box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;background:#1f2937;width:380px;' +
    'color:#fff;padding:10px 14px;border-radius:6px;font:13px sans-serif;box-shadow:0 2px 8px #0006';
  var head = document.createElement('div'), status = document.createElement('div'), logBox = document.createElement('div');
  var bar = document.createElement('div'), rowsIn = document.createElement('input'), stopBtn = document.createElement('button');
  head.textContent = 'GPAX 스캔 · ' + SOURCE.name;
  head.style.cssText = 'font-weight:bold;margin-bottom:4px';
  status.style.cssText = 'margin-bottom:6px';
  logBox.style.cssText = 'max-height:180px;overflow:auto;font:11px/1.5 monospace;background:#111827;padding:4px 6px;border-radius:4px';
  bar.style.cssText = 'margin-top:6px;display:flex;gap:6px;align-items:center;font-size:12px';
  rowsIn.type = 'number';
  rowsIn.min = '1';
  rowsIn.max = String(MAX_ROWS);
  rowsIn.value = String(ctl.rows);
  rowsIn.setAttribute('aria-label', 'GPAX 상세 열 행 수');
  rowsIn.style.cssText = 'width:56px;color:#000';
  rowsIn.oninput = function () {
    var n = parseInt(rowsIn.value, 10);
    if (n >= 1 && n <= MAX_ROWS && n !== ctl.rows) { ctl.rows = n; log('조회 한도를 ' + n + '행으로 바꿨습니다'); }
  };
  stopBtn.textContent = '멈추기';
  stopBtn.onclick = function () {
    if (ctl.stop) return;
    ctl.stop = true;
    stopBtn.disabled = true;
    log('멈추는 중 — 지금 하던 일까지 하고 받은 것을 내려받습니다');
  };
  bar.appendChild(document.createTextNode('상세 열 행'));
  bar.appendChild(rowsIn);
  bar.appendChild(stopBtn);
  box.appendChild(head);
  box.appendChild(status);
  box.appendChild(logBox);
  box.appendChild(bar);
  /* 열린 화면을 다 읽은 뒤에 붙인다 — 먼저 붙이면 이 안내문이 화면 글에 섞인다. */
  function show() { if (!box.parentNode) document.body.appendChild(box); }
  function log(t) {
    var line = document.createElement('div');
    line.textContent = new Date().toTimeString().slice(0, 8) + ' ' + t;
    logBox.appendChild(line);
    while (logBox.childNodes.length > 300) logBox.removeChild(logBox.firstChild);
    logBox.scrollTop = logBox.scrollHeight;
    show();
  }
  function say(t) { status.textContent = t; log(t); }

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
   * 화면은 숨긴 틀(iframe)에 따로 연다 — 사이트가 화면을 옮겨도(상세로 이동) 이 북마크는 살아 있고,
   * 사람이 보던 화면은 그대로다. 누르는 것은 조회·상세·첨부·엑셀 버튼뿐이다:
   *  - 이름(짧은 버튼 글자)이나 스크립트(onclick·href)에 저장·삭제·승인 같은 말이 걸리면 누르지 않는다.
   *  - 사이트가 묻는 창(confirm)에는 늘 '아니오', 알림(alert)은 띄우지 않고 메모로 남긴다.
   *  - 첨부·엑셀을 누를 때는 이동·새 창·폼 전송을 막고 같은 요청으로 파일을 받는다(사람 PC에 내려받지 않는다). */
  var MAX_ATTACH = 10, SETTLE_MS = 700, WAIT_MS = 12000, CATCH_MS = 3000;
  var MUTATE_TEXT = /저장|삭제|등록|수정|승인|반려|결재|상신|전송|제출|발송|취소|확정|신청|반영|초기화|로그아웃|save|delete|remove|insert|update|modify|edit|approve|reject|submit|send|cancel|reset|logout|sign ?out/i;
  var MUTATE_CODE = /delete|remove|save|insert|update|modify|approve|reject|cancel|logout|signout|send|_del|del_|fn_?del|fn_?reg|fn_?upd|fn_?apr/i;
  var SEARCH = /^(조회|검색|찾기|조회하기|검색하기|search|find)$/i;
  var DETAIL = /상세|보기|detail|view|열기/i;
  var EXPORT = /엑셀|excel|xls|export|내보내기|다운로드|내려받기|download/i;
  var NOT_EXPORT = /양식|서식|템플릿|template|매뉴얼|manual|도움말|help|첨부/i;
  var ATTACH = /첨부|다운로드|내려받기|download|파일/i;
  var FILE_NAME = /\.(pdf|docx?|hwpx?|txt|xlsx?|csv|pptx?)(\s|\(|\[|$)/i;
  var CLOSE = /^(닫기|close|×|x|✕)$/i;
  var mode = 'pass', caught = [], notes = null;
  /* 수집 메모 — 결과 파일에 실리고(콘솔의 스캔 메모), 진행 창에도 바로 보인다. */
  function note(t) { if (notes) notes.push(t); log('! ' + t); }

  function sleep(ms) { return new Promise(function (ok) { setTimeout(ok, ms); }); }
  function docOf(frame) { try { return frame.contentDocument; } catch (e) { return null; } }
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
  function clickables(doc) {
    return Array.prototype.slice.call(doc.querySelectorAll(
      'a,button,input[type=button],input[type=submit],input[type=image],[role=button],[onclick]'));
  }
  function visible(el) { return el.offsetParent !== null || el.getClientRects().length > 0; }

  function makeFrame(name) {
    var f = document.createElement('iframe');
    f.name = name;
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

  /* 틀 안 화면의 손잡이 — 새 문서가 뜰 때마다 다시 건다(이동하면 전역이 새로 생긴다). */
  function arm(frame) {
    var w;
    try { w = frame.contentWindow; if (!w || !w.document || w.__gpaxArmed) return; } catch (e) { return; }
    w.__gpaxArmed = true;
    w.alert = function (m) { note('사이트 알림: ' + clean(m, 120)); };
    w.confirm = function () { return false; };
    w.prompt = function () { return null; };
    var popup = { closed: false, close: function () {}, focus: function () {}, blur: function () {},
                  document: { write: function () {}, writeln: function () {}, close: function () {} } };
    w.open = function (url) {
      if (url) caught.push({ kind: 'get', url: new URL(url, w.location.href).href, popup: true });
      return popup;
    };
    var submit = w.HTMLFormElement.prototype.submit;
    function take(form) {
      var target = (form.getAttribute('target') || '').toLowerCase();
      /* 상세를 열 때는 같은 틀로 가는 전송은 그대로 둔다(틀이 상세로 바뀐다) — 새 창으로 가는 것만 잡는다. */
      if (mode === 'detail' && (!target || target === '_self' || target === '_parent' || target === '_top')) return false;
      caught.push({ kind: 'form', form: form, submit: submit });
      return true;
    }
    w.HTMLFormElement.prototype.submit = function () {
      if (mode === 'pass' || !take(this)) return submit.call(this);
    };
    w.document.addEventListener('submit', function (e) {
      if (mode !== 'pass' && take(e.target)) e.preventDefault();
    }, true);
    /* 파일을 누를 때는 이 틀의 이동도 막는다 — location.href로 내려받는 사이트가 많다(크롬·엣지). */
    if (w.navigation) {
      w.navigation.addEventListener('navigate', function (e) {
        if (mode !== 'file' || !e.cancelable) return;
        e.preventDefault();
        var url = e.destination.url;
        caught.push(e.formData ? { kind: 'post', url: url, body: e.formData }
          : { kind: 'get', url: url, early: /^blob:/.test(url) ? fetch(url) : null });
      });
    }
    /* 화면에 붙이지 않은 링크로 내려받는 경우(스크립트가 만든 blob) — 곧 지워지므로 바로 받는다. */
    var aclick = w.HTMLAnchorElement.prototype.click;
    w.HTMLAnchorElement.prototype.click = function () {
      if (mode === 'file' && this.href && (/^blob:/.test(this.href) || this.hasAttribute('download'))) {
        caught.push({ kind: 'get', url: this.href, name: this.getAttribute('download') || '',
                      early: /^blob:/.test(this.href) ? fetch(this.href) : null });
        return;
      }
      return aclick.call(this);
    };
  }

  function press(el) {
    var w = el.ownerDocument.defaultView;
    if (el.tagName === 'TR' && el.getAttribute('ondblclick')) el.dispatchEvent(new w.MouseEvent('dblclick', { bubbles: true }));
    else el.click();
  }

  /* 상대 날짜(@today-1m)를 그 칸의 모양으로 — 지금 값·안내 글이 2024.01.01이면 점으로, 20240101이면 붙여서. */
  function dateValue(el, value) {
    var m = /^@today(?:([+-]\d+)([dmy]))?$/i.exec(value);
    if (!m) return value;
    var d = new Date(), n = m[1] ? parseInt(m[1], 10) : 0, unit = (m[2] || '').toLowerCase();
    if (unit === 'd') d.setDate(d.getDate() + n);
    if (unit === 'm') d.setMonth(d.getMonth() + n);
    if (unit === 'y') d.setFullYear(d.getFullYear() + n);
    var y = String(d.getFullYear()), mo = ('0' + (d.getMonth() + 1)).slice(-2), da = ('0' + d.getDate()).slice(-2);
    if (el.type === 'date') return y + '-' + mo + '-' + da;
    var cur = clean(el.value || el.getAttribute('placeholder') || '', 40);
    var sep = /\d{4}([.\-\/])\d{1,2}/.exec(cur) || /[yY]{4}([.\-\/])[mM]{2}/.exec(cur);
    if (sep) return y + sep[1] + mo + sep[1] + da;
    if (/^\d{8}$/.test(cur) || /^[yY]{4}[mM]{2}[dD]{2}$/.test(cur)) return y + mo + da;
    return y + '-' + mo + '-' + da;
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
  function findSearch(doc) {
    var hits = clickables(doc).filter(function (el) { return SEARCH.test(labelOf(el)) && !risky(el); });
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
  function detailOf(tr) {
    var els = clickables(tr).filter(function (el) { return !risky(el) && !isAttachment(el); });
    var named = els.filter(function (el) { return DETAIL.test(labelOf(el)); })[0];
    if (named) return named;
    var title = els.filter(function (el) { return el.tagName === 'A' && labelOf(el).length > 1; })[0];
    if (title) return title;
    if (els.length) return els[0];
    if (tr.getAttribute('onclick') || tr.getAttribute('ondblclick')) return risky(tr) ? null : tr;
    /* 행 클릭을 스크립트로 단 그리드 — 제목 칸을 눌러 본다(아무 일도 없으면 그뿐이다). */
    return tr.querySelectorAll('td')[1] || null;
  }
  function exportButton(doc, rows) {
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

  /* 조건을 채우고 조회한다 — 실패하면 그 까닭을 돌려준다. */
  async function search(q, url, work) {
    await load(work, url);
    var doc = docOf(work);
    if (!doc || !doc.body) return '화면을 틀 안에 열지 못했습니다(사이트가 막아 두었을 수 있음)';
    var map = fieldMap(doc), used = {}, missing = [];
    q.conditions.forEach(function (c) {
      var n = used[c.field] || 0, el = (map[c.field] || [])[n];
      used[c.field] = n + 1;
      if (!el || !c.value) { if (!el) missing.push(c.field); return; }
      if (!setValue(el, c.value)) missing.push(c.field + '=' + c.value);
    });
    if (missing.length && missing.length === q.conditions.length) return '조건 칸을 찾지 못했습니다: ' + missing.join(', ');
    if (missing.length) note('채우지 못한 조건: ' + missing.join(', '));
    var btn = findSearch(doc);
    if (!btn) return '조회 버튼을 찾지 못했습니다';
    arm(work);
    mode = 'pass';
    press(btn);
    await settle(work);
    return '';
  }

  /* 결과 파일 — 엑셀 버튼이 있으면 그것으로, 없거나 받지 못하면 결과 표를 CSV로. */
  async function exportResult(q, work, files, budget) {
    var doc = docOf(work), rows = resultRows(doc), btn = exportButton(doc, rows);
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
    var trigger = detailOf(tr);
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
      var c = popups[0];
      if (c.kind === 'form') {
        var wait = loaded(detail);
        c.form.setAttribute('target', detail.name);
        c.submit.call(c.form);
        await wait;
        await settle(detail);
      } else if (same(c.url)) await load(detail, c.url);
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

  /* 조건을 채울 화면 — 지금 열려 있는 틀 중 조건 칸이 다 있는 화면의 주소가 먼저(탭마다 iframe인 사이트는
   * 저장한 주소가 껍데기 화면이다), 없으면 저장한 주소. */
  function screenUrl(q) {
    var open = frames(window, 0, []).filter(function (f) { return f.doc.body && hasFields(f.doc, q); })[0];
    return open ? open.win.location.href : q.url.split('#')[0];
  }

  async function collectScreen(q, work, detail, files, budget) {
    var rep = { screen: q.screen, url: q.url, rows: 0, details: 0, files: 0, notes: [] };
    notes = rep.notes;
    var url = screenUrl(q), start = files.length;
    say(q.screen + ' — 조건을 채우고 조회하는 중');
    var why = await search(q, url, work);
    if (why) { note(why); notes = null; return rep; }
    var rows = resultRows(docOf(work));
    rep.rows = rows.length;
    log(q.screen + ' — 결과 ' + rows.length + '행');
    await exportResult(q, work, files, budget);
    /* 한도(ctl.rows)는 도는 동안 바뀔 수 있다 — 행마다 다시 본다. */
    var i = 0;
    for (; i < Math.min(rep.rows, ctl.rows) && !ctl.stop; i++) {
      if (files.length >= MAX_FILES || budget.left <= 0) { note('파일 상한에 닿아 멈췄습니다'); break; }
      rows = docOf(work) ? resultRows(docOf(work)) : [];
      if (rows.length <= i) {
        why = await search(q, url, work);
        rows = why ? [] : resultRows(docOf(work));
      }
      if (rows.length <= i) { note((i + 1) + '번째 행을 다시 찾지 못해 멈췄습니다'); break; }
      say(q.screen + ' — 상세 ' + (i + 1) + '/' + Math.min(rep.rows, ctl.rows) + ' · ' + rowLabel(rows[i]));
      var got = await openDetail(rows[i], q, work, detail, files, budget);
      if (got.opened) rep.details++;
      if (got.navigated) await search(q, url, work);
    }
    if (ctl.stop) note('사람이 멈췄습니다 — 결과 ' + rep.rows + '행 중 ' + i + '행까지 봤습니다');
    else if (rep.rows > i) note('결과 ' + rep.rows + '행 중 앞 ' + i + '행만 상세를 열었습니다(조회 한도)');
    rep.files = files.length - start;
    notes = null;
    return rep;
  }

  async function collect(files, budget) {
    var queries = SOURCE.queries || [], out = [];
    if (!queries.length) return out;
    var work = makeFrame('gpax-work'), detail = makeFrame('gpax-detail');
    try {
      for (var i = 0; i < queries.length && !ctl.stop; i++) out.push(await collectScreen(queries[i], work, detail, files, budget));
    } finally {
      work.remove();
      detail.remove();
    }
    return out;
  }

  async function run() {
    var pages = [], seen = {}, menu = [], menuN = 0;
    frames(window, 0, []).forEach(function (f) {
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
    var collected = await collect(files, budget);
    var out = { agent: AGENT, source: { id: SOURCE.id, name: SOURCE.name }, origin: origin,
                url: location.href.split('#')[0], at: new Date().toISOString(), menu: menu, pages: pages,
                files: files, collected: collected };
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out)], { type: 'application/json' }));
    a.download = 'gpax-scan-' + SOURCE.id + '-' + String(SOURCE.name).replace(/[\\/:*?"<>|\s]+/g, '_') + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    say('끝 — 메뉴 ' + menuN + '개 · 화면 ' + pages.length + '장 · 파일 ' + files.length + '개. 내려받은 파일을 GPAX 콘솔 ' +
        '정보 업데이트 → ' + SOURCE.name + ' → 브라우저에서 스캔에 올리세요.');
    finish();
  }
  /* 끝나면 기록은 남겨 둔다(읽을 수 있게) — 닫기로 치운다. */
  function finish() {
    bar.textContent = '';
    var close = document.createElement('button');
    close.textContent = '닫기';
    close.onclick = function () { box.remove(); };
    bar.appendChild(close);
  }
  function dismiss() {
    box.onclick = function () { box.remove(); };
    setTimeout(function () { box.remove(); }, 20000);
  }

  /* 다른 사이트에서 누른 북마크는 아무것도 읽지 않는다 — 북마크가 여러 개면 헷갈리기 쉽다. */
  if (origin !== SOURCE.origin) {
    say('이 북마크는 ' + SOURCE.origin + '용입니다. 지금 사이트(' + origin + ')에서는 읽지 않습니다.');
    dismiss();
    return;
  }
  run().catch(function (e) { say('실패 — ' + (e && e.message)); finish(); });
})(__GPAX_SOURCE__);
