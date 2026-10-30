"""스마트워크 html 보고서 → PDF(services/htmlpdf). 글자가 남는 PDF, 스크립트·바깥 자원은 막는다."""
import io

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import htmlpdf

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def test_document_puts_the_csp_before_the_report():
    doc = htmlpdf.document("<html><head><script>x()</script></head><body>본문</body></html>", "제목<b>")
    assert doc.startswith(htmlpdf.CSP) and "default-src 'none'" in doc
    assert "<title>제목b</title>" in doc and "@page" in doc


@pytest.mark.skipif(not htmlpdf.engine(), reason="이 기계에 PDF를 만들 브라우저가 없다")
def test_real_pdf_keeps_text_and_runs_no_script_or_outside_resource():
    from pypdf import PdfReader

    c = TestClient(create_app())
    html = ("<h1>3분기 협력사 보고서</h1><p>에이전자는 배터리를 공급한다.</p>"
            "<script>document.body.innerHTML='HACKED'</script>"
            "<img src='file:///C:/Windows/win.ini' alt='ALTONLY'>")
    r = c.post(f"{API}/smartwork/report-pdf", json={"title": "3분기 보고서", "html": html}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "filename*=UTF-8''3%EB%B6%84%EA%B8%B0" in r.headers["content-disposition"]
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "협력사 보고서" in text and "HACKED" not in text            # 글자는 남고 스크립트는 돌지 않는다


def test_no_browser_means_503_so_the_screen_opens_the_print_dialog(monkeypatch):
    monkeypatch.setattr(htmlpdf, "engine", lambda: "")
    r = TestClient(create_app()).post(f"{API}/smartwork/report-pdf", json={"title": "t", "html": "<p>x</p>"},
                                      headers=ADMIN)
    assert r.status_code == 503 and "브라우저" in r.json()["detail"]


def test_too_large_or_unauthenticated_is_refused(monkeypatch):
    monkeypatch.setattr(htmlpdf, "MAX_HTML_BYTES", 10)
    c = TestClient(create_app())
    assert c.post(f"{API}/smartwork/report-pdf", json={"title": "t", "html": "x" * 50}, headers=ADMIN).status_code == 422
    assert c.post(f"{API}/smartwork/report-pdf", json={"title": "t", "html": "x"}).status_code == 401


def test_the_screen_asks_first_whether_pdf_is_possible(monkeypatch):
    """화면은 미리 묻고, 서버가 만들 수 없으면 PDF 단추를 감추고 HTML 다운로드만 보인다."""
    c = TestClient(create_app())
    monkeypatch.setattr(htmlpdf, "engine", lambda: "")
    assert c.get(f"{API}/smartwork/report-pdf", headers=ADMIN).json() == {"available": False}
    monkeypatch.setattr(htmlpdf, "engine", lambda: "browser")
    assert c.get(f"{API}/smartwork/report-pdf", headers=ADMIN).json() == {"available": True}
    assert c.get(f"{API}/smartwork/report-pdf").status_code == 401
