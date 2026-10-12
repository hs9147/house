"""LLM 호출 관측 — OpenTelemetry 규약으로 재고, 우리 DB에 남긴다.

**왜 둘인가.** 화면(작업 로그의 대시보드·평가 탭)이 읽는 것은 우리 DB다. 외부 수집기를
운영하지 않는 환경이라 그것이 유일한 보관처고, 수집기가 없을 때 조용히 아무것도 안 남는
구조는 "관측을 넣었다"는 착각만 만든다. OpenTelemetry는 **계측 규약**으로 쓴다 — 속성
이름을 gen_ai.* 의미 규약으로 맞춰 두면, 나중에 PAAS_OTEL_ENDPOINT를 채워 수집기
(Prometheus·VictoriaMetrics 등 Apache-2.0 계열)로 같은 스팬을 흘릴 수 있다. opentelemetry
패키지가 없으면 그 쪽만 꺼지고 DB 기록은 그대로 남는다 — 필수 의존성으로 두면 설치 한 번
실패에 관측이 통째로 사라진다(requirements.txt의 pypdf 사례).

**본문은 절대 싣지 않는다.** 프롬프트·응답 텍스트는 개인 업무 맥락이 섞인다. 모델 이름,
걸린 시간, 토큰 수, 실패 사유만 남긴다 — 관측은 그것으로 성립한다.
"""
import contextlib
import contextvars
import json
import time
import uuid
from typing import Iterator

from ..config import get_settings
from ..db import SessionLocal
from ..models import LlmCallDetail, LlmCallLog, LlmTurn

# 지금 재고 있는 호출. 컨텍스트로 두는 이유: 호출부(chat_completion·_responses_call)마다
# 인자를 더하면 _post_chat을 monkeypatch하는 테스트 열 곳이 함께 깨진다.
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("paas_llm_call", default=None)
# 어느 화면에서 온 요청인지(미들웨어가 넣는다). 없으면 빈 문자열 — 스케줄러·CLI 경로다.
_route: contextvars.ContextVar[str] = contextvars.ContextVar("paas_route", default="")
# 지금의 턴 — 바깥 호출이 열고 도구 왕복이 같은 값을 쓴다(turn).
_turn: contextvars.ContextVar[str] = contextvars.ContextVar("paas_llm_turn", default="")
# 호출부가 아는 입력의 이름(hints) — 지시문의 구역, 대화 이력의 구역, 도구 결과의 출처.
# 메시지만 보고는 알 수 없다(어디까지가 에이전트 목록인지, 어느 글이 첨부인지).
_hints: contextvars.ContextVar[dict | None] = contextvars.ContextVar("paas_llm_hints", default=None)

# 토큰 추정의 무게. 영문·JSON(도구 정의)은 약 4자에 1토큰, 한글은 1~2자에 1토큰이라 글자 수를
# 그대로 비율로 쓰면 한글이 많은 구간(지시문·문서)이 작게 잡힌다. 이미지는 글자가 없어 장당
# 어림값을 쓴다(Claude 기준 1000x1000 한 장이 약 1,300토큰).
_ASCII_PER_TOKEN = 4.0
_OTHER_PER_TOKEN = 1.5
_IMAGE_TOKENS = 1_300

_UNSET = object()
_tracer_cache: object = _UNSET


def set_route(route: str) -> None:
    _route.set(route)


def _tracer():
    """opentelemetry가 있으면 트레이서, 없으면 None — 한 번만 확인한다."""
    global _tracer_cache
    if _tracer_cache is _UNSET:
        try:
            from opentelemetry import trace  # noqa: PLC0415

            _tracer_cache = trace.get_tracer("paas.llm")
        except Exception:  # noqa: BLE001 — 관측이 호출을 막으면 본말전도다
            _tracer_cache = None
    return _tracer_cache


def note(**fields) -> None:
    """지금 재고 있는 호출에 사실을 덧붙인다(토큰 수·실제로 쓴 경로 등)."""
    row = _current.get()
    if row is not None:
        row.update(fields)


@contextlib.contextmanager
def turn() -> Iterator[str]:
    """한 턴(도구 왕복 여러 번)을 묶는다. 이미 열린 턴 안이면 그것을 쓴다."""
    if _turn.get():
        yield _turn.get()
        return
    token = _turn.set(uuid.uuid4().hex)
    try:
        yield _turn.get()
    finally:
        _turn.reset(token)


@contextlib.contextmanager
def hints(*, system: list[tuple[str, str]] | None = None, history: list[tuple[str, str]] | None = None,
          base_len: int = 0, source_of=None) -> Iterator[None]:
    """입력의 이름을 알려 준다 — 그 안의 모든 호출(도구 왕복 포함)이 쓴다.

    system·history는 (이름, 글) 목록이고 messages의 첫 base_len개(지시문 + 대화 이력)를 대신한다.
    source_of(도구 이름, 인자) → 도구 결과의 출처 이름(없으면 도구 이름).
    """
    token = _hints.set({"system": system or [], "history": history or [], "base_len": base_len,
                        "source_of": source_of})
    try:
        yield
    finally:
        _hints.reset(token)


def _weight(text: str) -> float:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars / _ASCII_PER_TOKEN + (len(text) - ascii_chars) / _OTHER_PER_TOKEN


def _texts(content) -> tuple[str, int]:
    """메시지 content → (글, 이미지 수). 이미지가 붙은 사용자 메시지는 목록 모양이다."""
    if isinstance(content, list):
        texts = [str(c.get("text") or "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        images = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "image_url")
        return "\n".join(texts), images
    return str(content or ""), 0


def composition(messages: list[dict], tools: list[dict] | None) -> list[dict]:
    """입력을 구간으로 나눠 크기를 잰다 — **본문은 싣지 않는다**(도구 이름과 크기뿐).

    도구 결과는 그 결과를 낸 도구 이름으로 묶는다(앞의 assistant 메시지의 tool_calls id로 찾는다)
    — 어느 도구가 입력을 불리는지가 줄일 곳을 고르는 근거다. weight는 _save가 실제 입력 토큰을
    나눌 때 쓰는 비율이다.
    """
    cells: dict[tuple[str, str], dict] = {}

    def add(part: str, name: str, text: str = "", images: int = 0) -> None:
        cell = cells.setdefault((part, name), {"part": part, "name": name, "chars": 0, "weight": 0.0})
        cell["chars"] += len(text)
        cell["weight"] += _weight(text) + images * _IMAGE_TOKENS

    hint = _hints.get() or {}
    base_len = hint.get("base_len") or 0 if (hint.get("system") or hint.get("history")) else 0
    source_of = hint.get("source_of")
    for label, text in hint.get("system") or [] if base_len else []:
        add("system", label, text)
    for label, text in hint.get("history") or [] if base_len else []:
        add("history", label, text)

    calls: dict[str, tuple[str, dict]] = {}
    for i, m in enumerate(messages):
        role = m.get("role")
        text, images = _texts(m.get("content"))
        if images:
            add("image", "", "", images)
        if i < base_len:
            continue  # 이름을 아는 구역으로 이미 셌다
        if role == "system":
            add("system", "", text)
        elif role == "tool":
            name, args = calls.get(str(m.get("tool_call_id") or ""), ("", {}))
            add("tool_result", (source_of(name, args) if source_of else name) or name, text)
        else:
            add("history", "" if not base_len else "이번 턴의 중간 답", text)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                calls[str(tc.get("id") or "")] = (str(fn.get("name") or ""),
                                                  args if isinstance(args, dict) else {})
                add("tool_call", "", str(fn.get("arguments") or ""))
    if tools:
        add("tools", "", json.dumps(tools, ensure_ascii=False))
    return [c for c in cells.values() if c["chars"] or c["weight"]]


def _apportion(parts: list[dict], prompt_tokens: int) -> list[dict]:
    """실제 입력 토큰을 구간의 무게 비율로 나눈다. 프로바이더가 0을 주면 무게 자체가 추정치다."""
    total = sum(p["weight"] for p in parts)
    out = []
    for p in parts:
        tokens = (prompt_tokens * p["weight"] / total) if prompt_tokens and total else p["weight"]
        out.append({"part": p["part"], "name": p["name"], "chars": p["chars"], "tokens": round(tokens)})
    return out


def result_fields(data: dict) -> dict:
    """OpenAI 모양 응답에서 **셀 수 있는 것만** 뽑는다 — 본문은 건드리지 않는다.

    usage는 프로바이더마다 비어 오기도 한다(Bedrock은 inputTokens로 주고, 응답 API는
    input_tokens로 준다 — 각 경로에서 이 이름으로 맞춰 놓는다). 없으면 0으로 남긴다.
    """
    usage = data.get("usage") or {}
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    details = usage.get("prompt_tokens_details") or {}
    choice = (data.get("choices") or [{}])[0] or {}
    # 출력 구성 — 글 답과 도구 인자(도구 이름별). 크기만 잰다.
    output: dict[tuple[str, str], dict] = {}
    pieces = [("text", "", str(message.get("content") or ""))] + [
        ("tool_args", str((tc.get("function") or {}).get("name") or ""),
         str((tc.get("function") or {}).get("arguments") or ""))
        for tc in message.get("tool_calls") or []]
    for part, name, text in pieces:
        if text:
            cell = output.setdefault((part, name), {"part": part, "name": name, "chars": 0, "weight": 0.0})
            cell["chars"] += len(text)
            cell["weight"] += _weight(text)
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or usage.get("cached_tokens") or 0),
        "tool_calls": len(message.get("tool_calls") or []),
        "output_parts": list(output.values()),
        "finish_reason": str(choice.get("finish_reason") or "")[:32],
    }


@contextlib.contextmanager
def llm_call(provider: str, model: str, path: str, tool_round: int = 0) -> Iterator[dict]:
    """모델 호출 한 번을 잰다. **실패도 남긴다** — 실패율이 대시보드의 첫 숫자다."""
    row: dict = {
        "provider": provider[:64], "model": model[:128], "path": path,
        "route": _route.get()[:128], "tool_round": tool_round, "turn_id": _turn.get(),
    }
    token = _current.set(row)
    started = time.monotonic()
    tracer = _tracer()
    span_cm = tracer.start_as_current_span("chat") if tracer else contextlib.nullcontext()
    try:
        with span_cm as span:
            try:
                yield row
                row["ok"] = True
            except BaseException as e:  # noqa: BLE001 — 기록만 하고 그대로 올린다
                row["ok"] = False
                # 예외 종류를 앞에 둔다 — 같은 문구의 400이라도 어디서 난 것인지 구분된다.
                row["error"] = f"{type(e).__name__}: {e}"[:500]
                raise
            finally:
                row["ms"] = int((time.monotonic() - started) * 1000)
                _annotate(span, row)
                _save(row)
    finally:
        _current.reset(token)


def _annotate(span, row: dict) -> None:
    """스팬에 gen_ai.* 속성을 싣고, 트레이스 ID를 DB 행에도 적어 둔다."""
    if span is None:
        return
    try:
        span.set_attribute("gen_ai.system", row["provider"])
        span.set_attribute("gen_ai.request.model", row["model"])
        span.set_attribute("gen_ai.operation.name", row["path"])
        span.set_attribute("gen_ai.usage.input_tokens", row.get("prompt_tokens") or 0)
        span.set_attribute("gen_ai.usage.output_tokens", row.get("completion_tokens") or 0)
        span.set_attribute("paas.route", row["route"])
        hex_id = f"{span.get_span_context().trace_id:032x}"
        # SDK 없이 API만 있으면 no-op 스팬이라 trace_id가 0이다 — 0을 남기면 없는 트레이스를
        # 가리키게 된다.
        if hex_id.strip("0"):
            row["trace_id"] = hex_id
    except Exception as e:  # noqa: BLE001
        print(f"[paas] 관측 스팬 기록 실패(무시): {e}")


def _save(row: dict) -> None:
    """자기 세션으로 쓴다 — 호출부의 트랜잭션에 끼어들면 미완성 작업을 함께 커밋한다."""
    row = dict(row)
    parts = row.pop("parts", None) or []
    output = row.pop("output_parts", None) or []
    finish = row.pop("finish_reason", "") or ""
    turn_id = row.pop("turn_id", "") or ""
    cached = int(row.pop("cached_tokens", 0) or 0)
    try:
        with SessionLocal() as db:
            call = LlmCallLog(**row)
            db.add(call)
            db.flush()
            db.add(LlmCallDetail(call_id=call.id, turn_id=turn_id, cached_tokens=cached,
                                 parts=_apportion(parts, call.prompt_tokens or 0),
                                 output_parts=_apportion(output, call.completion_tokens or 0),
                                 finish_reason=finish))
            db.commit()
    except Exception as e:  # noqa: BLE001 — 관측 실패가 LLM 호출을 깨뜨리면 안 된다
        print(f"[paas] 관측 기록 실패(무시): {e}")


def record_turn(turn_id: str, outcome: str, reads: list[dict]) -> None:
    """턴의 결과를 남긴다(models.LlmTurn) — 실패해도 대화는 그대로다."""
    if not turn_id:
        return
    try:
        with SessionLocal() as db:
            db.add(LlmTurn(turn_id=turn_id, outcome=outcome[:32], reads=reads))
            db.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[paas] 턴 기록 실패(무시): {e}")


def normalize_route(path: str) -> str:
    """집계되는 이름으로 줄인다 — 원본 경로로는 묶이지 않는다.

    /paas/api/v1/smartwork/sessions/12/messages 와 .../13/messages 는 같은 기능이다.
    공통 prefix를 떼고 숫자 조각을 :id로 바꿔야 "어느 기능이 토큰을 태우나"에 답할 수 있다.
    """
    for prefix in ("/paas/api/v1", "/paas"):
        if path.startswith(prefix):
            path = path[len(prefix):] or "/"
            break
    parts = [":id" if p.isdigit() else p for p in path.split("/")]
    return "/".join(parts)[:128]


class RouteTagMiddleware:
    """요청 경로를 관측 컨텍스트에 넣는다 — 요청마다 DB에 쓰지는 않는다.

    순수 ASGI로 쓴다(BaseHTTPMiddleware 아님): 응답 본문을 거쳐 가지 않아 스트리밍·
    웹소켓에 영향이 없고, 이 플랫폼의 유일한 미들웨어가 그 때문에 사고를 낼 이유가 없다.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            set_route(normalize_route(scope.get("path") or ""))
        await self.app(scope, receive, send)


def setup_tracing() -> None:
    """OTLP로도 내보낼 때만 SDK를 켠다 — 주소가 없으면 아무것도 하지 않는다.

    주소가 없는데 켜면 수집기가 없는 환경에서 스팬마다 재시도가 돌고(배치 익스포터가
    조용히 반복한다) 로그만 더러워진다. DB 기록은 이 함수와 무관하게 늘 남으므로, 끄는
    쪽이 안전한 기본값이다. 패키지가 없으면 그 사실만 한 줄 남기고 넘어간다 — 관측
    의존성 때문에 플랫폼이 안 뜨는 일은 없어야 한다.
    """
    endpoint = get_settings().otel_endpoint.strip()
    if not endpoint:
        return
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        print(f"[paas] PAAS_OTEL_ENDPOINT가 설정됐지만 opentelemetry 패키지가 없습니다 — "
              f"내보내기만 꺼집니다(기록은 DB에 그대로): {e}")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "paas"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    global _tracer_cache
    _tracer_cache = _UNSET  # 다음 호출에서 새 provider의 트레이서를 받는다
    print(f"[paas] OpenTelemetry 내보내기 활성화 — {endpoint}")
