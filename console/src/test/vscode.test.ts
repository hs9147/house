import { describe, expect, it } from 'vitest';
import { gitArchiveZipUrl, hasUsableGitUrl, vscodeCloneUri } from '../lib/vscode';

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

describe('hasUsableGitUrl', () => {
  it('실제 http(s) 주소면 열 수 있다', () => {
    expect(hasUsableGitUrl('http://git.example.com/o/r.git')).toBe(true);
    expect(hasUsableGitUrl('https://git.example.com/o/r.git')).toBe(true);
  });

  // 비관리자·비소속 사용자에게 git_url은 안내 문구로 마스킹돼 내려온다
  // (api/projects.py의 GIT_URL_MASK). 그걸 clone에 넘기면 VS Code만 열리고 실패한다.
  it('마스킹된 문구는 열 수 없다', () => {
    expect(hasUsableGitUrl('(내부 관리 — 관리자만 조회 가능)')).toBe(false);
  });

  it('값이 없으면 열 수 없다', () => {
    expect(hasUsableGitUrl(null)).toBe(false);
    expect(hasUsableGitUrl(undefined)).toBe(false);
    expect(hasUsableGitUrl('')).toBe(false);
    expect(hasUsableGitUrl('   ')).toBe(false);
  });
});

describe('gitArchiveZipUrl', () => {
  it('Gitea 아카이브 주소를 만든다', () => {
    expect(gitArchiveZipUrl('https://paas.example.com/git/org/repo.git', 'main'))
      .toBe('https://paas.example.com/git/org/repo/archive/main.zip');
  });

  // repo.git/archive/... 는 없는 리포라 404다 — clone 주소를 그대로 쓰면 안 된다.
  it('끝의 .git과 슬래시를 떼고 붙인다', () => {
    expect(gitArchiveZipUrl('https://h/o/r.git', 'main')).toBe('https://h/o/r/archive/main.zip');
    expect(gitArchiveZipUrl('https://h/o/r/', 'main')).toBe('https://h/o/r/archive/main.zip');
    expect(gitArchiveZipUrl('https://h/o/r', 'main')).toBe('https://h/o/r/archive/main.zip');
  });

  // %2F로 인코딩하면 Gitea가 ref를 못 찾는다 — 브랜치의 /는 경로 구분자로 살려 둔다.
  it('브랜치의 슬래시를 인코딩하지 않는다', () => {
    expect(gitArchiveZipUrl('https://h/o/r.git', 'feature/x'))
      .toBe('https://h/o/r/archive/feature/x.zip');
  });

  it('브랜치가 비면 main으로 받는다', () => {
    expect(gitArchiveZipUrl('https://h/o/r.git', '')).toBe('https://h/o/r/archive/main.zip');
  });
});
