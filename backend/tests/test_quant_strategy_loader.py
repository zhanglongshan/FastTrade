"""策略自动发现单元测试。"""

from pathlib import Path

from app.modules.quant.strategy.loader import load_strategies, load_strategy


def test_load_strategies_from_directory(tmp_path: Path) -> None:
    """自动扫描目录，识别事件模式与信号模式策略。"""
    (tmp_path / "demo_strategy.py").write_text(
        """from quant_api import Strategy


class DemoStrategy(Strategy):
    \"\"\"演示策略\"\"\"

    name = \"演示策略\"
    timeframe = \"1h\"

    def on_bar(self, api, candles, index):
        if index == 1:
            api.buy()
""",
        encoding="utf-8",
    )
    (tmp_path / "_ignored.py").write_text("raise RuntimeError('不应加载')\n", encoding="utf-8")

    items = load_strategies(tmp_path)
    assert len(items) == 1
    assert items[0].name == "演示策略"
    assert items[0].mode == "event"
    assert "演示策略" in items[0].doc

    found = load_strategy("DemoStrategy", tmp_path)
    assert found.file.endswith("demo_strategy.py")
