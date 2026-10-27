import mailAgent from './mailAgent.js?raw';
import scanAgent from './scanAgent.js?raw';
import scanData from './scanData.js?raw';

// 통합 북마크 「GPAX」 — 사람마다·사이트마다 같은 코드 하나. 누른 곳이 웹 Outlook이면 메일(mailAgent.js),
// 아니면 스캔·녹화·봇·스마트워크로(scanAgent.js — 조회 응답 읽기는 scanData.js를 함께 넘긴다). 코드에는 이 콘솔의 주소만 들어간다 — 사이트의 출처 정보와
// 수집 조건은 누를 때 콘솔(또는 받는 창 pages/AgentReceive)이 보낸다.
//
// 둘 다 식 하나(`(function (X) {…})`)다. 북마크에 통째로 들어간다 — 주석과 들여쓰기만 덜어 낸다(두 코드에는
// 줄 주석 //가 없다). 웹 Outlook은 받아 온 코드 실행(eval)을 막으므로 코드는 북마크에 다 들어 있어야 한다.
const strip = (code: string) => code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\n\s+/g, '\n').trim().replace(/;$/, '');

export const OUTLOOK_HOSTS = /(^|\.)(outlook\.cloud\.microsoft|outlook\.office\.com|outlook\.office365\.com)$/;

const AGENT = `(function (GPAX) {
var OUTLOOK = /${OUTLOOK_HOSTS.source}/;
if (OUTLOOK.test(location.hostname)) (${strip(mailAgent)})(GPAX);
else (${strip(scanAgent)})({ console: GPAX.console, receive: GPAX.receive }, ${strip(scanData)});
})(__GPAX__);`;

// 받는 창은 이 콘솔의 #/agent — 다른 사이트가 연 창이라 로그인이 따라오지 않는다(로그인한 콘솔 탭이 받는다).
export function agentCode(base: { origin: string; pathname: string } = location): string {
  return AGENT.replace('__GPAX__', JSON.stringify({
    console: base.origin, receive: `${base.origin}${base.pathname}#/agent`,
  }));
}
