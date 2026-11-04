"""메일 발송(services/mailer · MCP /mcp/mail) — 사내 SMTP 중계, 시스템 주소에서 사내 수신자에게만.

중계를 가짜(smtplib.SMTP)로 바꿔 실제로는 보내지 않는다. 보는 것: 보내는 사람이 늘 시스템 주소인지(사칭 방지),
받는 사람 도메인·인원·크기·한 시간 발송 수, 보내지 못한 것은 발송 수에서 빠지는지, 감사에 본문이 없는지."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import mailer

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"
SENDER = "GPAX <gpax-noreply@lge.com>"


class FakeSmtp:
    sent: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls = host, port, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return 250, b"ok"

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self):
        self.tls = True

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if FakeSmtp.fail:
            raise FakeSmtp.fail
        FakeSmtp.sent.append({"msg": msg, "from": from_addr, "to": to_addrs, "tls": self.tls,
                              "host": (self.host, self.port)})
        return {}


@pytest.fixture
def smtp(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_SMTP_HOST", "lgesmtp.lge.com")
    monkeypatch.setenv("PAAS_MAIL_FROM", SENDER)
    monkeypatch.setenv("PAAS_MAIL_ALLOWED_DOMAINS", "lge.com")
    get_settings.cache_clear()
    FakeSmtp.sent, FakeSmtp.fail = [], None
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    mailer._sent.clear()
    return FakeSmtp


def _call(c, tool, args=None):
    return c.post(f"{API}/mcp/mail", headers=ADMIN, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}).json()


def test_mail_goes_out_from_the_system_address_with_the_requester_as_reply_to(smtp):
    out = mailer.send("chs.jo@lge.com", ["kim@lge.com", "Lee <lee@LGE.com>", "kim@lge.com"], "주간 보고",
                      "이번 주 진행 상황입니다.", cc="park@lge.com",
                      attachments=[{"name": "보고.txt", "type": "text/plain",
                                    "data": base64.b64encode("첨부 내용".encode()).decode()}])
    assert out["sent"] and out["recipients"] == 3       # 같은 주소는 한 번
    sent = smtp.sent[0]
    msg = sent["msg"]
    assert sent["from"] == "gpax-noreply@lge.com" and sent["tls"] and sent["host"] == ("lgesmtp.lge.com", 25)
    assert msg["From"] == SENDER and msg["Reply-To"] == "chs.jo@lge.com"
    assert msg["X-GPAX-Requested-By"] == "chs.jo@lge.com" and msg["Cc"] == "park@lge.com"
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "이번 주 진행 상황입니다." in body and "chs.jo@lge.com님이 GPAX에서 보낸 메일" in body
    assert "회신은 chs.jo@lge.com" in body
    assert [p.get_filename() for p in msg.iter_attachments()] == ["보고.txt"]


def test_only_company_recipients_within_limits(smtp, monkeypatch):
    with pytest.raises(mailer.MailError, match="사내 주소"):
        mailer.send("a@lge.com", ["someone@gmail.com"], "제목", "본문")
    with pytest.raises(mailer.MailError, match="메일 주소가 아닙니다"):
        mailer.send("a@lge.com", ["그냥 글자"], "제목", "본문")
    with pytest.raises(mailer.MailError, match="받는 사람이 없습니다"):
        mailer.send("a@lge.com", [], "제목", "본문")
    monkeypatch.setenv("PAAS_MAIL_MAX_RECIPIENTS", "2")
    monkeypatch.setenv("PAAS_MAIL_MAX_BYTES", "100")
    get_settings.cache_clear()
    with pytest.raises(mailer.MailError, match="2명까지"):
        mailer.send("a@lge.com", ["b@lge.com", "c@lge.com", "d@lge.com"], "제목", "본문")
    with pytest.raises(mailer.MailError, match="MB를 넘습니다"):
        mailer.send("a@lge.com", ["b@lge.com"], "제목", "가" * 200)
    with pytest.raises(mailer.MailError, match="base64"):
        mailer.send("a@lge.com", ["b@lge.com"], "제목", "본문", attachments=[{"name": "x", "data": "@@@"}])
    assert smtp.sent == []                                  # 검사에 걸린 것은 중계에 붙지도 않는다


def test_hourly_limit_and_failed_sends_do_not_count(smtp, monkeypatch):
    monkeypatch.setenv("PAAS_MAIL_MAX_PER_HOUR", "2")
    get_settings.cache_clear()
    smtp.fail = OSError("연결 끊김")
    with pytest.raises(mailer.MailError, match="보내지 못했습니다"):
        mailer.send("a@lge.com", ["b@lge.com"], "제목", "본문")
    assert mailer.remaining("a@lge.com") == 2             # 못 보낸 것은 세지 않는다
    smtp.fail = None
    mailer.send("a@lge.com", ["b@lge.com"], "제목", "본문")
    mailer.send("a@lge.com", ["b@lge.com"], "제목", "본문")
    with pytest.raises(mailer.MailError, match="한 시간에 2통"):
        mailer.send("a@lge.com", ["b@lge.com"], "제목", "본문")
    assert mailer.remaining("other@lge.com") == 2          # 보내는 사람마다 따로


def test_html_mail_keeps_a_plain_text_part(smtp):
    mailer.send("a@lge.com", ["b@lge.com"], "제목", "<h1>요약</h1><p>내용</p>", html=True)
    msg = smtp.sent[0]["msg"]
    assert "<h1>요약</h1>" in msg.get_body(preferencelist=("html",)).get_content()
    assert "요약내용" in msg.get_body(preferencelist=("plain",)).get_content().replace("\n", "")


def test_mcp_mail_server_sends_and_audits_without_the_body(smtp):
    c = TestClient(create_app())
    tools = c.post(f"{API}/mcp/mail", headers=ADMIN, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert [t["name"] for t in tools["result"]["tools"]] == ["send_mail", "mail_status"]
    status = _call(c, "mail_status")["result"]["content"][0]["text"]
    assert '"configured": true' in status and "lge.com" in status
    bad = _call(c, "send_mail", {"to": ["x@gmail.com"], "subject": "s", "body": "b"})
    assert "사내 주소" in bad["error"]["message"]
    ok = _call(c, "send_mail", {"to": ["b@lge.com"], "subject": "배포 완료", "body": "비밀-본문-내용"})
    assert '"sent": true' in ok["result"]["content"][0]["text"]
    assert smtp.sent[-1]["msg"]["From"] == SENDER and smtp.sent[-1]["msg"]["Reply-To"] is None   # 키 이름은 주소가 아니다

    from app.db import SessionLocal
    from app.models import AuditEvent
    with SessionLocal() as s:
        event = s.query(AuditEvent).filter(AuditEvent.action == "mcp.mail.send").one()
        assert event.detail["subject"] == "배포 완료" and event.detail["recipients"] == 1
        assert "비밀-본문-내용" not in str(event.detail) and "b@lge.com" not in str(event.detail)


def test_mail_server_is_listed_only_when_configured(monkeypatch, fresh_settings):
    c = TestClient(create_app())
    names = lambda: [s["id"] for s in c.get(f"{API}/mcp/search", headers=ADMIN).json()]  # noqa: E731
    monkeypatch.setenv("PAAS_SMTP_HOST", "")
    get_settings.cache_clear()
    assert "paas-mail" not in names()
    off = _call(c, "send_mail", {"to": ["b@lge.com"], "subject": "s", "body": "b"})
    assert "설정되지 않았습니다" in off["error"]["message"]
    monkeypatch.setenv("PAAS_SMTP_HOST", "lgesmtp.lge.com")
    monkeypatch.setenv("PAAS_MAIL_FROM", SENDER)
    get_settings.cache_clear()
    assert "paas-mail" in names()
