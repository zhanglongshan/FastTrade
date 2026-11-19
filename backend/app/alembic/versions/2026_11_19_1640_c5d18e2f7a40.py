"""量化回测与实时策略增加杠杆字段。

Revision ID: c5d18e2f7a40
Revises: b4e7c1a9d3f2
Create Date: 2026-11-19 16:40:00.000000

背景 | Background:
    合约回测/模拟盘需要支持杠杆。杠杆决定保证金占用与强平判定，属于必须随记录持久化的
    配置：回测记录要能复现"当时用的是几倍杠杆"，实时实例重启后也要按同一倍数恢复账本。
    Contract backtests and paper trading need leverage. Leverage drives margin and
    liquidation, so it must persist with the record: a backtest has to reproduce the leverage
    it ran with, and a live instance has to restore its ledger with the same multiple.

影响 | Impact:
    - quant_web3_backtest.leverage：历史记录统一回填 1（现货全额），口径不变。
    - quant_web3_live_strategy.leverage：历史实例回填 1，恢复后行为与升级前一致。
    Existing rows are backfilled with 1 so their behaviour stays identical after the upgrade.
"""

from typing import Sequence, Union  # 导入类型标注 | Import typing helpers

from alembic import op  # 导入迁移操作对象 | Import Alembic operations
import sqlalchemy as sa  # 导入 SQLAlchemy 类型 | Import SQLAlchemy types

# revision identifiers, used by Alembic.
revision: str = "c5d18e2f7a40"
down_revision: Union[str, None] = "b4e7c1a9d3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKTEST_TABLE = "quant_web3_backtest"
LIVE_TABLE = "quant_web3_live_strategy"


def upgrade() -> None:
    """为回测记录与实时实例增加杠杆字段（历史数据回填 1 倍）。"""
    for table in (BACKTEST_TABLE, LIVE_TABLE):  # 两张表加同一列 | Add the same column to both tables
        op.add_column(
            table,
            sa.Column(
                "leverage",
                sa.Float(),
                nullable=False,
                server_default="1",
                comment="杠杆倍数，1=现货全额",
            ),
        )


def downgrade() -> None:
    """移除杠杆字段（降级后杠杆一律按 1 倍处理）。"""
    for table in (BACKTEST_TABLE, LIVE_TABLE):  # 逐表删除列 | Drop the column from both tables
        op.drop_column(table, "leverage")
