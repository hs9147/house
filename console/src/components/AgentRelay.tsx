import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { AGENT_CHANNEL, offerMailChat, webMailOf } from '../lib/webmail';

/**
 * 로그인한 콘솔 탭에서 — 통합 북마크가 연 받는 창(pages/AgentReceive)과 같은 브라우저 안에서 주고받는다.
 * 받는 창에는 로그인이 따라오지 않는다(다른 사이트가 연 창) — 콘솔 API는 이 탭이 부른다.
 *
 *   claim-offer → 맡겠다고 손든다(스마트워크를 연 탭인지·보이는 탭인지 함께). 받는 창이 **한 탭만** 고르고,
 *                 그 뒤의 요청은 고른 탭(tab)에만 온다 — 여러 탭이 같은 일을 하면 대화·올리기가 겹친다.
 *   mail        → 웹 Outlook 메일을 이 사람의 개인 업무 맥락에 넣는다.
 *   source      → 그 사이트(주소)로 등록한 데이터 수집 출처와 수집 조건
 *   upload      → 북마크의 스캔·녹화·봇 결과를 그 출처에 올린다.
 *   sessions    → 고를 수 있는 스마트워크 업무 목록
 *   chat        → 스마트워크로 가서 그 업무를 열고 메일·화면을 붙여 보낸다(pages/Smartwork).
 */
export default function AgentRelay() {
  const navigate = useNavigate();
  useEffect(() => {
    const me = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const channel = new BroadcastChannel(AGENT_CHANNEL);
    const reply = (msg: Record<string, unknown>) => channel.postMessage(msg);
    channel.onmessage = async (e: MessageEvent) => {
      const m = e.data;
      if (!m?.id || typeof m.gpax !== 'string') return;
      if (m.gpax === 'claim-offer') {
        reply({ gpax: 'claim', id: m.id, tab: me, smartwork: location.hash.startsWith('#/smartwork'),
                visible: document.visibilityState === 'visible' });
        return;
      }
      if (m.tab !== me) return;
      try {
        if (m.gpax === 'mail') {
          const mail = webMailOf(m.mail);
          if (!mail) throw new Error('읽은 메일이 비어 있습니다.');
          const out = await api.saveWebMail([mail]);
          reply({ gpax: 'mail-ack', id: m.id, ok: true, path: out.saved[0]?.path ?? '',
                  text: out.new ? '개인 업무 맥락에 넣었습니다.' : '이미 넣은 메일입니다.' });
        } else if (m.gpax === 'source') {
          const origin = String(m.origin || '');
          const mine = (await api.listSources()).filter((s) => {
            try {
              return s.kind === 'web' && new URL(s.url).origin === origin;
            } catch {
              return false;
            }
          });
          const full = await Promise.all(mine.slice(0, 10).map((s) => api.getSource(s.id)));
          reply({ gpax: 'source-reply', id: m.id, sources: full.map((s) => ({
            id: s.id, name: s.name, origin, queries: s.queries ?? [], version: s.queries_version ?? '',
          })) });
        } else if (m.gpax === 'upload') {
          const kind = m.kind === 'record' ? 'record' : 'scan';
          const file = new File([JSON.stringify(m.payload)], `gpax-${kind}-${m.sourceId}.json`, { type: 'application/json' });
          if (kind === 'record') {
            const out = await api.uploadSourceRecording(Number(m.sourceId), file);
            reply({ gpax: 'upload-ack', id: m.id, ok: true, text: `녹화를 수집 조건에 올렸습니다 — 화면 ${out.screens.length}개` });
          } else {
            await api.uploadBrowserScan(Number(m.sourceId), file);
            reply({ gpax: 'upload-ack', id: m.id, ok: true, text: '결과를 올렸습니다 — 콘솔의 데이터 수집에서 확인하세요' });
          }
        } else if (m.gpax === 'sessions') {
          const list = await api.listSessions();
          reply({ gpax: 'sessions-reply', id: m.id, sessions: list.slice(0, 30).map((s) => ({
            id: s.id,
            label: `${s.title || '(새 업무)'}${s.workflow ?? s.organization ? ` · ${s.workflow ?? s.organization}` : ''}`,
          })) });
        } else if (m.gpax === 'chat') {
          const mail = webMailOf(m.mail);
          if (!mail) throw new Error('붙일 내용이 비어 있습니다.');
          offerMailChat({ kind: m.kind === 'page' ? 'page' : 'mail',
                          sessionId: typeof m.sessionId === 'number' ? m.sessionId : null,
                          text: String(m.text || ''), mail, path: String(m.path || ''), from: String(m.from || '') });
          navigate('/smartwork');
          reply({ gpax: 'chat-ack', id: m.id, ok: true });
        }
      } catch (err) {
        reply({ gpax: `${m.gpax}-ack`, id: m.id, ok: false, text: (err as Error).message });
      }
    };
    return () => channel.close();
  }, [navigate]);
  return null;
}
