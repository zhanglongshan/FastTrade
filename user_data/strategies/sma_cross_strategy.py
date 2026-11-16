"""示例策略：SMA 金叉做多、死叉平多（事件模式）。"""

from quant_api import Strategy


class SmaCrossStrategy(Strategy):
    """5/20 周期均线交叉策略。

    金叉开多，死叉平多；如需做空，可在死叉分支调用 api.sellshort()。
    """

    name = "SMA均线交叉"
    timeframe = "1h"
    startup_candle_count = 20

    def on_bar(self, api, candles, index):
        """逐根 K 线判断均线交叉。"""
        fast = api.sma(5)
        slow = api.sma(20)
        if index < self.startup_candle_count:
            return
        values = [fast[index - 1], fast[index], slow[index - 1], slow[index]]
        if any(value is None for value in values):
            return

        fast_prev, fast_now, slow_prev, slow_now = values
        if fast_prev <= slow_prev and fast_now > slow_now:
            # 金叉：先平掉可能的空单，再开多
            api.buycover(reason="sma_golden_cross_cover")
            api.buy(ratio=0.95, reason="sma_golden_cross")
            api.log("SMA 金叉，开多")
        elif fast_prev >= slow_prev and fast_now < slow_now:
            # 死叉：平多；如需做空可取消注释下一行
            api.sell(reason="sma_death_cross")
            # api.sellshort(ratio=0.3, reason="sma_death_cross")
            api.log("SMA 死叉，平多")
