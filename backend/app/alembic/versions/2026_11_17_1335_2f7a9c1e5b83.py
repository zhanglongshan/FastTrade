"""migrate quant web3 data and drop legacy tables

Revision ID: 2f7a9c1e5b83
Revises: d6f54025b384
Create Date: 2026-11-17 13:35:00.000000

并行自动生成的新表（quant_web3_*）已经建立，但旧表（quant_*）中还有历史数据。
本迁移把旧表数据搬入新表（按主键去重，幂等），随后删除旧表，完成表重命名。

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "2f7a9c1e5b83"
down_revision: Union[str, None] = "d6f54025b384"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 旧表 -> 新表，列清单一致（两代表结构完全相同）
_TABLES = {
    "quant_dataset": "quant_web3_dataset",
    "quant_backtest": "quant_web3_backtest",
    "quant_live_strategy": "quant_web3_live_strategy",
    "quant_trade_record": "quant_web3_trade_record",
}


def _copy_and_drop(old: str, new: str, bind) -> None:
    """把 old 的数据按 id 去重搬入 new，然后删除 old。"""
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if old not in tables:
        return
    if new not in tables:
        # 新表缺失则直接改名为新表，保留数据
        op.rename_table(old, new)
        return

    columns = [col["name"] for col in inspector.get_columns(new)]
    col_list = ", ".join(columns)

    bind.execute(
        text(
            f"INSERT INTO {new} ({col_list}) "
            f"SELECT {col_list} FROM {old} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {new} WHERE {new}.id = {old}.id)"
        )
    )
    op.drop_table(old)


def upgrade() -> None:
    """旧表数据迁入 quant_web3_* 并删除旧表。"""
    bind = op.get_bind()
    for old, new in _TABLES.items():
        _copy_and_drop(old, new, bind)


def downgrade() -> None:
    """降级不自动拆回旧表：新表数据完整保留，避免回滚丢数据。"""
    # 如需回滚到旧表名，可手工执行 ALTER TABLE quant_web3_* RENAME TO quant_*
    pass
