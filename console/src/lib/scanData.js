/* GPAX 조회 응답 읽기 — 화면이 받은 응답(XHR·fetch의 JSON)에서 결과 표와 같은 목록을 찾고, 그 요청의 페이지 인자를
 * 바꿔 목록 **전체**를 받는 데 쓰는 순수 함수들이다. scanAgent.js가 쓴다(북마크에 함께 들어간다 — lib/agentCode.ts).
 * DOM·네트워크를 만지지 않는다 — 시험은 test/scanData.test.ts.
 *
 * 생각은 시연을 데이터로 읽는 쪽이다: 사람이 보여 준 조회 한 번은 "이 조건으로 이 목록"이라는 뜻이고, 화면의 표는
 * 그 목록의 첫 쪽일 뿐이다. 화면이 실제로 받은 응답에는 목록과 전체 건수, 페이지 인자가 있다 — 그 요청을 페이지만
 * 바꿔 다시 보내면 같은 조건의 목록을 빠짐없이 받고, 전체 건수로 다 받았는지 잰다(Integuru·mitmproxy2swagger의
 * 요청 읽기, Beyond Browsing의 "화면보다 API"). 어느 배열이 그 목록인지는 화면의 칸 글자와 값이 겹치는 정도로 고른다
 * — 모델에 값을 보내지 않고 정한다.
 *
 * 요청(call)의 모양: {method, url, headers: {이름: 값}, body: 문자열|null, ctype: 요청 본문 형식, status,
 *                    rtype: 응답 형식, text: 응답 본문}
 */
(function () {
  var PAGE = /^(page|pageno|pagenum|pagenumber|pageindex|pageidx|currentpage|curpage|cpage|pg|nowpage|pagecurrent|currpage)$/;
  var OFFSET = /^(start|offset|startrow|startindex|firstindex|firstrecordindex|first|skip|rowstart|startnum)$/;
  var SIZE = /^(rows|size|pagesize|limit|perpage|pageunit|recordcountperpage|rowcount|rowsperpage|length|listsize|pagerow|pagerows|numofrows|pagecnt|pagescale|max|maxresults|displaycount|viewcount|listcount)$/;
  var TOTAL = /(total|totcnt|totalcnt|totalcount|totalrecords|records|recordstotal|recordsfiltered|totalelements|totalsize|totalrow|totrow|totalitems|listcnt|allcnt)$/;
  var KEY = /(id|no|seq|key|num|code|cd|sn|idx)$/;
  var MAX_DEPTH = 6, SAMPLE = 50, CAP = 2000;

  function bare(name) { return String(name || '').replace(/[^0-9a-zA-Z]/g, '').toLowerCase(); }
  function lastName(path) { return bare(path[path.length - 1]); }
  function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
  function intOf(v) {
    if (typeof v === 'number' && isFinite(v) && Math.floor(v) === v) return v;
    if (typeof v === 'string' && /^\d{1,9}$/.test(v.trim())) return parseInt(v, 10);
    return null;
  }
  /* 화면 글자와 값을 견줄 모양 — 빈칸·쉼표를 빼고(1,234 = 1234), 날짜는 숫자만(2024.01.02 = 2024-01-02). */
  function norms(v) {
    var s = String(v == null ? '' : v).replace(/[\s,]+/g, '');
    if (!s || s.length > 100) return [];
    var out = [s];
    if (/^\d{4}\D\d{1,2}\D\d{1,2}/.test(s)) out.push(s.replace(/\D/g, '').slice(0, 8));
    return out;
  }

  function json(text) {
    var t = String(text || '').trim();
    if (!/^[\[{]/.test(t)) return undefined;
    try { return JSON.parse(t); } catch (e) { return undefined; }
  }
  function at(value, path) {
    for (var i = 0; i < path.length; i++) {
      if (value == null || typeof value !== 'object') return undefined;
      value = value[path[i]];
    }
    return value;
  }

  /* 응답 안의 목록 후보 — 객체가 든 배열마다 그 자리(path). */
  function arrays(value) {
    var out = [];
    (function walk(v, path, depth) {
      if (depth > MAX_DEPTH || v == null || typeof v !== 'object') return;
      if (Array.isArray(v)) {
        var objs = v.filter(isObj).length;
        if (v.length && objs * 2 >= v.length) out.push({ path: path, list: v });
        return;
      }
      Object.keys(v).forEach(function (k) { walk(v[k], path.concat([k]), depth + 1); });
    })(value, [], 0);
    return out;
  }

  /* 레코드 하나를 값만 남긴 평평한 모양으로 — 한 겹 안의 객체는 "바깥.안" 이름으로, 배열은 버린다. */
  function flat(rec) {
    var out = {};
    (function put(v, prefix, depth) {
      Object.keys(v).forEach(function (k) {
        var x = v[k], name = prefix ? prefix + '.' + k : k;
        if (x == null || typeof x === 'number' || typeof x === 'boolean') out[name] = x;
        else if (typeof x === 'string') out[name] = x.slice(0, CAP);
        else if (isObj(x) && depth < 1) put(x, name, depth + 1);
      });
    })(isObj(rec) ? rec : {}, '', 0);
    return out;
  }

  /* 목록이 화면 표와 얼마나 겹치나 — 앞 레코드들 중 값 둘 이상이 화면 칸 글자와 같은 레코드의 수. */
  function overlap(list, cells) {
    var n = 0, m = Math.min(list.length, SAMPLE);
    for (var i = 0; i < m; i++) {
      var r = flat(list[i]), hits = 0;
      Object.keys(r).forEach(function (k) {
        if (norms(r[k]).some(function (s) { return cells[s]; })) hits++;
      });
      if (hits >= 2) n++;
    }
    return n;
  }
  function cellSet(texts) {
    var set = {};
    (texts || []).forEach(function (t) { norms(t).forEach(function (s) { set[s] = 1; }); });
    return set;
  }

  /* 전체 건수 — 목록 바깥의 숫자 칸 중 이름이 건수이고 목록 길이 이상인 것(목록에 가까운 것부터). 같은 자리에
   * 둘이면 큰 것 — 쪽 수(jqGrid의 total)와 건수(records)가 나란히 오면 건수가 크다. */
  function totalOf(value, listPath, n) {
    var best = null, bestDepth = 99;
    (function walk(v, path, depth) {
      if (depth > MAX_DEPTH || v == null || typeof v !== 'object' || Array.isArray(v)) return;
      Object.keys(v).forEach(function (k) {
        var x = v[k], p = path.concat([k]);
        if (x && typeof x === 'object') { walk(x, p, depth + 1); return; }
        var num = intOf(x);
        if (num == null || num < n || !TOTAL.test(bare(k))) return;
        var shared = 0;
        while (shared < p.length && shared < listPath.length && p[shared] === listPath[shared]) shared++;
        var d = p.length - shared;
        if (d < bestDepth || (d === bestDepth && num > best)) { best = num; bestDepth = d; }
      });
    })(value, [], 0);
    return best;
  }

  /* 요청 인자 — 주소의 쿼리, 폼 본문, JSON 본문(안쪽까지). where·path로 다시 바꿀 수 있다. */
  function form(text) {
    var out = [];
    String(text || '').split('&').forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf('='), k = i < 0 ? kv : kv.slice(0, i), v = i < 0 ? '' : kv.slice(i + 1);
      try { out.push([decodeURIComponent(k.replace(/\+/g, ' ')), decodeURIComponent(v.replace(/\+/g, ' '))]); }
      catch (e) { out.push([k, v]); }
    });
    return out;
  }
  function bodyKind(call) {
    if (call.body == null || call.body === '') return 'none';
    if (/json/i.test(call.ctype || '') || json(call.body) !== undefined) return 'json';
    if (/^[^=&\s]+=[^&]*(&[^=&\s]+=[^&]*)*$/.test(String(call.body).trim())) return 'form';
    return 'raw';
  }
  function params(call) {
    var out = [];
    try {
      new URL(call.url).searchParams.forEach(function (v, k) { out.push({ where: 'query', path: [k], value: v }); });
    } catch (e) { /* 주소가 아니다 */ }
    var kind = bodyKind(call);
    if (kind === 'form') form(call.body).forEach(function (kv) { out.push({ where: 'form', path: [kv[0]], value: kv[1] }); });
    if (kind === 'json') {
      (function walk(v, path, depth) {
        if (depth > MAX_DEPTH || v == null) return;
        if (isObj(v)) Object.keys(v).forEach(function (k) { walk(v[k], path.concat([k]), depth + 1); });
        else if (!Array.isArray(v)) out.push({ where: 'json', path: path, value: v });
      })(json(call.body), [], 0);
    }
    return out;
  }
  /* 인자 이름 — 마지막 이름과, 바깥 이름을 붙인 것(JSON {page: {no, size}} → pageno·pagesize). 폼 이름의
   * 점·괄호(searchVO.pageIndex, page[size])도 같은 겹으로 본다. */
  function paramNames(p) {
    var parts = [];
    p.path.forEach(function (x) { parts = parts.concat(String(x).split(/[.\[\]]/).filter(Boolean)); });
    var last = bare(parts[parts.length - 1]);
    return parts.length > 1 ? [last, bare(parts[parts.length - 2]) + last] : [last];
  }

  /* 페이지 인자 — 쪽 번호(page)나 시작 위치(offset), 그리고 쪽 크기(size). 없으면 null. */
  function paging(call) {
    var ps = params(call), page = null, size = null;
    ps.forEach(function (p) {
      var names = paramNames(p), v = intOf(p.value);
      var is = function (re) { return names.some(function (n) { return re.test(n); }); };
      if (v == null) return;
      if (!page && is(PAGE)) page = { where: p.where, path: p.path, value: v, kind: 'page', raw: typeof p.value };
      else if (!page && is(OFFSET)) page = { where: p.where, path: p.path, value: v, kind: 'offset', raw: typeof p.value };
      else if (!size && is(SIZE) && v > 0) size = { where: p.where, path: p.path, value: v, raw: typeof p.value };
    });
    return page ? { page: page, size: size } : null;
  }

  /* 인자 몇 개만 바꾼 같은 요청 — 바꿀 것: [{where, path, value}] */
  function withParams(call, changes) {
    var url = call.url, body = call.body;
    var query = changes.filter(function (c) { return c.where === 'query'; });
    if (query.length) {
      var u = new URL(url);
      query.forEach(function (c) { u.searchParams.set(c.path[0], String(c.value)); });
      url = u.href;
    }
    var forms = changes.filter(function (c) { return c.where === 'form'; });
    if (forms.length) {
      body = form(body).map(function (kv) {
        var hit = forms.filter(function (c) { return c.path[0] === kv[0]; })[0];
        return encodeURIComponent(kv[0]) + '=' + encodeURIComponent(hit ? String(hit.value) : kv[1]);
      }).join('&');
    }
    var jsons = changes.filter(function (c) { return c.where === 'json'; });
    if (jsons.length) {
      var obj = json(body);
      jsons.forEach(function (c) {
        var cur = obj;
        for (var i = 0; i < c.path.length - 1; i++) cur = cur[c.path[i]];
        cur[c.path[c.path.length - 1]] = c.value;
      });
      body = JSON.stringify(obj);
    }
    return { method: call.method, url: url, headers: call.headers, body: body, ctype: call.ctype };
  }
  function valueAs(p, n) { return p.raw === 'string' ? String(n) : n; }

  /* n번째 쪽(0부터)의 요청. 잡힌 요청은 조회를 막 누른 첫 쪽이라 그 값에서 센다. 쪽 크기를 바꾸면(size)
   * 그 크기로 처음부터 다시 센다. */
  function pageCall(call, pg, n, size) {
    var per = size || (pg.size && pg.size.value) || pg.len || 0;
    var v = pg.page.kind === 'page' ? pg.page.value + n : pg.page.value + n * per;
    var changes = [{ where: pg.page.where, path: pg.page.path, value: valueAs(pg.page, v) }];
    if (size && pg.size) changes.push({ where: pg.size.where, path: pg.size.path, value: valueAs(pg.size, size) });
    return withParams(call, changes);
  }

  /* 조회 뒤에 잡힌 요청들 중 화면 표의 목록 — {call, path, records, total, paging, score}. 없으면 null.
   * 화면 칸 글자가 없으면(캔버스로 그리는 그리드) 페이지·건수가 있는 가장 긴 목록. */
  function findList(calls, cellTexts) {
    var cells = cellSet(cellTexts), hasCells = Object.keys(cells).length > 0, best = null;
    (calls || []).forEach(function (call) {
      if (!call || call.status >= 400) return;
      var data = json(call.text);
      if (data === undefined) return;
      arrays(data).forEach(function (a) {
        /* 화면에 보이는 행이 레코드와 겹쳐야 한다 — 둘 이상, 한 건뿐인 목록이면 그 한 건. */
        var score = hasCells ? overlap(a.list, cells) : 0;
        if (hasCells && score < Math.min(2, a.list.length)) return;
        var pg = paging(call), total = totalOf(data, a.path, a.list.length);
        if (!hasCells && !pg && total == null) return;
        var rank = score * 10 + (pg ? 2 : 0) + (total != null ? 1 : 0) + Math.min(a.list.length, 1000) / 1000;
        if (!best || rank > best.rank) {
          best = { call: call, path: a.path, records: a.list, total: total, score: score, rank: rank,
                   paging: pg ? { page: pg.page, size: pg.size, len: a.list.length } : null };
        }
      });
    });
    if (best) delete best.rank;
    return best;
  }

  /* 레코드를 가르는 열 — 값이 다 있고 겹치지 않는 열(바라는 이름 → 이름이 번호·코드 같은 것 → 첫 열). 없으면 ''. */
  function keyOf(records, prefer) {
    var rows = records.map(flat), keys = rows.length ? Object.keys(rows[0]) : [];
    var unique = keys.filter(function (k) {
      var seen = {};
      return rows.every(function (r) {
        var v = r[k];
        if (v == null || v === '' || typeof v === 'boolean' || seen['_' + v]) return false;
        seen['_' + v] = 1;
        return true;
      });
    });
    if (prefer && unique.indexOf(prefer) >= 0) return prefer;
    return unique.filter(function (k) { return KEY.test(bare(k.split('.').pop())); })[0] || unique[0] || '';
  }

  /* 열 이름 — 화면 표 머리글의 j번째 칸 글자들과 값이 가장 많이 같은 키에 그 머리글을 붙인다. */
  function columns(records, head, grid) {
    var rows = records.slice(0, SAMPLE).map(flat), keys = {}, label = {};
    rows.forEach(function (r) { Object.keys(r).forEach(function (k) { keys[k] = 1; }); });
    (head || []).forEach(function (h, j) {
      if (!h) return;
      var col = cellSet((grid || []).map(function (cells) { return cells[j]; })), top = 0, pick = '';
      Object.keys(keys).forEach(function (k) {
        if (label[k]) return;
        var n = rows.filter(function (r) { return norms(r[k]).some(function (s) { return col[s]; }); }).length;
        if (n > top) { top = n; pick = k; }
      });
      if (pick) label[pick] = h;
    });
    return Object.keys(keys).map(function (k) { return { key: k, label: label[k] || '' }; });
  }

  /* 같은 레코드인가(쪽 인자를 무시하는 서버는 같은 쪽을 또 준다) — 앞 레코드의 모양으로. */
  function sameHead(a, b) {
    return !!(a && a.length && b && b.length) && JSON.stringify(flat(a[0])) === JSON.stringify(flat(b[0]));
  }

  return { json: json, at: at, arrays: arrays, flat: flat, params: params, paging: paging, withParams: withParams,
           pageCall: pageCall, findList: findList, totalOf: totalOf, keyOf: keyOf, columns: columns,
           sameHead: sameHead };
})()
