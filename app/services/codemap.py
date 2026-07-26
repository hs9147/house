"""코드 구조 정적 파싱 — 파일 → 클래스/함수 계층 트리 + 항목별 요약.

대화식 편집 화면(Chat)에서 로직 구조를 확대/축소로 확인하는 용도(요청 1)이자,
그 개요를 채팅 LLM 컨텍스트에 주입해 요청에 맞게 대응하도록 하는 용도(요청 2)다.

정적 파싱만 사용한다(LLM 호출 없음): Python은 표준 `ast`, JS/TS는 가벼운 정규식
추출기. 완전한 파서가 아니라 "구조 개요"를 목표로 하며, 외부 의존성을 추가하지 않는다.
"""
import ast
import re
from pathlib import Path

from .workspace import CONTEXT_EXTENSIONS, MAX_CONTEXT_FILE_BYTES, file_tree

# 구조를 추출할 코드 파일. 나머지(md/json/yaml 등 데이터 파일)는 로직 개요 대상이 아니라 제외.
PY_EXTS = {".py"}
JS_EXTS = {".js", ".jsx", ".ts", ".tsx"}
CODE_EXTS = PY_EXTS | JS_EXTS

# 개요가 지나치게 커지지 않도록 상한
MAX_FILES = 300
MAX_OUTLINE_CHARS = 12_000


def build_code_map(workdir: Path, limit: int = MAX_FILES) -> list[dict]:
    """리포의 코드 파일별 구조 트리를 반환한다.

    반환 형태:
      [{"path", "lang", "summary", "children": [Node, ...]}]
      Node = {"kind": "class"|"function"|"method", "name", "signature", "doc",
              "lineno", "children": [Node, ...]}
    """
    root = workdir.resolve()
    result: list[dict] = []
    for rel in file_tree(workdir, limit=2000):
        suffix = Path(rel).suffix.lower()
        if suffix not in CODE_EXTS or suffix not in CONTEXT_EXTENSIONS:
            continue
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            continue
        if p.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            continue
        source = p.read_text(encoding="utf-8", errors="replace")
        if suffix in PY_EXTS:
            summary, children = _parse_python(source)
            lang = "python"
        else:
            summary, children = _parse_js(source)
            lang = "typescript" if suffix in {".ts", ".tsx"} else "javascript"
        if not children and not summary:
            continue
        result.append({"path": rel, "lang": lang, "summary": summary, "children": children})
        if len(result) >= limit:
            break
    return result


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


# --- Python (ast) ---

def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in node.args.args]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({', '.join(args)})"


def _parse_python(source: str) -> tuple[str, list[dict]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "", []
    summary = _first_line(ast.get_docstring(tree))
    children: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            children.append({
                "kind": "function", "name": node.name,
                "signature": _py_signature(node), "doc": _first_line(ast.get_docstring(node)),
                "lineno": node.lineno, "children": [],
            })
        elif isinstance(node, ast.ClassDef):
            methods = [
                {
                    "kind": "method", "name": m.name, "signature": _py_signature(m),
                    "doc": _first_line(ast.get_docstring(m)), "lineno": m.lineno, "children": [],
                }
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            bases = ", ".join(_base_name(b) for b in node.bases)
            children.append({
                "kind": "class", "name": node.name,
                "signature": f"class {node.name}({bases})" if bases else f"class {node.name}",
                "doc": _first_line(ast.get_docstring(node)), "lineno": node.lineno,
                "children": methods,
            })
    return summary, children


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


# --- JS / TS (정규식 개요) ---

_JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)")
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?")
_JS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:\(([^)]*)\)|(\w+))\s*(?::[^=]+)?=>"
)


def _parse_js(source: str) -> tuple[str, list[dict]]:
    lines = source.splitlines()
    summary = ""
    for line in lines[:5]:
        s = line.strip()
        if s.startswith(("//", "/*", "*")):
            summary = s.lstrip("/*").strip()[:200]
            if summary:
                break
    children: list[dict] = []
    for i, line in enumerate(lines, start=1):
        m = _JS_CLASS.match(line)
        if m:
            name, base = m.group(1), m.group(2)
            sig = f"class {name} extends {base}" if base else f"class {name}"
            children.append({"kind": "class", "name": name, "signature": sig,
                             "doc": "", "lineno": i, "children": []})
            continue
        m = _JS_FUNC.match(line)
        if m:
            children.append({"kind": "function", "name": m.group(1),
                             "signature": f"function {m.group(1)}({m.group(2).strip()})",
                             "doc": "", "lineno": i, "children": []})
            continue
        m = _JS_ARROW.match(line)
        if m:
            params = (m.group(2) or m.group(3) or "").strip()
            children.append({"kind": "function", "name": m.group(1),
                             "signature": f"{m.group(1)}({params})",
                             "doc": "", "lineno": i, "children": []})
    return summary, children


# --- LLM 컨텍스트용 텍스트 개요 ---

def render_outline(code_map: list[dict], max_chars: int = MAX_OUTLINE_CHARS) -> str:
    """코드맵을 LLM 시스템 컨텍스트에 넣을 압축 텍스트 개요로 변환한다.

    파일 경로 → 클래스/함수 시그니처(+한 줄 요약)를 들여쓰기로 표현하고,
    상한(max_chars)을 넘으면 잘라내며 잘림을 명시한다.
    """
    lines: list[str] = []
    for f in code_map:
        header = f["path"]
        if f.get("summary"):
            header += f"  # {f['summary']}"
        lines.append(header)
        for node in f["children"]:
            lines.append(_outline_node(node, indent=1))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n… (구조 개요가 잘렸습니다)"
    return text


def _outline_node(node: dict, indent: int) -> str:
    pad = "  " * indent
    line = f"{pad}{node['signature']}"
    if node.get("doc"):
        line += f"  # {node['doc']}"
    for child in node.get("children", []):
        line += "\n" + _outline_node(child, indent + 1)
    return line
