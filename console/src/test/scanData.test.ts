import { describe, expect, it } from 'vitest';
import code from '../lib/scanData.js?raw';

// 북마크에 들어가는 식 그대로 연다 — 모듈이 아니라 식 하나다(lib/agentCode.ts).
const D = new Function(`return (${code.trim()});`)();

const call = (over: Record<string, unknown>) => ({
  method: 'POST', url: 'https://erp.example.com/pr/list.do', headers: {}, body: null, ctype: '',
  status: 200, rtype: 'application/json', text: '', ...over,
});

const rows = (from: number, n: number) => Array.from({ length: n }, (_, i) => ({
  reqNo: `PR-${String(from + i).padStart(5, '0')}`, title: `요청 ${from + i}`, amount: 1000 * (from + i),
  regDate: '2026-10-01', dept: { name: '구매3팀' },
}));

describe('조회 응답에서 화면 표의 목록 찾기', () => {
  it('화면 칸 글자와 값이 겹치는 배열을 고르고 건수·페이지 인자를 읽는다', () => {
    const list = call({
      body: 'searchVO.pageIndex=1&searchVO.pageUnit=20&fromDt=20260901',
      ctype: 'application/x-www-form-urlencoded',
      text: JSON.stringify({ menu: [{ id: 1, label: '홈' }, { id: 2, label: '공지' }],
                             result: { totalCnt: 57, list: rows(1, 20) } }),
    });
    const noise = call({ url: 'https://erp.example.com/common/codes.do', text: JSON.stringify({ codes: [{ cd: 'A', nm: '승인' }] }) });
    // 화면 글자는 쉼표·점 모양 — 1,000 / 2026.10.01
    const cells = ['PR-00001', '요청 1', '1,000', '2026.10.01', 'PR-00002', '요청 2', '2,000'];
    const found = D.findList([noise, list], cells);
    expect(found.path).toEqual(['result', 'list']);
    expect(found.total).toBe(57);
    expect(found.paging.page).toMatchObject({ where: 'form', path: ['searchVO.pageIndex'], value: 1, kind: 'page' });
    expect(found.paging.size).toMatchObject({ path: ['searchVO.pageUnit'], value: 20 });
  });

  it('jqGrid처럼 쪽 수(total)와 건수(records)가 나란하면 건수를 고른다', () => {
    const text = JSON.stringify({ page: 1, total: 30, records: 590, rows: rows(1, 20) });
    const found = D.findList([call({ url: 'https://erp.example.com/list?page=1&rows=20', method: 'GET', text })],
      ['PR-00001', '요청 1', 'PR-00002', '요청 2']);
    expect(found.total).toBe(590);
    expect(found.paging.page.where).toBe('query');
  });

  it('화면 칸 글자가 없으면(캔버스 그리드) 페이지·건수가 있는 목록만', () => {
    const plain = call({ text: JSON.stringify({ items: rows(1, 3) }) });
    expect(D.findList([plain], [])).toBeNull();
    const paged = call({ body: JSON.stringify({ page: { no: 1, size: 3 } }), ctype: 'application/json',
                         text: JSON.stringify({ data: rows(1, 3), totalCount: 9 }) });
    const found = D.findList([plain, paged], []);
    expect(found.path).toEqual(['data']);
    expect(found.paging.page).toMatchObject({ where: 'json', path: ['page', 'no'] });
  });

  it('화면과 겹치지 않는 배열은 고르지 않는다', () => {
    const other = call({ text: JSON.stringify({ list: rows(100, 5) }) });
    expect(D.findList([other], ['PR-00001', '요청 1'])).toBeNull();
  });
});

describe('다음 쪽 요청 만들기', () => {
  it('폼 본문의 쪽 번호만 바꾼다 — 다른 조건은 그대로', () => {
    const c = call({ body: 'searchVO.pageIndex=1&searchVO.pageUnit=20&fromDt=20260901' });
    const pg = { ...D.paging(c), len: 20 };
    const next = D.pageCall(c, pg, 2);
    expect(next.body).toBe('searchVO.pageIndex=3&searchVO.pageUnit=20&fromDt=20260901');
    // 쪽 크기를 키우면 그 크기로 처음부터 센다
    expect(D.pageCall(c, pg, 0, 500).body).toBe('searchVO.pageIndex=1&searchVO.pageUnit=500&fromDt=20260901');
  });

  it('시작 위치(offset)는 쪽 크기만큼 민다 — 문자열 값은 문자열로', () => {
    const c = call({ method: 'GET', url: 'https://erp.example.com/list?start=0&length=25&q=a' });
    const pg = { ...D.paging(c), len: 25 };
    expect(pg.page.kind).toBe('offset');
    expect(new URL(D.pageCall(c, pg, 3).url).searchParams.get('start')).toBe('75');
    const j = call({ body: JSON.stringify({ cond: { dept: 'A' }, offset: '0', limit: '50' }) });
    const jp = { ...D.paging(j), len: 50 };
    expect(JSON.parse(D.pageCall(j, jp, 1).body)).toEqual({ cond: { dept: 'A' }, offset: '50', limit: '50' });
  });

  it('페이지 인자가 없으면 null', () => {
    expect(D.paging(call({ body: 'fromDt=20260901&dept=A' }))).toBeNull();
  });
});

describe('레코드 정리', () => {
  it('값만 남긴 평평한 모양 — 한 겹 안은 바깥.안, 배열은 버린다', () => {
    expect(D.flat({ a: 1, b: { c: 'x', d: { e: 1 } }, f: [1, 2], g: null })).toEqual({ a: 1, 'b.c': 'x', g: null });
  });

  it('키는 겹치지 않는 번호 같은 열 — 바라는 열이 있으면 그것', () => {
    const recs = rows(1, 5);
    expect(D.keyOf(recs)).toBe('reqNo');
    expect(D.keyOf(recs, 'title')).toBe('title');
    expect(D.keyOf([{ a: 'x' }, { a: 'x' }])).toBe('');
  });

  it('열 이름은 화면 머리글에서 — 값이 같은 칸의 머리글을 붙인다', () => {
    const recs = rows(1, 2);
    const cols = D.columns(recs, ['요청번호', '제목', '금액'],
      [['PR-00001', '요청 1', '1,000'], ['PR-00002', '요청 2', '2,000']]);
    const label = Object.fromEntries(cols.map((c: { key: string; label: string }) => [c.key, c.label]));
    expect(label).toMatchObject({ reqNo: '요청번호', title: '제목', amount: '금액', regDate: '' });
  });

  it('쪽 인자를 무시하는 서버 — 같은 쪽이 또 오면 알아본다', () => {
    expect(D.sameHead(rows(1, 3), rows(1, 3))).toBe(true);
    expect(D.sameHead(rows(1, 3), rows(4, 3))).toBe(false);
  });
});
