"""경로 틀(services/pathshape) — 운영 저장소에서 잰 네 가지 모양을 본뜬 경로로 칸 판정을 본다(실제 경로는 싣지 않는다)."""
from app.services import pathshape


def _contracts():
    paths = []
    for i in range(40):
        no = f"KR-CT{2301 + i % 30:04d}{i:05d}"
        folder = f"{no}_{i % 3}"
        paths += [f"{folder}/{no}_{no}_0.pdf", f"{folder}/{no}_협력사 지정 품의서.pdf",
                  f"{folder}/{no}_Rnd Outsourcing_표준견적서양식_v1.2.xls", f"{folder}/{no}_RE 확인요청_20251203.eml"]
    paths.append("gpax-4.0---계약조회/pages/GPS-4.0-0b63e6.md")     # 데이터 수집이 쓴 폴더 — 틀 밖
    return paths


def _credit():
    paths = []
    for i, name in enumerate(["(주)지엔텔", "MEKAS CO.,LTD", "유니챌주식회사", "(주)유비샘", "다보코퍼레이션(주)", "(주)인피닉"]):
        paths += [f"KR05{i:04d}_{name}/{y}년 신용평가.pdf" for y in (22, 23, 24)]
    return paths


def test_record_folders_with_a_revision_number_are_one_contract_each():
    out = pathshape.analyze(_contracts())
    assert out["kind"] == "건 폴더"
    folder = next(s for s in out["slots"] if s["where"] == "folder" and s["role"] == "identifier")
    assert folder["pattern"] == r"KR\-CT\d{9}" and folder["tail"] == "number"
    assert folder["tail_values"] == {0: 14, 1: 13, 2: 13} and folder["values"] == 40
    assert folder["share"] >= 0.97        # 수집 폴더 하나는 틀 밖
    file_slot = next(s for s in out["slots"] if s["where"] == "file" and s["role"] == "identifier")
    assert file_slot["groups"] == ["*"] and file_slot["repeats_folder"] and file_slot["share"] == 1.0
    # 파일 이름의 날짜(20251203)·차수 숫자는 식별자가 아니다
    assert not any(s["where"] == "file" and s["pattern"].startswith(r"\d") for s in out["slots"])


def test_entity_folders_carry_a_code_a_name_and_yearly_files():
    out = pathshape.analyze(_credit())
    assert out["kind"] == "대상 폴더"
    folder = next(s for s in out["slots"] if s["where"] == "folder")
    assert (folder["role"], folder["pattern"], folder["tail"]) == ("identifier", r"KR\d{6}", "name")
    year = next(s for s in out["slots"] if s["where"] == "file" and s["role"] == "year")
    assert year["share"] == 1.0 and year["groups"] == ["*"]


def test_codes_in_flat_file_names_and_year_and_version_folders():
    intro = [f"(주)회사{i}_KR05{i:04d}_회사소개서.pdf" for i in range(9)] + ["안내.pdf"]
    out = pathshape.analyze(intro)
    assert out["kind"] == "평평함"
    slot = next(s for s in out["slots"] if s["where"] == "file")
    assert (slot["role"], slot["pattern"], slot["share"]) == ("identifier", r"KR\d{6}", 0.9)

    work = [f"WPR(팀)/{y}년/W{w} 주간보고.pptx" for y in (2022, 2023, 2024) for w in (1, 2)]
    work += ["KPI/2024/MBR Master File/History/실적 Master_v0.91.xlsx", "KPI/2025/경쟁평가율/Raw/VI Tracker.xlsx",
             "MBR(팀)/2024년/3월 MBR.pptx"]
    out = pathshape.analyze(work)
    roles = {m["folder"]: m["role"] for m in out["marked"]}
    assert roles["WPR(팀)/2024년"] == "year" and roles["KPI/2024"] == "year"
    assert roles["KPI/2024/MBR Master File/History"] == "version" and roles["KPI/2025/경쟁평가율/Raw"] == "raw"
    assert out["kind"] == "종류/기간"
    top = next(s for s in out["slots"] if s["role"] == "category" and not s["parent"])
    assert set(top["names"]) == {"WPR(팀)", "KPI", "MBR(팀)"}      # 셋뿐이어도 맨 위의 종류는 낸다


def test_generated_ids_are_identifiers_but_amounts_are_not():
    docs = [f"documents/doc-2026102{i}-{tail}/본문.md" for i, tail in enumerate(["h47d", "gh6n", "n5aw", "b6ds", "oym5"])]
    out = pathshape.analyze(docs)
    slot = next(s for s in out["slots"] if s["where"] == "folder" and s["parent"] == "documents")
    assert slot["role"] == "identifier" and slot["pattern"] == r"doc\-\d{8}\-[a-z0-9]{4}"
    amounts = pathshape.analyze([f"목표/{n}억원기준_24년 목표.xlsx" for n in (3080, 3100, 3500, 3600, 3700)])
    assert not any(s["role"] == "identifier" for s in amounts["slots"])


def test_shared_identifier_shapes_link_stores(monkeypatch):
    """같은 모양의 식별자가 두 저장소에 나오면 값이 몇 개 겹치는지 — 저장소 사이의 연결 후보."""
    from app.services import storage

    data = {"credit": _credit(), "intro": [f"(주)회사{i}_KR05{i:04d}_회사소개서.pdf" for i in range(2, 9)]}
    monkeypatch.setattr(pathshape, "_paths", lambda name: data[name])
    monkeypatch.setattr(pathshape.storage_service, "indexed_stores",
                        lambda: [storage.Store(n, None, False) for n in data])
    out = pathshape.profile_all()
    link = next(link for link in out["links"] if link["pattern"] == r"KR\d{6}")
    assert link["stores"] == {"credit": 6, "intro": 7} and link["shared"] == 4


def test_mixed_file_names_keep_real_identifiers_but_not_version_dates_and_counters_share_a_shape():
    """파일 이름 식별자는 다른 문서와 섞여 있어도 같은 모양이 5건 이상이면 칸이다(contracts의 KR-PR 구매요청 번호). 글자 하나에
    날짜를 붙인 판 표시(V20241224)는 식별자가 아니고, 붙여 센 번호(-1 · -10)는 한 모양이다."""
    files = [f"KR-CT2401{i:05d}_0/KR-CT2401{i:05d}_품의서_KR-PR2403{i:05d}.pdf" for i in range(6)]
    files += [f"KR-CT2401{i:05d}_0/KR-CT2401{i:05d}_견적서_V2024122{i}.xls" for i in range(6)]
    files += [f"KR-CT2401{i:05d}_0/KR-CT2401{i:05d}_계약서.doc" for i in range(6)]
    out = pathshape.analyze(files)
    patterns = {s["pattern"] for s in out["slots"] if s["where"] == "file" and s["role"] == "identifier"}
    assert r"KR\-PR\d{9}" in patterns and not any(p.startswith("V") for p in patterns)
    assert pathshape.shape("r-doc-20261028-gh6n-1") == pathshape.shape("r-doc-20261028-gh6n-10")
