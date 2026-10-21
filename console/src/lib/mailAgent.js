/* GPAX 메일 — 웹 Outlook(outlook.cloud.microsoft 등)에서 연 메일 한 통을 콘솔의 개인 업무 맥락으로 넘기는
 * 북마크릿. Graph 권한 없이, 사람이 고른 메일만 넘어간다.
 *
 * 웹 Outlook은 다른 사이트가 연 탭과의 연결을 끊고(COOP same-origin-allow-popups) GPAX로 직접 보내는 요청도
 * 막는다(CSP connect-src). 그래서 거꾸로 — 이 북마크가 콘솔의 받는 창을 팝업으로 열고(자기가 연 팝업과는
 * 연결이 남는다) 그 창에 postMessage로 넘긴다. 받는 창은 보낸 쪽이 웹 Outlook 주소인지 보고, 로그인한 콘솔
 * 탭으로 넘겨 저장한다(pages/MailReceive.tsx).
 *
 * 웹 Outlook은 Trusted Types를 강제한다 — innerHTML·DOMParser를 쓰지 않고 글자(innerText·textContent)만
 * 읽는다. 주석은 블록 주석만 쓴다(북마크로 만들 때 줄을 붙인다). */
(function (GPAX) {
  function clean(s, cap) { return String(s || '').replace(/[ \t\u00a0]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, cap); }
  var hosts = /(^|\.)(outlook\.cloud\.microsoft|outlook\.office\.com|outlook\.office365\.com)$/;
  if (!hosts.test(location.hostname)) {
    alert('GPAX 메일 — 웹 Outlook(outlook.cloud.microsoft)에서 메일을 연 채 누르세요.');
    return;
  }
  var body = document.querySelector('[aria-label="메시지 본문"], [aria-label="Message body"]');
  if (!body) {
    alert('GPAX 메일 — 열린 메일을 찾지 못했습니다. 메일 하나를 읽기 창에 연 채 누르세요.');
    return;
  }
  /* 제목·머리글은 본문을 감싼 가장 가까운 상자 — 제목(heading)이 처음 들어오는 곳까지 올라간다. */
  var box = body.parentElement;
  while (box && box !== document.body && !box.querySelector('[role="heading"]')) box = box.parentElement;
  var heading = box && box.querySelector('[role="heading"]');
  var header = '';
  if (box && box !== document.body) {
    var copy = box.cloneNode(true);
    var inner = copy.querySelector('[aria-label="메시지 본문"], [aria-label="Message body"]');
    if (inner) inner.remove();
    var h = copy.querySelector('[role="heading"]');
    if (h) h.remove();
    header = clean(copy.textContent, 2000);
  }
  var mail = { subject: clean(heading && heading.innerText, 300), header: header,
               body: clean(body.innerText, 200000) };

  var win = window.open(GPAX.receive, 'gpax-mail', 'width=520,height=420');
  if (!win) {
    alert('GPAX 메일 — 받는 창이 막혔습니다. 이 사이트의 팝업을 허용하고 다시 누르세요.');
    return;
  }
  var sent = false;
  function onMessage(e) {
    if (e.origin !== GPAX.console || e.source !== win || !e.data || e.data.gpax !== 'ready' || sent) return;
    sent = true;
    win.postMessage({ gpax: 'mail', mail: mail }, GPAX.console);
    window.removeEventListener('message', onMessage);
  }
  window.addEventListener('message', onMessage);
  setTimeout(function () {
    if (!sent) {
      window.removeEventListener('message', onMessage);
      alert('GPAX 메일 — 받는 창이 답하지 않습니다. 콘솔 주소(' + GPAX.console + ')가 열리는지 보세요.');
    }
  }, 15000);
})(__GPAX_MAIL__);
