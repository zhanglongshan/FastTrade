"""量化数据集市场回填 + 回测记录增加 market 字段。

Revision ID: b4e7c1a9d3f2
Revises: 903a127afd4d
Create Date: 2026-11-18 17:40:00.000000

前一条迁移 903a127afd4d 已为数据集加上 market 列，本迁移负责：
- 按 source / file_path 回填历史数据集的市场；
- 为回测记录增加 market 列，并按其交易对命中的数据集回填。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b4e7c1a9d3f2"
down_revision: Union[str, None] = "903a127afd4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATASET_TABLE = "quant_web3_dataset"
BACKTEST_TABLE = "quant_web3_backtest"


def upgrade() -> None:
    """回填数据集市场，并为回测记录新增市场字段。"""
    # 数据集 market 列由 903a127afd4d 添加，这里只回填历史数据
    op.execute(
        f"""
        UPDATE {DATASET_TABLE}
        SET market = 'futures'
        WHERE source = 'binance_futures'
           OR replace(file_path, chr(92), '/') LIKE '%/futures/%'
        """
    )

    op.add_column(
        BACKTEST_TABLE,
        sa.Column(
            "market",
            sa.String(length=10),
            nullable=False,
            server_default="spot",
            comment="市场：spot-现货，futures-合约",
        ),
    )
    op.execute(
        f"""
        UPDATE {BACKTEST_TABLE} AS bt
        SET market = 'futures'
        WHERE EXISTS (
            SELECT 1 FROM {DATASET_TABLE} ds
            WHERE ds.symbol = bt.symbol AND ds.market = 'futures'
        )
        """
    )


def downgrade() -> None:
    """移除回测记录市场字段（数据集 market 列由前一条迁移负责删除）。"""
    op.drop_column(BACKTEST_TABLE, "market")
