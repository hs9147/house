/* GPAX 브라우저 스캔 — 사용자가 로그인한 탭에서 북마크릿으로 돈다(pages/SourceDetail.tsx).
 *
 * 하는 일: 화면에 그려진 메뉴 트리, 열려 있는 화면(같은 출처의 iframe 포함)의 제목·표 머리글·
 * 조회 조건, 메뉴의 GET 링크 몇 장을 읽어 JSON 파일로 내려받는다.
 * 하지 않는 일: gpax로 보내기(운영 콘솔이 http라 https 페이지가 부를 수 없다), 쿠키 읽기,
 * 입력값 읽기(이름·라벨만), POST, 로그아웃·삭제 링크 열기.
 * 바깥 파일을 불러오지 않는다 — 북마크에 이 코드가 통째로 들어간다(외부 의존 금지).
 */
(function () {
  var AGENT = 'gpax-scan/1';
  var MAX_FETCH = 25;
  var DANGER = /log-?out|sign-?out|logoff|로그아웃|delete|remove|삭제/i;
  var NAV = /(^|[-_ ])(nav|navbar|menu|gnb|lnb|snb|sidebar|sitemap|tree)([-_ ]|$)/i;
  var NAV_ROLES = { navigation: 1, menu: 1, menubar: 1, tree: 1, tablist: 1 };
  var origin = location.origin;

  var box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;background:#1f2937;' +
    'color:#fff;padding:10px 14px;border-radius:6px;font:13px sans-serif;box-shadow:0 2px 8px #0006';
  /* 열린 화면을 다 읽은 뒤에 붙인다 — 먼저 붙이면 이 안내문이 화면 글에 섞인다. */
  function say(t) { box.textContent = 'GPAX 스캔 — ' + t; if (!box.parentNode) document.body.appendChild(box); }

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

  function fieldLabel(el) {
    if (el.labels && el.labels[0]) return textOf(el.labels[0]);
    var named = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title');
    if (named) return clean(named, 80);
    var cell = el.closest && el.closest('td');
    var head = cell && cell.previousElementSibling;
    if (head && /^(TH|TD)$/.test(head.tagName)) return textOf(head);
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
    var fetched = 0;
    for (var i = 0; i < targets.length && fetched < MAX_FETCH; i++) {
      var t = targets[i];
      if (seen[t.url] || DANGER.test(t.url + ' ' + t.text)) continue;
      seen[t.url] = 1;
      fetched++;
      say('메뉴 화면 읽는 중 ' + fetched + '…');
      try {
        var res = await fetch(t.url, { credentials: 'same-origin' });
        if (!res.ok || !/html/.test(res.headers.get('content-type') || '')) continue;
        var doc = new DOMParser().parseFromString(await res.text(), 'text/html');
        pages.push(readDoc(doc, same(res.url) || t.url, null));
      } catch (e) { /* 열리지 않는 화면은 건너뛴다 */ }
    }
    var out = { agent: AGENT, origin: origin, url: location.href.split('#')[0],
                at: new Date().toISOString(), menu: menu, pages: pages };
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out)], { type: 'application/json' }));
    a.download = 'gpax-scan-' + location.hostname + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    say('끝 — 메뉴 ' + menuN + '개 · 화면 ' + pages.length + '장. 내려받은 파일을 GPAX 콘솔 ' +
        '정보 업데이트 → 이 출처 → "브라우저 스캔 결과 올리기"로 올리세요.');
    box.onclick = function () { box.remove(); };
    setTimeout(function () { box.remove(); }, 20000);
  }

  run().catch(function (e) { say('실패 — ' + (e && e.message)); });
})();
