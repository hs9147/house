// 웹 Outlook 북마크(lib/mailAgent.js)가 넘긴 메일 — 받는 창(pages/MailReceive)과 로그인한 콘솔 탭
// (components/MailRelay)이 같은 브라우저 안에서 이 채널로 주고받는다. 같은 출처(콘솔)만 쓸 수 있다.
export const MAIL_CHANNEL = 'gpax-mail';

// 메일을 보내도 되는 곳 — 웹 Outlook 주소만(받는 창은 다른 사이트가 열어도 이 출처가 아니면 받지 않는다).
export const OUTLOOK_ORIGINS = [
  'https://outlook.cloud.microsoft',
  'https://outlook.office.com',
  'https://outlook.office365.com',
];

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

// 받는 창에서 「대화 시작」 — 메일을 넣은 뒤 그 메일로 스마트워크 대화를 잇는다. 콘솔 탭 하나가 맡아
// (components/MailRelay) 스마트워크 화면으로 가고, 화면(pages/Smartwork)이 이것을 꺼내 보낸다.
export interface MailChat {
  sessionId: number | null;  // null = 새 업무
  text: string;
  mail: WebMail;
  path: string;              // 개인 저장소에 넣은 자리(outlook/web_….md)
}

export const MAIL_CHAT_EVENT = 'gpax-mail-chat';
export const DEFAULT_MAIL_ASK = '이 메일을 읽고 요지와 내가 할 일을 정리해 줘';
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
export function mailAttachment(mail: WebMail): { name: string; type: string; data: string } {
  const subject = mail.subject.trim() || '(제목 없음)';
  const text = [`# ${subject}`, '', mail.header ? `- 머리글: ${mail.header.split(/\s+/).join(' ')}` : '', '',
                mail.body, ''].join('\n');
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  const name = `${subject.replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 60).trim() || 'mail'}.md`;
  return { name, type: 'text/markdown', data: btoa(bin) };
}
