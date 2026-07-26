"""코드 구조 정적 파싱 — Python/JS 파서, 개요 렌더링, /codemap 엔드포인트."""
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import codemap

ADMIN = {"x-api-key": "test-admin-key"}


def test_parse_python_classes_and_functions():
    src = (
        '"""모듈 요약."""\n'
        "import os\n\n"
        "def top(a, b):\n"
        '    """더한다."""\n'
        "    return a + b\n\n"
        "class Svc(Base):\n"
        '    """서비스."""\n'
        "    def run(self, x):\n"
        "        return x\n"
    )
    summary, children = codemap._parse_python(src)
    assert summary == "모듈 요약."
    kinds = {c["kind"]: c for c in children}
    assert kinds["function"]["signature"] == "def top(a, b)"
    assert kinds["function"]["doc"] == "더한다."
    cls = kinds["class"]
    assert cls["signature"] == "class Svc(Base)"
    assert cls["children"][0]["signature"] == "def run(self, x)"


def test_parse_python_syntax_error_is_soft():
    summary, children = codemap._parse_python("def broken(:\n")
    assert summary == "" and children == []


def test_parse_js_functions_classes_arrows():
    src = (
        "// Chat 컴포넌트\n"
        "export default function Chat() {}\n"
        "const groupResources = (items) => {};\n"
        "export class Foo extends Bar {}\n"
    )
    summary, children = codemap._parse_js(src)
    assert summary == "Chat 컴포넌트"
    names = {c["name"] for c in children}
    assert names == {"Chat", "groupResources", "Foo"}
    foo = next(c for c in children if c["name"] == "Foo")
    assert foo["kind"] == "class" and "extends Bar" in foo["signature"]


def test_render_outline_indents_and_truncates():
    cmap = [{
        "path": "app/x.py", "lang": "python", "summary": "요약",
        "children": [{
            "kind": "class", "name": "C", "signature": "class C", "doc": "클래스",
            "lineno": 1, "children": [
                {"kind": "method", "name": "m", "signature": "def m(self)", "doc": "",
                 "lineno": 2, "children": []},
            ],
        }],
    }]
    out = codemap.render_outline(cmap)
    assert "app/x.py  # 요약" in out
    assert "  class C  # 클래스" in out
    assert "    def m(self)" in out
    # 상한 초과 시 잘림 표기
    truncated = codemap.render_outline(cmap, max_chars=10)
    assert "잘렸습니다" in truncated


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    # encoding 명시 필수 — Windows 기본 인코딩(cp1252)은 한글을 못 써서 UnicodeEncodeError.
    (path / "svc.py").write_text('"""서비스."""\ndef handler(req):\n    return req\n', encoding="utf-8")
    (path / "ui.tsx").write_text("export function App() {}\n", encoding="utf-8")
    (path / "data.json").write_text("{}\n", encoding="utf-8")  # 데이터 파일은 트리에서 제외되어야 함
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=path, check=True,
    )


def test_build_code_map_over_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    cmap = codemap.build_code_map(repo)
    paths = {f["path"]: f for f in cmap}
    assert "svc.py" in paths and "ui.tsx" in paths
    assert "data.json" not in paths  # 데이터 파일 제외
    assert paths["svc.py"]["children"][0]["name"] == "handler"


def test_codemap_endpoint(monkeypatch, tmp_path):
    from app.api import llm as llm_api

    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(llm_api, "checkout", lambda project: (repo, "sha"))

    c = TestClient(create_app())
    pid = c.post("/paas/api/v1/projects", json={
        "name": "codemap-target", "type": "python", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]

    r = c.get(f"/paas/api/v1/projects/{pid}/codemap", headers=ADMIN)
    assert r.status_code == 200, r.text
    files = {f["path"] for f in r.json()["files"]}
    assert "svc.py" in files and "ui.tsx" in files
