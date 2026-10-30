"""AWS SSO 세션 유지 — 여러 사람이 같은 Bedrock 프로바이더를 쓰므로 **서버가 스스로** 토큰을 살려 둔다.

예전에는 누군가의 요청이 만료된 토큰에 부딪힌 뒤에야 알았다(api/llm.provider_error가 그 자리에서 로그인을
띄운다). 사람마다 그 실패를 한 번씩 겪고, 대화 여러 개가 동시에 깨진다.

**AWS가 허락하는 것.** sso_session 프로필의 액세스 토큰은 1시간짜리이고, botocore는 **만료 15분 안에서만**
갱신 토큰으로 새 토큰을 받는다(SSOTokenProvider._REFRESH_WINDOW). 그 밖에서 부르면 캐시를 그대로 준다 —
세션이 살아 있는지는 그 창 안에서 갱신을 시도해야만 알 수 있다. 갱신이 실패해도 토큰이 아직 살아 있으면
botocore는 조용히 넘어가므로, 갱신 여부는 캐시의 expires_at이 앞으로 갔는지로 판정한다.

**재로그인 시각은 어디에도 없다.** 갱신이 거절되는 때는 IAM Identity Center의 세션 시간(관리자 설정)이
끝날 때이고, 그 값은 캐시에 없다. 그래서 **배운다**: 로그인에서 마지막으로 갱신에 성공한 때까지가 하한,
갱신이 거절된 때까지가 상한이다. 갱신은 로그인에서 늘 같은 간격(약 45~57분)으로 일어나 그대로 두면 구간이
좁혀지지 않는다 — 그래서 창 안의 어느 때에 갱신할지를 고른다(간격을 늘이고 줄인다). 다음 시도가 지금
추정 구간의 가운데에 떨어지게 몰아 가면, 세션마다 구간이 절반씩 줄어든다.

상태는 스케줄러 표의 그 job 행(last_detail)에 남는다 — 재시작해도 배운 것을 잃지 않는다.
"""
from datetime import datetime, timedelta, timezone

from . import bedrock

# botocore가 갱신을 시도하는 창(만료 전 15분). 창이 열리자마자부터, 늦어도 재시도할 틈을 남기고.
EARLY = timedelta(minutes=14)
LATE = timedelta(minutes=3)
# 창 안에서 갱신이 안 됐을 때 다시 보는 간격 — 일시적인 네트워크 실패와 세션 종료를 가른다.
RETRY = timedelta(minutes=2)
# 세션이 끝난 뒤 — 누군가 다시 로그인하면 빨리 알아채야 로그인 시각(배울 출발점)이 정확하다.
ENDED_POLL = timedelta(minutes=5)
MIN_INTERVAL = timedelta(minutes=1)
MAX_INTERVAL = timedelta(hours=1)
# 액세스 토큰 수명 — 캐시에서 갱신 간격을 재지 못할 때의 값.
TOKEN_LIFETIME = timedelta(hours=1)
# 추정 구간이 이보다 좁으면 더 몰아 가지 않는다(창 안에서 맞출 수 있는 정밀도).
GOOD_ENOUGH = timedelta(minutes=5)
KEEP_SESSIONS = 10


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def estimate(sessions: list[dict]) -> tuple[float, float] | None:
    """지난 세션들에서 배운 세션 시간(초)의 (하한, 상한). 서로 맞지 않으면(관리자가 세션 시간을 바꿨다)
    가장 최근 세션만 믿는다."""
    if not sessions:
        return None
    low = max(s["lower"] for s in sessions)
    high = min(s["upper"] for s in sessions)
    if low > high:
        last = sessions[-1]
        return last["lower"], last["upper"]
    return low, high


def next_check(now: datetime, expires: datetime, login_at: datetime | None,
               est: tuple[float, float] | None, lifetime: timedelta = TOKEN_LIFETIME) -> tuple[datetime, str]:
    """다음에 볼 때와 그 이유. 갱신은 [만료-EARLY, 만료-LATE] 안에서만 된다 — 그 안의 어느 때를 고를지가 전부다.

    배울 것이 있으면(로그인 시각과 추정 구간) 시도가 구간 가운데에 떨어지게 몬다. 이번에 r에 갱신하면 다음 창은
    r+[수명-EARLY, 수명-LATE]이므로, n번 뒤에 닿을 수 있는 때는 [창 시작 + n·(수명-EARLY), 창 끝 + n·(수명-LATE)]이다.
    그 안에서 가운데에 가장 가까운 점을 고르고, 거기 닿도록 이번 갱신 시각을 정한다(매번 다시 계산한다). 로그인
    직후 몇 시간은 닿을 수 없는 틈이 있어(갱신 한 번에 46~57분씩만 나아간다) 짧은 세션은 그만큼만 좁혀진다.
    """
    open_at, close_at = expires - EARLY, expires - LATE
    if login_at is None or est is None or est[1] - est[0] <= GOOD_ENOUGH.total_seconds():
        return max(open_at, now), "refresh"
    low, high = login_at + timedelta(seconds=est[0]), login_at + timedelta(seconds=est[1])
    middle = low + (high - low) / 2
    step_lo, step_hi = lifetime - EARLY, lifetime - LATE
    best: tuple[datetime, int] | None = None
    for n in range(500):
        a, b = open_at + n * step_lo, close_at + n * step_hi
        if a > high:
            break
        lo, hi = max(a, low), min(b, high)
        if lo <= hi:
            candidate = min(max(middle, lo), hi)
            if best is None or abs(candidate - middle) < abs(best[0] - middle):
                best = (candidate, n)
    if best is None:
        return max(open_at, now), "refresh"
    target, n = best
    if n == 0:
        return max(target, now), "probe"
    # 이번 갱신 r은 target - r이 n번의 걸음 [n·step_lo, n·step_hi] 안에 들게 — 그 범위의 가운데(뒤에 고칠 여유)
    r_lo, r_hi = max(open_at, target - n * step_hi), min(close_at, target - n * step_lo)
    return max(r_lo + (r_hi - r_lo) / 2, now), "steer"


def keep_alive(profile: str, prev: dict | None, now: datetime | None = None) -> tuple[str, dict, int]:
    """한 번 돌기 — (상태, 남길 detail, 다음까지 초). 상태는 scheduler의 JobStatus 값(ok·failed·skipped)."""
    now = now or datetime.now(timezone.utc)
    state = dict(prev or {})
    for key in ("error", "reason", "plan"):   # 지난 실행의 설명은 이번 것이 아니다
        state.pop(key, None)
    sessions = list(state.get("sessions") or [])

    def done(status: str, interval: timedelta, **extra) -> tuple[str, dict, int]:
        seconds = int(min(MAX_INTERVAL, max(MIN_INTERVAL, interval)).total_seconds())
        if len(sessions) > KEEP_SESSIONS:
            # 오래된 세션은 버리지 않고 하나로 접는다 — 버리면 좁혀 둔 구간이 다시 넓어진다(좁혀진 뒤에는 몰아 가지
            # 않아 새 세션들은 넓은 구간만 남긴다).
            old = sessions[:-(KEEP_SESSIONS - 1)]
            low, high = estimate(old)
            sessions[:] = [{"login_at": None, "lower": low, "upper": high}] + sessions[-(KEEP_SESSIONS - 1):]
        est = estimate(sessions)
        login_at = _ts(state.get("login_at"))
        detail = {
            **state, **extra, "profile": profile, "sessions": sessions,
            "checked_at": _iso(now), "next_in_seconds": seconds,
            # 화면이 읽는 값 — 배운 세션 시간과 그로 본 이번 세션의 끝(이르면·늦으면)
            "session_seconds": list(est) if est else None,
            "session_ends": ([_iso(login_at + timedelta(seconds=est[0])), _iso(login_at + timedelta(seconds=est[1]))]
                             if est and login_at and not state.get("ended_at") else None),
        }
        return status, detail, seconds

    if not bedrock.botocore_available():
        return done("skipped", MAX_INTERVAL, reason="botocore가 없어 토큰을 갱신할 수 없습니다 (pip install botocore)")

    before = bedrock.sso_token_state(profile)
    try:
        if before.get("refreshable"):
            bedrock.refresh_sso_token(profile)
        bedrock._frozen(profile)   # 역할 자격증명(STS)까지 — 호출이 실제로 되는지
        alive = True
    except bedrock.SsoExpired:
        alive = False
    except bedrock.BedrockError as e:
        # 끊긴 것인지 모른다(네트워크·설정) — 배운 것은 그대로 두고 곧 다시 본다.
        return done("failed", RETRY, error=str(e)[:300])
    after = bedrock.sso_token_state(profile)

    if not after:
        # SSO가 아니다(정적 키·AssumeRole) — 갱신할 토큰이 없다. 자격증명이 되는지만 본다.
        return done("ok" if alive else "failed", MAX_INTERVAL, sso=False)
    expires = _ts(after.get("expires_at"))
    refreshable = bool(after.get("refreshable"))
    state.update(sso=True, refreshable=refreshable)

    if not alive:
        if not state.get("ended_at"):
            # 세션이 끝났다 — 이번 세션에서 배운 것을 적는다. 상한은 처음 갱신이 안 된 때다.
            login_at = _ts(state.get("login_at"))
            if login_at:
                ended = _ts(state.get("first_fail_at")) or now
                last_ok = _ts(state.get("last_ok_at")) or login_at
                if not refreshable and expires:
                    # 갱신 토큰이 없으면 토큰 만료가 곧 세션의 끝이다
                    last_ok = ended = expires
                sessions.append({"login_at": _iso(login_at),
                                 "lower": max(0.0, (last_ok - login_at).total_seconds()),
                                 "upper": max(0.0, (ended - login_at).total_seconds())})
            state.update(ended_at=_iso(now), first_fail_at=None)
        return done("failed", ENDED_POLL, reason="SSO 세션이 끝났습니다 — 서버에서 다시 로그인해야 합니다")

    if state.get("ended_at") or "login_at" not in state:
        # 끝난 뒤 다시 살아났다 = 누군가 로그인했다. 그때 캐시가 쓰였다. 처음 보는 프로필은 언제 로그인했는지
        # 모른다(이번 세션은 배우지 않는다 — 틀린 출발점으로 배우면 구간이 엉뚱하게 좁아진다).
        state["login_at"] = after.get("written_at") if state.get("ended_at") else None
        state.update(ended_at=None, first_fail_at=None, last_ok_at=state.get("login_at"))

    if not refreshable or expires is None:
        # 구형 프로필 — 갱신이 없다. 만료가 곧 끝이니 그 직후에 본다.
        return done("ok", (expires - now + MIN_INTERVAL) if expires else MAX_INTERVAL)

    old = _ts(before.get("expires_at"))
    seen = _ts((prev or {}).get("expires_at"))
    in_window = old is not None and old - now <= EARLY + MIN_INTERVAL
    if old is not None and expires > old:
        state.update(last_ok_at=_iso(now), first_fail_at=None)
    elif seen is not None and expires > seen:
        # 지난번 본 뒤에 다른 요청(사람의 Bedrock 호출)이 갱신했다 — 그것도 세션이 살아 있었다는 증거다.
        state.update(last_ok_at=after.get("written_at"), first_fail_at=None)
    elif in_window:
        # 창 안인데 갱신이 안 됐다 — 세션이 끝나 가는 중일 수 있다. 토큰이 살아 있는 동안 몇 번 더 본다.
        state["first_fail_at"] = state.get("first_fail_at") or _iso(now)
        return done("failed", min(RETRY, max(MIN_INTERVAL, expires - now - MIN_INTERVAL)),
                    reason="액세스 토큰을 갱신하지 못했습니다 — 세션이 끝나 가는 중일 수 있습니다")

    state["expires_at"] = after.get("expires_at")
    login_at = _ts(state.get("login_at"))
    last_ok = _ts(state.get("last_ok_at"))
    lifetime = (expires - last_ok) if last_ok and expires - last_ok > timedelta(minutes=30) else TOKEN_LIFETIME
    when, why = next_check(now, expires, login_at, estimate(sessions), lifetime)
    return done("ok", when - now, plan=why)
