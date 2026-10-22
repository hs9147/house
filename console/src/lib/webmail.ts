// 통합 북마크 「GPAX」(lib/agentCode.ts)가 연 받는 창(pages/AgentReceive)과 로그인한 콘솔 탭
// (components/AgentRelay)이 같은 브라우저 안에서 이 채널로 주고받는다. 같은 출처(콘솔)만 쓸 수 있다.
export const AGENT_CHANNEL = 'gpax-agent';

// 메일을 보내도 되는 곳 — 웹 Outlook 주소만(받는 창은 다른 사이트가 열어도 이 출처가 아니면 메일로 받지 않는다).
export const OUTLOOK_ORIGINS = [
  'https://outlook.cloud.microsoft',
  'https://outlook.office.com',
  'https://outlook.office365.com',
];

// 메일 한 통 또는 화면 하나 — 제목, 머리글(메일: 보낸 사람·시각, 화면: 주소), 본문 글자
export interface WebMail {
  subject: string;
  header: string;
  body: string;
}

// 모양만 맞춘다 — 길이 상한은 서버가 자른다(services/personal.mail_save_page).
export function webMailOf(raw: unknown): WebMail | null {
  if (!raw || typeof raw !== 'object') return null;
  const m = raw as Record<string, unknown>;
  const text = (v: unknown) => (typeof v === 'string' ? v : '');
  const mail = { subject: text(m.subject), header: text(m.header), body: text(m.body) };
  return mail.body || mail.header ? mail : null;
}

// 받는 창에서 「대화 시작」 — 메일(개인 맥락에 넣은 것)이나 화면(넣지 않고 붙이기만)으로 스마트워크 대화를 잇는다.
// 콘솔 탭 하나가 맡아(components/AgentRelay) 스마트워크 화면으로 가고, 화면(pages/Smartwork)이 꺼내 보낸다.
export interface MailChat {
  kind: 'mail' | 'page';
  sessionId: number | null;  // null = 새 업무
  text: string;
  mail: WebMail;
  path: string;              // 메일: 개인 저장소에 넣은 자리(outlook/web_….md)
  from: string;              // 보낸 사이트(출처 주소)
}

export const MAIL_CHAT_EVENT = 'gpax-mail-chat';
export const DEFAULT_MAIL_ASK = '이 메일을 읽고 요지와 내가 할 일을 정리해 줘';
export const DEFAULT_PAGE_ASK = '이 화면의 내용을 정리하고 내가 할 일을 알려 줘';
let pendingChat: MailChat | null = null;

export function offerMailChat(chat: MailChat): void {
  pendingChat = chat;
  window.dispatchEvent(new Event(MAIL_CHAT_EVENT));
}

export function takeMailChat(): MailChat | null {
  const chat = pendingChat;
  pendingChat = null;
  return chat;
}

// 대화에 붙이는 첨부 — 개인 저장소에 넣은 것과 같은 모양(services/personal.mail_save_page)
export function mailAttachment(mail: WebMail, kind: MailChat['kind'] = 'mail'): { name: string; type: string; data: string } {
  const subject = mail.subject.trim() || '(제목 없음)';
  const head = mail.header ? `- ${kind === 'page' ? '주소' : '머리글'}: ${mail.header.split(/\s+/).join(' ')}` : '';
  const text = [`# ${subject}`, '', head, '', mail.body, ''].join('\n');
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  const name = `${subject.replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 60).trim() || 'mail'}.md`;
  return { name, type: 'text/markdown', data: btoa(bin) };
}
