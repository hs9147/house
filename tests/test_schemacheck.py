"""DB 스키마가 코드보다 뒤처졌을 때 화면이 그것을 말하는가.

`create_all`은 없는 **테이블**만 만들고 기존 테이블에 **컬럼을 추가하지 않는다.** 그래서
마이그레이션을 돌리지 않고 새 버전으로 올리면 그 테이블을 읽는 엔드포인트가 전부 500이
되는데, 화면에는 "Internal Server Error"만 남는다. 같은 일을 세 번 겪었다:
projects.structure · chat_sessions.merged_at · build_tasks.number.

기동 로그에만 적으면 로그를 보는 사람만 안다 — /health에 실어 콘솔이 띄울 수 있게 한다.
"""
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.schemacheck import RECOVERY, missing_columns

ADMIN = {"x-api-key": "test-admin-key"}


def _engine_with_table(tmp_path, columns: str):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE widgets ({columns})"))
    return engine


def _metadata(columns: list[sa.Column]):
    meta = sa.MetaData()
    sa.Table("widgets", meta, *columns)
    return meta


def test_names_the_column_that_is_missing(tmp_path):
    """이름을 말해야 복구할 수 있다 — "스키마가 다르다"만으로는 ALTER TABLE을 쓸 수 없다."""
    engine = _engine_with_table(tmp_path, "id INTEGER PRIMARY KEY")
    meta = _metadata([sa.Column("id", sa.Integer, primary_key=True),
                      sa.Column("number", sa.Integer)])
    assert missing_columns(engine, meta) == ["widgets.number"]


def test_matching_schema_reports_nothing(tmp_path):
    engine = _engine_with_table(tmp_path, "id INTEGER PRIMARY KEY, number INTEGER")
    meta = _metadata([sa.Column("id", sa.Integer, primary_key=True),
                      sa.Column("number", sa.Integer)])
    assert missing_columns(engine, meta) == []


def test_table_that_does_not_exist_yet_is_not_a_missing_column(tmp_path):
    """create_all이 곧 만든다 — 없는 테이블을 '컬럼 누락'으로 세면 거짓 경고가 된다."""
    engine = _engine_with_table(tmp_path, "id INTEGER PRIMARY KEY")
    meta = sa.MetaData()
    sa.Table("gadgets", meta, sa.Column("id", sa.Integer, primary_key=True))
    assert missing_columns(engine, meta) == []


def test_broken_engine_does_not_raise(tmp_path):
    """진단이 기동을 막으면 본말전도다 — 못 보면 조용히 빈 목록."""
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "nope" / "deep" / "x.db"))
    assert missing_columns(engine, sa.MetaData()) == []


def test_health_carries_the_schema_gap_and_how_to_fix_it():
    """콘솔이 배너로 띄우려면 목록과 복구 방법이 응답에 있어야 한다."""
    body = TestClient(create_app()).get("/paas/health").json()
    # 테스트 DB는 create_all로 만들어져 최신이다 — 목록은 비어 있고 키는 있어야 한다
    assert body["schema_missing"] == []
    assert body["schema_recovery"] == RECOVERY
    assert "alembic upgrade head" in body["schema_recovery"]
