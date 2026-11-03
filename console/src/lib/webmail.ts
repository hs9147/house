import type { SmartworkSession } from './types';

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

// 받는 창의 스마트워크 대화 — 메일(개인 맥락에 넣은 것)이나 화면(넣지 않고 붙이기만)으로 시작해 **받는 창에서**
// 이어 간다. 콘솔 API는 콘솔 탭 하나가 부르고(components/AgentRelay) 대화는 받는 창(pages/AgentReceive)에 보인다.
export type ChatKind = 'mail' | 'page';

export const DEFAULT_MAIL_ASK = '이 메일을 읽고 요지와 내가 할 일을 정리해 줘';
export const DEFAULT_PAGE_ASK = '이 화면의 내용을 정리하고 내가 할 일을 알려 줘';

// 받는 창에 보이는 대화 — 글·진행 과정·첨부 이름·고를 것. 보고서·웹·파일·에이전트 화면(panel)은 콘솔에서 본다.
export interface PopupMessage {
  id: number;
  role: 'user' | 'assistant';
  author: string;
  content: string;
  steps: string[];
  attachments: string[];
  choices: { label: string; prompt: string }[];
  suggestions: { title: string; prompt: string }[];
  panel: boolean;
}
export interface PopupSession { id: number; title: string; messages: PopupMessage[] }
const POPUP_MESSAGES = 40;

export function popupSession(s: SmartworkSession): PopupSession {
  return {
    id: s.id, title: s.title,
    messages: s.messages.slice(-POPUP_MESSAGES).map((m) => ({
      id: m.id, role: m.role, author: m.author, content: m.content, steps: m.steps ?? [],
      attachments: (m.attachments ?? []).map((a) => a.name),
      choices: (m.choices ?? []).map((c) => ({ label: c.label, prompt: c.prompt })),
      suggestions: (m.suggestions ?? []).map((x) => ({ title: x.title, prompt: x.prompt })),
      panel: !!(m.report || m.web || m.file || m.agent),
    })),
  };
}

// 받는 창의 「콘솔에서 보기」 — 콘솔 탭이 스마트워크로 가서 그 업무를 연다(pages/Smartwork가 꺼낸다).
export const OPEN_SESSION_EVENT = 'gpax-open-session';
let pendingSession: number | null = null;

export function offerSession(id: number): void {
  pendingSession = id;
  window.dispatchEvent(new Event(OPEN_SESSION_EVENT));
}

export function takeSession(): number | null {
  const id = pendingSession;
  pendingSession = null;
  return id;
}

// 대화에 붙이는 첨부 — 개인 저장소에 넣은 것과 같은 모양(services/personal.mail_save_page)
export function mailAttachment(mail: WebMail, kind: ChatKind = 'mail'): { name: string; type: string; data: string } {
  const subject = mail.subject.trim() || '(제목 없음)';
  const head = mail.header ? `- ${kind === 'page' ? '주소' : '머리글'}: ${mail.header.split(/\s+/).join(' ')}` : '';
  const text = [`# ${subject}`, '', head, '', mail.body, ''].join('\n');
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  const name = `${subject.replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 60).trim() || 'mail'}.md`;
  return { name, type: 'text/markdown', data: btoa(bin) };
}
