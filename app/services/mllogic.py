"""ML 로직 — 표 데이터에 예측(회귀·시계열)·분류·클러스터링·최적화를 돌린다(MCP /mcp/ml, 「paas-ml」).

에이전트(스마트워크·외부 개발 도구)가 데이터를 보고 "다음 분기 발주량은?", "이 협력사는 위험군인가?", "비슷한
협력사끼리 묶으면?", "예산 안에서 어떻게 나누면 가장 싼가?"를 물을 때, LLM이 숫자를 어림하지 않고 **계산한다.**

  - 데이터는 행 목록이다(열 이름 → 값). 에이전트가 직접 넘기거나(스킬·DB 조회 결과), 문서 저장소의 CSV·엑셀 파일을
    가리킨다(MCP 층이 읽는다 — 권한은 그 저장소의 것).
  - **학습과 예측을 한 번에** 한다(모델을 서버에 남기지 않는다) — 같은 데이터면 같은 결과다(random_state 고정).
    무엇을 근거로 했는지(교차 검증 점수·중요한 열·뒤로 시험한 오차)를 함께 준다: 점수 없이 예측만 주면 믿을지 말지를
    정할 수 없다.
  - 열은 스스로 가른다(숫자·범주·날짜·식별자·긴 글). 식별자(행마다 다른 값)와 긴 글은 특징에서 뺀다 — 넣으면 외워서
    교차 검증 점수만 좋아진다.
  - 크기에 상한이 있다(행·특징·범주·예측 행). 넘으면 앞부분만 쓰고 그렇게 알린다.

라이브러리는 scikit-learn·SciPy·NumPy(BSD)다. 없으면 이 도구들만 꺼진다(MlError로 이유를 말한다).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, timedelta

MAX_ROWS = 20_000
MAX_FEATURES = 50
MAX_CATEGORIES = 30       # 범주 열 하나에서 원-핫으로 펼칠 값 수(나머지는 「기타」)
MAX_PREDICT = 500
MAX_ASSIGN = 500          # 클러스터 배정을 돌려줄 행 수
MAX_CLASSES = 50
MIN_ROWS = 10
SEED = 0


class MlError(ValueError):
    """사람이 읽을 사유 — 도구 오류로 그대로 나간다."""


def available() -> tuple[bool, str]:
    try:
        import numpy  # noqa: F401, PLC0415
        import scipy  # noqa: F401, PLC0415
        import sklearn  # noqa: F401, PLC0415
    except ImportError as e:
        return False, f"{e.name}이 설치돼 있지 않습니다 — SW 업데이트(pip install -r requirements.txt)를 하세요"
    return True, ""


def _need() -> None:
    ok, why = available()
    if not ok:
        raise MlError(why)


# ---------------------------------------------------------------- 표와 열

_NUM = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$|^[+-]?\.\d+$")
_DATE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})")


def number(v) -> float | None:
    """1,200 · 12.5% · -3 → 수. 수가 아니면 None."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v or "").strip().replace(" ", "")
    pct = s.endswith("%")
    s = s.rstrip("%")
    if not s or not _NUM.match(s):
        return None
    x = float(s.replace(",", ""))
    return x / 100 if pct else x


def day(v) -> date | None:
    m = _DATE.match(str(v or "").strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def table(rows, cap: int = MAX_ROWS) -> tuple[list[str], list[dict], list[str]]:
    """행 목록을 받는다 — (열, 행, 메모). 열은 처음 나온 순서."""
    if not isinstance(rows, list) or not rows:
        raise MlError("데이터가 없습니다 — rows(열 이름 → 값의 목록)나 저장소의 파일을 주세요.")
    notes = []
    if len(rows) > cap:
        notes.append(f"행이 {len(rows):,}개라 앞의 {cap:,}개만 썼습니다.")
        rows = rows[:cap]
    clean, columns = [], []
    for r in rows:
        if not isinstance(r, dict):
            raise MlError("rows의 항목은 객체(열 이름 → 값)여야 합니다.")
        for k in r:
            if k not in columns:
                columns.append(str(k))
        clean.append({str(k): v for k, v in r.items()})
    return columns, clean, notes


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


def profile(columns: list[str], rows: list[dict]) -> dict[str, dict]:
    """열마다 종류 — numeric(수)·date(날짜)·category(범주)·id(행마다 다른 값)·text(긴 글)·empty."""
    out = {}
    n = len(rows)
    for c in columns:
        vals = [r.get(c) for r in rows if _present(r.get(c))]
        info: dict = {"missing": n - len(vals), "unique": len({str(v) for v in vals})}
        if not vals:
            out[c] = {**info, "kind": "empty"}
            continue
        nums = [x for x in (number(v) for v in vals) if x is not None]
        if len(nums) >= 0.9 * len(vals):
            arr = sorted(nums)
            out[c] = {**info, "kind": "numeric", "min": arr[0], "max": arr[-1],
                      "mean": sum(arr) / len(arr), "median": arr[len(arr) // 2]}
            continue
        days = [d for d in (day(v) for v in vals) if d]
        if len(days) >= 0.9 * len(vals):
            out[c] = {**info, "kind": "date", "min": min(days).isoformat(), "max": max(days).isoformat()}
            continue
        avg = sum(len(str(v)) for v in vals) / len(vals)
        kind = ("text" if avg > 40 else
                "id" if info["unique"] > 0.9 * len(vals) and len(vals) > 20 else "category")
        top = Counter(str(v) for v in vals).most_common(5)
        out[c] = {**info, "kind": kind, "top": [{"value": v, "count": k} for v, k in top]}
    return out


def describe(rows) -> dict:
    columns, rows, notes = table(rows)
    return {"rows": len(rows), "columns": profile(columns, rows), "notes": notes}


def _pick(columns: list[str], prof: dict, exclude: set[str], features) -> tuple[list[str], list[str]]:
    """쓸 특징 — 주면 그것(없는 열은 오류), 아니면 수·범주·날짜 열(식별자·긴 글·빈 열은 뺀다)."""
    if features:
        missing = [f for f in features if f not in columns]
        if missing:
            raise MlError(f"데이터에 없는 열입니다: {', '.join(missing)}")
        chosen = [f for f in features if f not in exclude]
    else:
        chosen = [c for c in columns if c not in exclude and prof[c]["kind"] in ("numeric", "category", "date")]
    notes = []
    if len(chosen) > MAX_FEATURES:
        notes.append(f"특징이 {len(chosen)}개라 앞의 {MAX_FEATURES}개만 썼습니다.")
        chosen = chosen[:MAX_FEATURES]
    if not chosen:
        raise MlError("쓸 특징 열이 없습니다 — features로 열을 고르세요(식별자·긴 글은 자동으로 빼고 있습니다).")
    return chosen, notes


def _matrix(rows: list[dict], feats: list[str], prof: dict):
    """특징 행렬(object) — 수와 날짜(일 수)는 float(빈 값은 nan), 범주는 글자(빈 값은 「(없음)」)."""
    import numpy as np  # noqa: PLC0415

    X = np.empty((len(rows), len(feats)), dtype=object)
    for j, f in enumerate(feats):
        kind = prof[f]["kind"]
        for i, r in enumerate(rows):
            v = r.get(f)
            if kind == "numeric":
                x = number(v)
                X[i, j] = np.nan if x is None else x
            elif kind == "date":
                d = day(v)
                X[i, j] = np.nan if d is None else float(d.toordinal())
            else:
                X[i, j] = str(v).strip() if _present(v) else "(없음)"
    return X


def _prep(feats: list[str], prof: dict, scale: bool = True):
    from sklearn.compose import ColumnTransformer  # noqa: PLC0415
    from sklearn.impute import SimpleImputer  # noqa: PLC0415
    from sklearn.pipeline import make_pipeline  # noqa: PLC0415
    from sklearn.preprocessing import OneHotEncoder, StandardScaler  # noqa: PLC0415

    num = [j for j, f in enumerate(feats) if prof[f]["kind"] in ("numeric", "date")]
    cat = [j for j, f in enumerate(feats) if j not in num]
    parts = []
    if num:
        steps = [SimpleImputer(strategy="median")] + ([StandardScaler()] if scale else [])
        parts.append(("num", make_pipeline(*steps), num))
    if cat:
        parts.append(("cat", OneHotEncoder(handle_unknown="infrequent_if_exist", max_categories=MAX_CATEGORIES,
                                           sparse_output=False), cat))
    return ColumnTransformer(parts)


def _importance(model, X, y, feats: list[str], scoring: str) -> list[dict]:
    """열마다 중요도 — 그 열을 섞으면 점수가 얼마나 떨어지나(permutation). 원래 열 단위라 원-핫으로 펼친 것도 하나로."""
    from sklearn.inspection import permutation_importance  # noqa: PLC0415

    n = min(len(y), 2000)
    res = permutation_importance(model, X[:n], y[:n], scoring=scoring, n_repeats=5, random_state=SEED)
    order = sorted(range(len(feats)), key=lambda j: -res.importances_mean[j])
    return [{"feature": feats[j], "importance": round(float(res.importances_mean[j]), 4)} for j in order][:15]


def _cv(n: int, classes: Counter | None = None) -> int:
    k = min(5, n // 4)
    if classes:
        k = min(k, min(classes.values()))
    return max(2, k)


# ---------------------------------------------------------------- 예측(회귀)

REGRESSORS = ("linear", "forest", "boosting")


def _regressor(name: str):
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor  # noqa: PLC0415
    from sklearn.linear_model import Ridge  # noqa: PLC0415

    return {"linear": lambda: Ridge(alpha=1.0),
            "forest": lambda: RandomForestRegressor(n_estimators=200, random_state=SEED, n_jobs=1),
            "boosting": lambda: HistGradientBoostingRegressor(random_state=SEED)}[name]()


def regress(rows, target: str, features=None, model: str = "auto", predict_rows=None) -> dict:
    """수 값(target)을 다른 열로 예측한다. auto면 세 모델을 교차 검증(MAE)으로 견줘 가장 나은 것."""
    _need()
    import numpy as np  # noqa: PLC0415
    from sklearn.model_selection import KFold, cross_validate  # noqa: PLC0415
    from sklearn.pipeline import make_pipeline  # noqa: PLC0415

    columns, rows, notes = table(rows)
    if target not in columns:
        raise MlError(f"target 열이 없습니다: {target}")
    prof = profile(columns, rows)
    if prof[target]["kind"] != "numeric":
        raise MlError(f"{target}은 수 열이 아닙니다({prof[target]['kind']}) — 범주를 맞히려면 classify를 쓰세요.")
    rows = [r for r in rows if number(r.get(target)) is not None]
    if len(rows) < MIN_ROWS:
        raise MlError(f"target 값이 있는 행이 {len(rows)}개뿐입니다(최소 {MIN_ROWS}).")
    feats, more = _pick(columns, prof, {target}, features)
    X, y = _matrix(rows, feats, prof), np.array([number(r[target]) for r in rows])
    names = REGRESSORS if model == "auto" else (model,)
    if any(m not in REGRESSORS for m in names):
        raise MlError(f"model은 auto·{'·'.join(REGRESSORS)} 중 하나입니다.")
    cv = KFold(_cv(len(y)), shuffle=True, random_state=SEED)
    scores = {}
    for m in names:
        r = cross_validate(make_pipeline(_prep(feats, prof), _regressor(m)), X, y, cv=cv,
                           scoring=("r2", "neg_mean_absolute_error"))
        scores[m] = {"r2": round(float(r["test_r2"].mean()), 4),
                     "mae": round(float(-r["test_neg_mean_absolute_error"].mean()), 4)}
    best = min(scores, key=lambda m: scores[m]["mae"])
    fitted = make_pipeline(_prep(feats, prof), _regressor(best)).fit(X, y)
    out = {"task": "regression", "target": target, "rows": len(y), "features": feats, "model": best,
           "cv_folds": cv.n_splits, "scores": scores,
           "importance": _importance(fitted, X, y, feats, "neg_mean_absolute_error"),
           "target_summary": {k: prof[target][k] for k in ("min", "max", "mean", "median")},
           "notes": notes + more}
    if predict_rows:
        _, prows, _ = table(predict_rows, MAX_PREDICT)
        pred = fitted.predict(_matrix(prows, feats, prof))
        out["predictions"] = [{"row": i, "prediction": round(float(p), 4)} for i, p in enumerate(pred)]
        out["interval_note"] = f"오차의 크기는 교차 검증 MAE {scores[best]['mae']} 정도로 보세요."
    return out


# ---------------------------------------------------------------- 분류

CLASSIFIERS = ("logistic", "forest", "boosting")


def _classifier(name: str):
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier  # noqa: PLC0415
    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415

    return {"logistic": lambda: LogisticRegression(max_iter=2000),
            "forest": lambda: RandomForestClassifier(n_estimators=200, random_state=SEED, n_jobs=1,
                                                     class_weight="balanced"),
            "boosting": lambda: HistGradientBoostingClassifier(random_state=SEED)}[name]()


def classify(rows, target: str, features=None, model: str = "auto", predict_rows=None) -> dict:
    """범주(target)를 다른 열로 맞힌다. auto면 세 모델을 교차 검증(macro F1)으로 견준다. 혼동 행렬은 교차 검증 예측으로."""
    _need()
    import numpy as np  # noqa: PLC0415
    from sklearn.metrics import confusion_matrix  # noqa: PLC0415
    from sklearn.model_selection import StratifiedKFold, cross_val_predict, cross_validate  # noqa: PLC0415
    from sklearn.pipeline import make_pipeline  # noqa: PLC0415

    columns, rows, notes = table(rows)
    if target not in columns:
        raise MlError(f"target 열이 없습니다: {target}")
    prof = profile(columns, rows)
    rows = [r for r in rows if _present(r.get(target))]
    classes = Counter(str(r[target]).strip() for r in rows)
    if len(classes) < 2:
        raise MlError("맞힐 범주가 둘 이상이어야 합니다.")
    if len(classes) > MAX_CLASSES:
        raise MlError(f"범주가 {len(classes)}개입니다(최대 {MAX_CLASSES}) — 수 값이면 regress를 쓰세요.")
    rare = [c for c, k in classes.items() if k < 2]
    if rare:
        notes.append(f"한 번만 나온 범주는 뺐습니다: {', '.join(rare[:10])}")
        rows = [r for r in rows if str(r[target]).strip() not in rare]
        classes = Counter(str(r[target]).strip() for r in rows)
    if len(rows) < MIN_ROWS:
        raise MlError(f"행이 {len(rows)}개뿐입니다(최소 {MIN_ROWS}).")
    feats, more = _pick(columns, prof, {target}, features)
    X, y = _matrix(rows, feats, prof), np.array([str(r[target]).strip() for r in rows])
    names = CLASSIFIERS if model == "auto" else (model,)
    if any(m not in CLASSIFIERS for m in names):
        raise MlError(f"model은 auto·{'·'.join(CLASSIFIERS)} 중 하나입니다.")
    cv = StratifiedKFold(_cv(len(y), classes), shuffle=True, random_state=SEED)
    scores = {}
    for m in names:
        r = cross_validate(make_pipeline(_prep(feats, prof), _classifier(m)), X, y, cv=cv,
                           scoring=("accuracy", "f1_macro"))
        scores[m] = {"accuracy": round(float(r["test_accuracy"].mean()), 4),
                     "f1_macro": round(float(r["test_f1_macro"].mean()), 4)}
    best = max(scores, key=lambda m: scores[m]["f1_macro"])
    labels = sorted(classes)
    guessed = cross_val_predict(make_pipeline(_prep(feats, prof), _classifier(best)), X, y, cv=cv)
    matrix = confusion_matrix(y, guessed, labels=labels).tolist()
    fitted = make_pipeline(_prep(feats, prof), _classifier(best)).fit(X, y)
    majority = classes.most_common(1)[0]
    out = {"task": "classification", "target": target, "rows": len(y), "features": feats, "model": best,
           "classes": [{"label": c, "count": classes[c]} for c in labels], "cv_folds": cv.n_splits, "scores": scores,
           # 가장 많은 범주만 찍어도 맞히는 비율 — 이보다 낫지 않으면 모델이 배운 것이 없다
           "baseline_accuracy": round(majority[1] / len(y), 4),
           "confusion": {"labels": labels, "matrix": matrix, "note": "행 = 실제, 열 = 예측(교차 검증)"},
           "importance": _importance(fitted, X, y, feats, "f1_macro"), "notes": notes + more}
    if predict_rows:
        _, prows, _ = table(predict_rows, MAX_PREDICT)
        Xp = _matrix(prows, feats, prof)
        proba = fitted.predict_proba(Xp)
        order = list(fitted.classes_)
        out["predictions"] = [
            {"row": i, "prediction": str(order[int(np.argmax(p))]),
             "probabilities": {str(order[k]): round(float(p[k]), 4) for k in np.argsort(-p)[:3]}}
            for i, p in enumerate(proba)]
    return out


# ---------------------------------------------------------------- 클러스터링

CLUSTERERS = ("kmeans", "agglomerative")


def cluster(rows, features=None, k: int | None = None, method: str = "kmeans") -> dict:
    """비슷한 행끼리 묶는다. k를 주지 않으면 2~10에서 실루엣 점수가 가장 높은 수. 묶음마다 수 열의 평균과 흔한 범주."""
    _need()
    import numpy as np  # noqa: PLC0415
    from sklearn.cluster import AgglomerativeClustering, KMeans  # noqa: PLC0415
    from sklearn.metrics import silhouette_score  # noqa: PLC0415

    if method not in CLUSTERERS:
        raise MlError(f"method는 {'·'.join(CLUSTERERS)} 중 하나입니다.")
    columns, rows, notes = table(rows)
    if len(rows) < MIN_ROWS:
        raise MlError(f"행이 {len(rows)}개뿐입니다(최소 {MIN_ROWS}).")
    prof = profile(columns, rows)
    feats, more = _pick(columns, prof, set(), features)
    Z = _prep(feats, prof).fit_transform(_matrix(rows, feats, prof))
    if method == "agglomerative" and len(rows) > 5000:
        raise MlError("agglomerative는 5,000행까지입니다 — kmeans를 쓰세요.")

    def fit(n: int):
        algo = (KMeans(n_clusters=n, n_init=10, random_state=SEED) if method == "kmeans"
                else AgglomerativeClustering(n_clusters=n))
        return algo.fit_predict(Z)

    sample = min(len(rows), 2000)
    tried = {}
    if k:
        if not 2 <= int(k) <= min(20, len(rows) - 1):
            raise MlError("k는 2 이상 20 이하(행 수보다 작게)입니다.")
        candidates = [int(k)]
    else:
        candidates = list(range(2, min(10, len(rows) - 1) + 1))
    best, labels = None, None
    for n in candidates:
        lab = fit(n)
        if len(set(lab)) < 2:
            continue
        s = float(silhouette_score(Z, lab, sample_size=sample, random_state=SEED))
        tried[n] = round(s, 4)
        if best is None or s > tried[best]:
            best, labels = n, lab
    if labels is None:
        raise MlError("묶음을 만들지 못했습니다 — 행이 서로 너무 같습니다.")
    groups = []
    for g in range(best):
        idx = [i for i, lab in enumerate(labels) if lab == g]
        members = [rows[i] for i in idx]
        summary = {}
        for f in feats:
            if prof[f]["kind"] == "numeric":
                vals = [x for x in (number(r.get(f)) for r in members) if x is not None]
                if vals:
                    summary[f] = round(sum(vals) / len(vals), 4)
            elif prof[f]["kind"] == "category":
                top = Counter(str(r.get(f)) for r in members if _present(r.get(f))).most_common(1)
                if top:
                    summary[f] = f"{top[0][0]} ({top[0][1]}/{len(members)})"
        groups.append({"cluster": g, "size": len(idx), "profile": summary})
    return {"task": "clustering", "method": method, "rows": len(rows), "features": feats, "k": best,
            "silhouette": tried[best], "tried": tried, "clusters": groups,
            "assignments": [{"row": i, "cluster": int(lab)} for i, lab in enumerate(labels[:MAX_ASSIGN])],
            "notes": notes + more + ([f"배정은 앞의 {MAX_ASSIGN}행만 실었습니다."] if len(rows) > MAX_ASSIGN else [])}


# ---------------------------------------------------------------- 시계열 예측

def _freq(days: list[date]) -> tuple[str, int]:
    """간격 — (D·W·M, 계절 주기). 간격의 중앙값으로 정한다."""
    gaps = sorted((b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0)
    g = gaps[len(gaps) // 2] if gaps else 1
    if g >= 25:
        return "M", 12
    if g >= 6:
        return "W", 52
    return "D", 7


def _step(d: date, freq: str, n: int) -> date:
    if freq == "M":
        months = d.year * 12 + d.month - 1 + n
        y, m = divmod(months, 12)
        return date(y, m + 1, min(d.day, 28))
    return d + timedelta(days=n * (7 if freq == "W" else 1))


def forecast(rows, date_col: str, value: str, horizon: int = 6, agg: str = "sum") -> dict:
    """날짜 열과 수 열로 앞으로 horizon 개 시점을 예측한다 — 추세(선형) + 계절(주기마다 자리) 회귀. 같은 날짜는 합치거나
    (sum) 평균한다(mean). 마지막 몇 시점을 떼어 놓고 뒤로 시험한 오차(MAE·MAPE)와 예측 구간(±1.96σ)을 함께 준다."""
    _need()
    import numpy as np  # noqa: PLC0415
    from sklearn.linear_model import Ridge  # noqa: PLC0415

    columns, rows, notes = table(rows)
    for c in (date_col, value):
        if c not in columns:
            raise MlError(f"열이 없습니다: {c}")
    if agg not in ("sum", "mean"):
        raise MlError("agg는 sum·mean 중 하나입니다.")
    series: dict[date, list[float]] = {}
    for r in rows:
        d, x = day(r.get(date_col)), number(r.get(value))
        if d and x is not None:
            series.setdefault(d, []).append(x)
    days = sorted(series)
    if len(days) < 8:
        raise MlError(f"날짜가 다른 시점이 {len(days)}개뿐입니다(최소 8).")
    freq, period = _freq(days)
    if freq == "M":    # 월 단위면 같은 달을 하나로
        monthly: dict[date, list[float]] = {}
        for d in days:
            monthly.setdefault(date(d.year, d.month, 1), []).extend(series[d])
        series, days = monthly, sorted(monthly)
    y = np.array([sum(series[d]) if agg == "sum" else sum(series[d]) / len(series[d]) for d in days])
    horizon = max(1, min(int(horizon or 6), 60))
    seasonal = len(days) >= 2 * period

    def slot(d: date) -> int:
        return (d.month - 1) if freq == "M" else (d.isocalendar()[1] - 1) % 52 if freq == "W" else d.weekday()

    def design(ds: list[date], t0: int) -> np.ndarray:
        cols = [[t0 + i] for i in range(len(ds))]
        if seasonal:
            for row, d in zip(cols, ds):
                row += [1.0 if slot(d) == s else 0.0 for s in range(1, period)]
        return np.array(cols, dtype=float)

    hold = max(1, min(horizon, len(days) // 4))
    back = Ridge(alpha=1.0).fit(design(days[:-hold], 0), y[:-hold])
    guess = back.predict(design(days[-hold:], len(days) - hold))
    err = np.abs(guess - y[-hold:])
    mape = float(np.mean(err / np.where(y[-hold:] == 0, np.nan, np.abs(y[-hold:])))) if np.any(y[-hold:]) else None
    model = Ridge(alpha=1.0).fit(design(days, 0), y)
    sigma = float(np.std(y - model.predict(design(days, 0))))
    future = [_step(days[-1], freq, i + 1) for i in range(horizon)]
    pred = model.predict(design(future, len(days)))
    return {"task": "forecast", "date": date_col, "value": value, "agg": agg, "frequency": freq,
            "points": len(days), "seasonal": seasonal, "trend_per_step": round(float(model.coef_[0]), 4),
            "backtest": {"points": hold, "mae": round(float(err.mean()), 4),
                         "mape": None if mape is None or math.isnan(mape) else round(mape, 4)},
            "history_tail": [{"date": d.isoformat(), "value": round(float(v), 4)} for d, v in zip(days[-12:], y[-12:])],
            "forecast": [{"date": d.isoformat(), "value": round(float(p), 4),
                          "low": round(float(p - 1.96 * sigma), 4), "high": round(float(p + 1.96 * sigma), 4)}
                         for d, p in zip(future, pred)],
            "notes": notes + ([] if seasonal else [f"시점이 {2 * period}개보다 적어 계절을 넣지 않았습니다(추세만)."])}


# ---------------------------------------------------------------- 최적화(선형·정수 계획)

MAX_VARS = 500
MAX_CONSTRAINTS = 500
_OPS = ("<=", ">=", "=")
_STATUS = {0: "최적해를 찾았습니다", 1: "반복·시간 상한에 걸렸습니다(가장 나은 해)", 2: "조건을 모두 만족하는 해가 없습니다",
           3: "목적값이 끝없이 좋아집니다(조건이 부족합니다)", 4: "수치 문제로 풀지 못했습니다"}


def optimize(objective: dict, constraints: list, sense: str = "min", bounds: dict | None = None,
             integers: list | None = None) -> dict:
    """선형·정수 계획 — 목적식(변수 → 계수)을 조건(계수·부등호·우변) 안에서 최소/최대로. 변수는 기본 0 이상.
    integers의 변수는 정수(0/1 선택이면 bounds를 [0, 1]로). 어느 조건이 빠듯한지(slack 0)도 알려 준다."""
    _need()
    import numpy as np  # noqa: PLC0415
    from scipy.optimize import Bounds, LinearConstraint, milp  # noqa: PLC0415

    if not isinstance(objective, dict) or not objective:
        raise MlError("objective는 {변수: 계수}입니다.")
    if sense not in ("min", "max"):
        raise MlError("sense는 min·max 중 하나입니다.")
    constraints = constraints if isinstance(constraints, list) else []
    if len(constraints) > MAX_CONSTRAINTS:
        raise MlError(f"조건은 {MAX_CONSTRAINTS}개까지입니다.")
    names = list(objective)
    for c in constraints:
        if not isinstance(c, dict) or not isinstance(c.get("coefs"), dict) or c.get("op") not in _OPS \
                or number(c.get("rhs")) is None:
            raise MlError('조건은 {"name": "", "coefs": {변수: 계수}, "op": "<=|>=|=", "rhs": 수}입니다.')
        names += [v for v in c["coefs"] if v not in names]
    if len(names) > MAX_VARS:
        raise MlError(f"변수는 {MAX_VARS}개까지입니다.")
    at = {v: i for i, v in enumerate(names)}

    def coef(x, what: str) -> float:
        n = number(x)
        if n is None:
            raise MlError(f"{what}의 계수가 수가 아닙니다: {x!r}")
        return n

    c = np.array([coef(objective.get(v, 0), "objective") for v in names])
    A, lo, hi = [], [], []
    for k in constraints:
        row = np.zeros(len(names))
        for v, a in k["coefs"].items():
            row[at[v]] = coef(a, k.get("name") or "조건")
        rhs = number(k["rhs"])
        A.append(row)
        lo.append(rhs if k["op"] in (">=", "=") else -np.inf)
        hi.append(rhs if k["op"] in ("<=", "=") else np.inf)
    lb, ub = np.zeros(len(names)), np.full(len(names), np.inf)
    for v, b in (bounds or {}).items():
        if v not in at or not isinstance(b, (list, tuple)) or len(b) != 2:
            raise MlError(f"bounds는 {{변수: [하한, 상한]}}입니다(null은 제한 없음): {v}")
        lb[at[v]] = -np.inf if b[0] is None else coef(b[0], "bounds")
        ub[at[v]] = np.inf if b[1] is None else coef(b[1], "bounds")
    integrality = np.array([1 if v in set(integers or []) else 0 for v in names])
    res = milp(c=-c if sense == "max" else c, constraints=[LinearConstraint(np.array(A), lo, hi)] if A else [],
               bounds=Bounds(lb, ub), integrality=integrality, options={"time_limit": 30})
    out = {"task": "optimization", "sense": sense, "variables": len(names), "constraints": len(constraints),
           "status": _STATUS.get(res.status, str(res.message)), "success": bool(res.success)}
    if res.x is not None:
        x = res.x
        out["objective"] = round(float(c @ x), 6)
        out["values"] = {v: (round(float(x[i])) if integrality[i] else round(float(x[i]), 6)) for i, v in enumerate(names)}
        out["constraints_used"] = [
            {"name": k.get("name") or f"조건{j + 1}", "lhs": round(float(A[j] @ x), 6), "op": k["op"],
             "rhs": number(k["rhs"]), "binding": abs(float(A[j] @ x) - number(k["rhs"])) < 1e-6}
            for j, k in enumerate(constraints)]
    return out


# ---------------------------------------------------------------- 파일에서 읽기(문서 저장소의 CSV·엑셀)

MAX_FILE_BYTES = 20_000_000


def rows_from_file(path) -> list[dict]:
    """CSV는 그대로, 엑셀(xlsx)·그 밖의 문서는 본문 추출(doctext)이 만든 첫 번째 표를. 머리글이 열 이름이다."""
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from . import doctext, ontology  # noqa: PLC0415

    path = Path(path)
    if not path.is_file():
        raise MlError(f"파일이 없습니다: {path.name}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise MlError(f"파일이 {MAX_FILE_BYTES // 1_000_000}MB보다 큽니다: {path.name}")
    if path.suffix.lower() in (".csv", ".tsv", ".txt"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp949"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
        dialect = "excel-tab" if path.suffix.lower() == ".tsv" or "\t" in text.split("\n", 1)[0] else "excel"
        return [dict(r) for r, _ in zip(csv.DictReader(io.StringIO(text), dialect=dialect), range(MAX_ROWS + 1))]
    try:
        markdown = doctext.extract(path)[0]
    except Exception as e:  # noqa: BLE001
        raise MlError(f"파일을 읽지 못했습니다: {path.name} — {str(e)[:200]}")
    lines = [line for line in markdown.splitlines() if line.strip()]
    for i in range(1, len(lines)):
        if ontology._TABLE_SEP_RE.match(lines[i]):
            head = ontology._cells(lines[i - 1])
            out = []
            for line in lines[i + 1:]:
                if not line.strip().startswith("|"):
                    break
                cells = ontology._cells(line)
                out.append({h: (cells[k] if k < len(cells) else "") for k, h in enumerate(head) if h})
            if out:
                return out
    raise MlError(f"표를 찾지 못했습니다: {path.name} — CSV나 머리글 행이 있는 엑셀을 주세요.")
