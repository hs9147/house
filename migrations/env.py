"""Alembic env — PAAS_DATABASE_URL + app 모델 메타데이터에 연결.

**마이그레이션은 이미 된 것을 건너뛴다(_idempotent_ops).** 앱은 기동할 때마다 없는 표를 직접 만든다(create_all).
그래서 앱이 먼저 표를 만든 DB에서 그 표를 다시 만드는 리비전이 「already exists」로 실패하고, 한번 막히면 SW
업데이트마다 같은 자리에서 멈춰 뒤의 리비전(열 추가·자료 옮기기)이 하나도 적용되지 않았다(실측: mcp_tokens에서
멈춤). 리비전 서른 개를 하나씩 고치는 대신 여기서 표·열·색인·외래키 만들기는 「이미 있으면」, 지우기는 「이미
없으면」 건너뛰게 감싼다 — 앞으로 만들 리비전에도 그대로 적용된다. 자료를 옮기는 SQL(op.execute)은 건드리지 않는다.
"""
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import context
from alembic.operations import BatchOperations, Operations
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app import models  # noqa: E402,F401 — 모델 등록

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def _say(what: str) -> None:
    print(f"[alembic] 이미 되어 있어 건너뜀: {what}")


def _idempotent_ops() -> None:
    """Operations·BatchOperations의 만들기·지우기를 「이미 되어 있으면 건너뛰기」로 감싼다(한 번만)."""
    if getattr(Operations, "_paas_idempotent", False):
        return
    Operations._paas_idempotent = True

    def insp(ops):
        return sa.inspect(ops.get_bind())

    def has_table(ops, table, schema=None):
        return insp(ops).has_table(table, schema=schema)

    def columns(ops, table, schema=None):
        return {c["name"] for c in insp(ops).get_columns(table, schema=schema)} if has_table(ops, table, schema) else set()

    def indexes(ops, table, schema=None):
        if not has_table(ops, table, schema):
            return set()
        i = insp(ops)
        return {x["name"] for x in i.get_indexes(table, schema=schema)} | \
            {x["name"] for x in i.get_unique_constraints(table, schema=schema) if x.get("name")}

    def has_fk(ops, table, cols, referent, schema=None):
        if not has_table(ops, table, schema):
            return False
        return any(fk.get("referred_table") == referent and list(fk.get("constrained_columns") or []) == list(cols)
                   for fk in insp(ops).get_foreign_keys(table, schema=schema))

    orig = {name: getattr(Operations, name) for name in (
        "create_table", "drop_table", "add_column", "drop_column", "create_index", "drop_index", "create_foreign_key")}
    borig = {name: getattr(BatchOperations, name) for name in (
        "add_column", "drop_column", "create_index", "drop_index", "create_foreign_key")}

    def create_table(self, table_name, *cols, **kw):
        if has_table(self, table_name, kw.get("schema")):
            _say(f"표 {table_name}")
            # 리비전이 돌려받은 표로 자료를 넣을 수 있게(op.bulk_insert) — 이름과 열 이름만 있는 표를 준다
            return sa.table(table_name, *[sa.column(c.name) for c in cols if isinstance(c, sa.Column)])
        return orig["create_table"](self, table_name, *cols, **kw)

    def drop_table(self, table_name, **kw):
        if not has_table(self, table_name, kw.get("schema")):
            _say(f"표 {table_name} 지우기(이미 없음)")
            return None
        return orig["drop_table"](self, table_name, **kw)

    def add_column(self, table_name, column, **kw):
        if column.name in columns(self, table_name, kw.get("schema")):
            _say(f"열 {table_name}.{column.name}")
            return None
        return orig["add_column"](self, table_name, column, **kw)

    def drop_column(self, table_name, column_name, **kw):
        if has_table(self, table_name, kw.get("schema")) and column_name not in columns(self, table_name, kw.get("schema")):
            _say(f"열 {table_name}.{column_name} 지우기(이미 없음)")
            return None
        return orig["drop_column"](self, table_name, column_name, **kw)

    def create_index(self, index_name, table_name, cols, **kw):
        if index_name and str(index_name) in indexes(self, table_name, kw.get("schema")):
            _say(f"색인 {index_name}")
            return None
        return orig["create_index"](self, index_name, table_name, cols, **kw)

    def drop_index(self, index_name, table_name=None, **kw):
        if table_name and has_table(self, table_name, kw.get("schema")) and \
                str(index_name) not in indexes(self, table_name, kw.get("schema")):
            _say(f"색인 {index_name} 지우기(이미 없음)")
            return None
        return orig["drop_index"](self, index_name, table_name=table_name, **kw)

    def create_foreign_key(self, constraint_name, source_table, referent_table, local_cols, remote_cols, **kw):
        if has_fk(self, source_table, local_cols, referent_table, kw.get("source_schema")):
            _say(f"외래키 {source_table}({', '.join(local_cols)}) → {referent_table}")
            return None
        return orig["create_foreign_key"](self, constraint_name, source_table, referent_table, local_cols,
                                          remote_cols, **kw)

    # 묶음(batch_alter_table) — 표 이름은 묶음이 안다. SQLite는 묶음 끝에 표를 다시 만들므로 미리 보고 거른다.
    def b_table(self):
        return self.impl.table_name, self.impl.schema

    def b_add_column(self, column, **kw):
        table, schema = b_table(self)
        if column.name in columns(self, table, schema):
            _say(f"열 {table}.{column.name}")
            return None
        return borig["add_column"](self, column, **kw)

    def b_drop_column(self, column_name, **kw):
        table, schema = b_table(self)
        if has_table(self, table, schema) and column_name not in columns(self, table, schema):
            _say(f"열 {table}.{column_name} 지우기(이미 없음)")
            return None
        return borig["drop_column"](self, column_name, **kw)

    def b_create_index(self, index_name, cols, **kw):
        table, schema = b_table(self)
        if index_name and str(index_name) in indexes(self, table, schema):
            _say(f"색인 {index_name}")
            return None
        return borig["create_index"](self, index_name, cols, **kw)

    def b_drop_index(self, index_name, **kw):
        table, schema = b_table(self)
        if has_table(self, table, schema) and str(index_name) not in indexes(self, table, schema):
            _say(f"색인 {index_name} 지우기(이미 없음)")
            return None
        return borig["drop_index"](self, index_name, **kw)

    def b_create_foreign_key(self, constraint_name, referent_table, local_cols, remote_cols, **kw):
        table, schema = b_table(self)
        if has_fk(self, table, local_cols, referent_table, schema):
            _say(f"외래키 {table}({', '.join(local_cols)}) → {referent_table}")
            return None
        return borig["create_foreign_key"](self, constraint_name, referent_table, local_cols, remote_cols, **kw)

    for name, fn in (("create_table", create_table), ("drop_table", drop_table), ("add_column", add_column),
                     ("drop_column", drop_column), ("create_index", create_index), ("drop_index", drop_index),
                     ("create_foreign_key", create_foreign_key)):
        setattr(Operations, name, fn)
    for name, fn in (("add_column", b_add_column), ("drop_column", b_drop_column), ("create_index", b_create_index),
                     ("drop_index", b_drop_index), ("create_foreign_key", b_create_foreign_key)):
        setattr(BatchOperations, name, fn)


def _catch_up(connection) -> list[str]:
    """리비전을 다 돈 뒤 — 모델에는 있는데 DB에는 없는 **표와 열**을 만든다(더하기만, 바꾸거나 지우지 않는다).
    표는 앱이 기동할 때 만드는 것(create_all)과 같다 — 리비전 없이 모델에만 있는 표가 있었다.

    모델에 열을 더하고 리비전을 빠뜨린 일이 있었다(실측: data_schemas.db_name·unit_id, personal_contexts.files) —
    그런 열은 upgrade head가 영영 만들지 못해 그 표를 쓰는 화면이 500이 된다. 표는 앱이 만든다(create_all). 더하는
    열은 늘 비울 수 있게(nullable) 만들고, 모델 기본값이 글자·숫자·참거짓이면 DB 기본값으로 둔다(이미 있는 행에 들어간다).
    """
    before = set(sa.inspect(connection).get_table_names())
    target_metadata.create_all(connection, checkfirst=True)
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names())
    added = [f"표 {t}" for t in sorted(existing - before)]
    for t in sorted(existing - before):
        print(f"[alembic] 리비전에 없던 표를 만듦: {t}")
    for table in target_metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            default = col.default.arg if col.default is not None and col.default.is_scalar else None
            server_default = (sa.text("1" if default else "0") if isinstance(default, bool)
                              else sa.text(str(default)) if isinstance(default, (int, float))
                              else default if isinstance(default, str) else None)
            ddl = sa.Column(col.name, col.type, nullable=True, server_default=server_default)
            context.get_context().impl.add_column(table.name, ddl)
            added.append(f"{table.name}.{col.name}")
            print(f"[alembic] 리비전에 없던 열을 더함: {table.name}.{col.name}")
    return added


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    _idempotent_ops()
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
            # upgrade(head)일 때만 — downgrade는 열을 거두는 중이다
            # 끝까지(head) 올라왔을 때만 — downgrade나 중간 리비전까지만 올리는 중에는 하지 않는다
            from alembic.script import ScriptDirectory  # noqa: PLC0415

            heads = set(ScriptDirectory.from_config(context.config).get_heads())
            if set(context.get_context().get_current_heads()) == heads:
                _catch_up(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
