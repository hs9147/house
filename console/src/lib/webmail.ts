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
