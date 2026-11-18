"""ML 로직(services/mllogic · MCP /mcp/ml) — 예측·분류·클러스터링·시계열·최적화를 계산하고, 믿을지 정할 점수를 함께 준다."""
import json
import random

import pytest

from app.config import get_settings
from app.services import mllogic

pytest.importorskip("sklearn")
pytest.importorskip("scipy")


def _vendors(n=120):
    rnd = random.Random(1)
    rows = []
    for i in range(n):
        emp, score = rnd.randint(10, 500), rnd.random() * 100
        rows.append({"사업자번호": f"{i:03d}-00-{i:05d}", "규모": rnd.choice(["대", "중", "소"]), "직원수": emp,
                     "점수": round(score, 1), "위험": "높음" if score < 30 else "낮음",
                     "매출": f"{emp * 12 + rnd.randint(-50, 50):,}", "메모": "긴 설명 " * 10})
    return rows


def test_columns_are_typed_and_ids_and_text_are_left_out():
    cols = mllogic.describe(_vendors())["columns"]
    assert cols["사업자번호"]["kind"] == "id" and cols["메모"]["kind"] == "text"
    assert cols["매출"]["kind"] == "numeric" and cols["규모"]["kind"] == "category"
    assert mllogic.number("1,200") == 1200 and mllogic.number("12.5%") == 0.125 and mllogic.number("abc") is None


def test_regression_compares_models_by_cross_validation_and_predicts():
    out = mllogic.regress(_vendors(), "매출", predict_rows=[{"규모": "대", "직원수": 100, "점수": 50}])
    assert set(out["scores"]) == {"linear", "forest", "boosting"} and out["model"] in out["scores"]
    assert out["scores"][out["model"]]["r2"] > 0.9
    assert "사업자번호" not in out["features"] and "메모" not in out["features"]     # 외우게 하지 않는다
    assert out["importance"][0]["feature"] == "직원수"
    assert abs(out["predictions"][0]["prediction"] - 1200) < 150
    with pytest.raises(mllogic.MlError, match="수 열이 아닙니다"):
        mllogic.regress(_vendors(), "규모")


def test_classification_reports_baseline_and_confusion():
    out = mllogic.classify(_vendors(), "위험", predict_rows=[{"규모": "소", "직원수": 30, "점수": 10}])
    assert out["scores"][out["model"]]["f1_macro"] > 0.9 > out["baseline_accuracy"]
    assert out["confusion"]["labels"] == ["낮음", "높음"] and sum(map(sum, out["confusion"]["matrix"])) == 120
    assert out["predictions"][0]["prediction"] == "높음"
    json.dumps(out, ensure_ascii=False)      # numpy 값이 섞이지 않는다


def test_clustering_picks_k_by_silhouette_and_profiles_groups():
    rows = [{"x": x + random.Random(i).random(), "y": y} for i, (x, y) in
            enumerate([(0, 0)] * 20 + [(10, 10)] * 20 + [(0, 10)] * 20)]
    out = mllogic.cluster(rows)
    assert out["k"] == 3 and sorted(g["size"] for g in out["clusters"]) == [20, 20, 20]
    assert len(out["assignments"]) == 60


def test_forecast_backtests_and_follows_trend_and_season():
    rows = [{"월": f"{y}-{m:02d}-15", "수량": base + m * 5 + (20 if m in (11, 12) else 0)}
            for y, base in ((2024, 100), (2025, 160)) for m in range(1, 13)]
    out = mllogic.forecast(rows, "월", "수량", horizon=3)
    assert out["frequency"] == "M" and out["seasonal"] and out["backtest"]["mape"] < 0.1
    assert [f["date"] for f in out["forecast"]] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert out["forecast"][0]["low"] < out["forecast"][0]["value"] < out["forecast"][0]["high"]


def test_integer_program_finds_the_optimum_and_binding_constraints():
    out = mllogic.optimize({"A": 3, "B": 5}, [
        {"name": "자재", "coefs": {"A": 1, "B": 2}, "op": "<=", "rhs": 14},
        {"name": "시간", "coefs": {"A": 3, "B": 1}, "op": "<=", "rhs": 18}], sense="max", integers=["A", "B"])
    assert out["success"] and out["objective"] == 37 and out["values"] == {"A": 4, "B": 5}
    assert [c["binding"] for c in out["constraints_used"]] == [True, False]
    bad = mllogic.optimize({"A": 1}, [{"coefs": {"A": 1}, "op": ">=", "rhs": 5},
                                      {"coefs": {"A": 1}, "op": "<=", "rhs": 3}])
    assert not bad["success"] and "해가 없습니다" in bad["status"]


def test_mcp_ml_reads_rows_or_a_store_file(monkeypatch, tmp_path, fresh_settings):
    from app.api import mcp_servers
    from app.db import SessionLocal
    from app.main import create_app

    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setenv("PAAS_DOC_ROOTS", str(root))
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    from app.db import Base, engine
    from app.models import DocumentStore, DocumentStoreRegistry
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.query(DocumentStore).delete()
        db.query(DocumentStoreRegistry).delete()
        db.commit()
    create_app()       # 레거시 경로(PAAS_DOC_ROOTS)를 DB에 이관한다 — 저장소 docs
    rows = _vendors()
    header = ["규모", "직원수", "점수", "위험"]
    (root / "협력사.csv").write_text("\n".join([",".join(header)] + [",".join(str(r[h]) for h in header) for r in rows]),
                                    encoding="utf-8-sig")
    with SessionLocal() as db:
        got = json.loads(mcp_servers._ml_call(db, None, "classify", {"source": "docs", "path": "협력사.csv",
                                                                      "target": "위험"}))
        assert got["rows"] == 120 and got["scores"][got["model"]]["f1_macro"] > 0.9
        inline = json.loads(mcp_servers._ml_call(db, None, "describe_data", {"rows": rows[:20]}))
        assert inline["rows"] == 20
        with pytest.raises(mcp_servers.mcp_server.McpToolError, match="벗어납니다"):
            mcp_servers._ml_call(db, None, "describe_data", {"source": "docs", "path": "../x.csv"})
        with pytest.raises(mcp_servers.mcp_server.McpToolError, match="데이터를 주세요"):
            mcp_servers._ml_call(db, None, "predict", {"target": "x"})
    assert [t["name"] for t in mcp_servers._ML_TOOLS] == [
        "describe_data", "predict", "classify", "cluster", "forecast", "optimize"]
