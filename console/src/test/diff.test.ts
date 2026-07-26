import { describe, expect, it } from 'vitest';
import { extractDiffFromReply, parseUnifiedDiff } from '../lib/diff';

const SAMPLE = `diff --git a/hello.py b/hello.py
index 1234567..89abcde 100644
--- a/hello.py
+++ b/hello.py
@@ -1,2 +1,2 @@
 import os
-print("hello")
+print("hello, paas")
\\ No newline at end of file
`;

describe('parseUnifiedDiff', () => {
  it('classifies every line kind', () => {
    const lines = parseUnifiedDiff(SAMPLE);
    expect(lines.map((l) => l.kind)).toEqual([
      'meta', 'meta', 'meta', 'meta', 'hunk', 'ctx', 'del', 'add', 'meta',
    ]);
  });

  it('handles empty input without crashing', () => {
    expect(parseUnifiedDiff('')).toEqual([{ kind: 'ctx', text: '' }]);
  });

  it('treats +++/--- headers as meta, not add/del', () => {
    const lines = parseUnifiedDiff('--- a/x\n+++ b/x\n+real add');
    expect(lines[0].kind).toBe('meta');
    expect(lines[1].kind).toBe('meta');
    expect(lines[2].kind).toBe('add');
  });
});

describe('extractDiffFromReply', () => {
  it('extracts fenced diff block', () => {
    const reply = '설명입니다.\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n끝';
    const diff = extractDiffFromReply(reply);
    expect(diff).not.toBeNull();
    expect(diff!.startsWith('--- a/x')).toBe(true);
    expect(diff).toContain('+b');
  });

  it('returns null when no diff exists', () => {
    expect(extractDiffFromReply('그냥 답변입니다.')).toBeNull();
    expect(extractDiffFromReply('```diff\n\n```')).toBeNull();
  });

  it('falls back to unfenced diff headers', () => {
    const reply = '변경:\ndiff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-1\n+2';
    expect(extractDiffFromReply(reply)!.startsWith('diff --git')).toBe(true);
  });
});
