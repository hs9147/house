import { describe, expect, it } from 'vitest';
import { canOpenInVscode, vscodeCloneUri } from '../lib/vscode';

describe('vscodeCloneUri', () => {
  it('VS Code git 확장이 처리하는 clone URI를 만든다', () => {
    expect(vscodeCloneUri('http://git.example.com/org/repo.git'))
      .toBe('vscode://vscode.git/clone?url=http%3A%2F%2Fgit.example.com%2Forg%2Frepo.git');
  });

  it('주소를 인코딩한다 — 인코딩을 빠뜨리면 쿼리스트링이 잘려 엉뚱한 곳을 clone한다', () => {
    const uri = vscodeCloneUri('http://git.example.com/o/r.git?x=1&y=2');
    expect(uri).not.toContain('&y=2');       // 원본의 &가 URI의 파라미터 구분자로 새지 않는다
    expect(uri).toContain('%3Fx%3D1%26y%3D2');
  });
});

describe('canOpenInVscode', () => {
  it('실제 http(s) 주소면 열 수 있다', () => {
    expect(canOpenInVscode('http://git.example.com/o/r.git')).toBe(true);
    expect(canOpenInVscode('https://git.example.com/o/r.git')).toBe(true);
  });

  // 비관리자·비소속 사용자에게 git_url은 안내 문구로 마스킹돼 내려온다
  // (api/projects.py의 GIT_URL_MASK). 그걸 clone에 넘기면 VS Code만 열리고 실패한다.
  it('마스킹된 문구는 열 수 없다', () => {
    expect(canOpenInVscode('(내부 관리 — 관리자만 조회 가능)')).toBe(false);
  });

  it('값이 없으면 열 수 없다', () => {
    expect(canOpenInVscode(null)).toBe(false);
    expect(canOpenInVscode(undefined)).toBe(false);
    expect(canOpenInVscode('')).toBe(false);
    expect(canOpenInVscode('   ')).toBe(false);
  });
});
