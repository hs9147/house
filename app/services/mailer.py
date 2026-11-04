"""메일 발송 — 사내 SMTP 중계로, **시스템 주소(noreply)에서** 보낸다(MCP /mcp/mail).

중계(lgesmtp.lge.com:25)는 인증 없이 사내 수신자만 받는다(실측: 외부 주소는 5.1.2로 거절). 대신 발신
주소는 검사하지 않는다 — 그래서 보내는 사람을 **설정(mail_from)으로 고정**한다. 요청한 사람의 주소로
보내게 두면 누구나 남의 이름으로 메일을 보낼 수 있다. 요청한 사람은 회신 주소(Reply-To)와 본문 끝
표기(「…님이 GPAX에서 보냄」), 머리글 X-GPAX-Requested-By로 남는다.

막는 것: 받는 사람은 허용한 도메인만, 한 번에 받는 사람 수, 보내는 사람마다 한 시간 발송 수, 본문·첨부
크기. 본문은 감사 기록에 남기지 않는다(받는 사람 수·제목만 — 호출하는 쪽이 남긴다).
"""
import base64
import re
import smtplib
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from ..config import get_settings

TIMEOUT = 30.0
MAX_ATTACHMENTS = 10
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
FOOTER = "\n\n—\n{actor}님이 GPAX에서 보낸 메일입니다. 이 주소({sender})는 회신을 받지 않습니다{reply}."

_sent: dict[str, list[float]] = {}   # 보내는 사람 → 최근 한 시간의 발송 시각(프로세스 안에서만)
_lock = threading.Lock()


class MailError(ValueError):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host.strip() and s.mail_from.strip())


def allowed_domains() -> list[str]:
    s = get_settings()
    raw = s.mail_allowed_domains.strip() or s.allowed_email_domain
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def _addresses(values, what: str) -> list[str]:
    if values in (None, ""):
        return []
    items = values if isinstance(values, list) else re.split(r"[,;\s]+", str(values))
    out, domains = [], allowed_domains()
    for raw in items:
        address = parseaddr(str(raw).strip())[1].strip()
        if not address:
            continue
        if not _EMAIL.match(address):
            raise MailError(f"{what} 주소가 메일 주소가 아닙니다: {str(raw)[:80]}")
        if address.split("@", 1)[1].lower() not in domains:
            raise MailError(f"{what} {address} — 사내 주소({', '.join(domains)})로만 보낼 수 있습니다.")
        if address.lower() not in {a.lower() for a in out}:
            out.append(address)
    return out


def remaining(actor: str) -> int:
    with _lock:
        now = time.time()
        recent = [t for t in _sent.get(actor, []) if now - t < 3600]
        _sent[actor] = recent
        return max(0, get_settings().mail_max_per_hour - len(recent))


def status(actor: str) -> dict:
    s = get_settings()
    return {"configured": configured(), "from": s.mail_from if configured() else "",
            "allowed_domains": allowed_domains(), "max_recipients": s.mail_max_recipients,
            "max_bytes": s.mail_max_bytes, "per_hour": s.mail_max_per_hour, "remaining": remaining(actor)}


def build(actor: str, to, subject: str, body: str, cc=None, html: bool = False,
          attachments: list[dict] | None = None) -> tuple[EmailMessage, list[str]]:
    """검사하고 메일을 만든다 — (메시지, 모든 받는 사람). 보내지는 않는다."""
    s = get_settings()
    if not configured():
        raise MailError("메일 발송이 설정되지 않았습니다 — 관리자가 PAAS_SMTP_HOST·PAAS_MAIL_FROM을 지정해야 합니다.")
    to_list, cc_list = _addresses(to, "받는 사람"), _addresses(cc, "참조")
    if not to_list:
        raise MailError("받는 사람이 없습니다.")
    if len(to_list) + len(cc_list) > s.mail_max_recipients:
        raise MailError(f"받는 사람과 참조는 합쳐 {s.mail_max_recipients}명까지입니다.")
    subject = re.sub(r"[\r\n]+", " ", str(subject or "")).strip()[:200]
    if not subject:
        raise MailError("제목이 비어 있습니다.")
    body = str(body or "")
    if not body.strip():
        raise MailError("본문이 비어 있습니다.")

    name, sender = parseaddr(s.mail_from)
    reply = actor if _EMAIL.match(actor or "") else ""
    footer = FOOTER.format(actor=actor or "GPAX", sender=sender,
                           reply=f" — 회신은 {reply}(으)로 갑니다" if reply else "")
    msg = EmailMessage()
    msg["From"] = formataddr((name or "GPAX", sender))
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if reply:
        msg["Reply-To"] = reply
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg["X-GPAX-Requested-By"] = re.sub(r"[\r\n]+", " ", actor or "")[:200]
    if html:
        safe_footer = footer.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        msg.set_content(re.sub(r"<[^>]+>", "", body) + footer)          # 글자만 보는 메일 프로그램용
        msg.add_alternative(f"{body}<p style=\"color:#666;font-size:12px\">{safe_footer}</p>", subtype="html")
    else:
        msg.set_content(body + footer)

    total = len(body.encode("utf-8"))
    attachments = attachments if isinstance(attachments, list) else []
    if len(attachments) > MAX_ATTACHMENTS:
        raise MailError(f"첨부는 {MAX_ATTACHMENTS}개까지입니다.")
    for a in attachments:
        if not isinstance(a, dict) or not str(a.get("name") or "").strip():
            raise MailError("첨부에는 이름(name)과 base64 내용(data)이 있어야 합니다.")
        try:
            data = base64.b64decode(str(a.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            raise MailError(f"첨부 {a.get('name')}의 내용이 base64가 아닙니다.")
        total += len(data)
        ctype = str(a.get("type") or "application/octet-stream")
        main, _, sub = ctype.partition("/")
        msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream",
                           filename=re.sub(r"[\r\n\\/]+", "_", str(a["name"]))[:120])
    if total > s.mail_max_bytes:
        raise MailError(f"본문과 첨부가 {s.mail_max_bytes // 1_000_000}MB를 넘습니다.")
    return msg, to_list + cc_list


def send(actor: str, to, subject: str, body: str, cc=None, html: bool = False,
         attachments: list[dict] | None = None) -> dict:
    """보낸다 — 검사(받는 사람·크기·발송 수)를 다 지난 뒤에만 중계에 붙는다."""
    msg, recipients = build(actor, to, subject, body, cc, html, attachments)
    with _lock:
        now = time.time()
        recent = [t for t in _sent.get(actor, []) if now - t < 3600]
        if len(recent) >= get_settings().mail_max_per_hour:
            raise MailError(f"한 시간에 {get_settings().mail_max_per_hour}통까지 보낼 수 있습니다 — 잠시 뒤에 보내세요.")
        recent.append(now)
        _sent[actor] = recent
    s = get_settings()
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=TIMEOUT) as smtp:
            smtp.ehlo()
            if s.smtp_starttls and smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            refused = smtp.send_message(msg, from_addr=parseaddr(s.mail_from)[1], to_addrs=recipients)
    except smtplib.SMTPRecipientsRefused as e:
        raise MailError("중계가 받는 사람을 모두 거절했습니다: " + ", ".join(
            f"{r} ({c} {m.decode(errors='replace')[:80]})" for r, (c, m) in e.recipients.items()))
    except (smtplib.SMTPException, OSError) as e:
        with _lock:   # 보내지 못한 것은 발송 수에서 뺀다
            if _sent.get(actor):
                _sent[actor].pop()
        raise MailError(f"메일 서버에 보내지 못했습니다: {type(e).__name__}: {e}"[:300])
    return {"sent": True, "message_id": msg["Message-ID"], "recipients": len(recipients),
            "refused": {r: f"{c} {m.decode(errors='replace')[:80]}" for r, (c, m) in (refused or {}).items()},
            "from": msg["From"]}
