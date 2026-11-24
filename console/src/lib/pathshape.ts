/** 경로 틀의 모양(정규식)을 읽을 글로 — KR\-CT\d{9} → KR-CT{9자리}, doc\-\d{8}\-[a-z0-9]{4} → doc-{8자리}-{영숫자 4}. */
export const readablePattern = (pattern = '') => pattern
  .replace(/\\d\{(\d+)\}/g, '{$1자리}').replace(/\[a-z0-9\]\{(\d+)\}/g, '{영숫자 $1}').replace(/\\/g, '');
