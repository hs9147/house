"""대화 문맥 회귀 평가(services/contexteval) — 시나리오가 성립하고, 채점이 맞는 답·틀린 답을 가르는지.

실제 모델로 도는 평가는 `python -m app.services.contexteval`이다. 여기서는 모델 없이 채점기만 본다.
"""
import json

from app.services import contexteval, convcontext


def test_every_case_points_only_at_artifacts_that_exist():
    for case in contexteval.cases():
        ids = {a["id"] for a in convcontext.artifacts_of(case["history"])}
        assert set(case["expect"].get("refers", [])) <= ids, case["id"]
        assert case["history"][-1]["role"] == "user" and "id" not in case["history"][-1], case["id"]
        assert convcontext.needs_restate(case["history"]) or case["id"] == "keep-old-brief", case["id"]


def test_scoring_passes_a_right_answer_and_names_what_a_wrong_one_missed():
    right = {"goal": "협력사 단가 비교", "requirements": ["대상: 2026 상반기 계약", "부서 열 추가"], "decisions": [],
             "open": [], "request": "보고서 「협력사 단가 비교」(r2)에 부서 열을 추가해 다시 내 줘", "refers": ["r2"]}
    out = contexteval.run(lambda prompt: json.dumps(right, ensure_ascii=False), only=["pronoun-table"])
    assert out["rate"] == 1.0 and out["cases"][0]["misses"] == []

    wrong = {**right, "request": "그 표에 부서 열도 넣어 줘", "refers": []}
    out = contexteval.run(lambda prompt: json.dumps(wrong, ensure_ascii=False), only=["pronoun-table"])
    assert out["rate"] == 0.0
    assert any("refers" in m for m in out["cases"][0]["misses"])
    assert "요청에 '그 표'가 남음" in out["cases"][0]["misses"]
    # 모델이 JSON이 아닌 글을 주면 실패로 센다
    out = contexteval.run(lambda prompt: "원장입니다", only=["ambiguous"], repeat=2)
    assert out["cases"][0]["passed"] == 0 and out["cases"][0]["runs"] == 2
