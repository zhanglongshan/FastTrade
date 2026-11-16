"""示例策略：RSI 超卖开多、超买平多（信号模式，参考 freqtrade）。"""

from quant_api import Strategy, rsi


class RsiSignalStrategy(Strategy):
    """RSI(14) 阈值策略。

    通过 populate_entry_trend / populate_exit_trend 返回信号列表，
    引擎只在当前下标位置读取信号，避免偷看未来数据。
    """

    name = "RSI超卖反弹"
    timeframe = "1h"
    startup_candle_count = 14

    def populate_entry_trend(self, candles, metadata):
        """RSI 低于 30 时给出开多信号。"""
        values = rsi([candle.close for candle in candles], 14)
        signals = [0] * len(candles)
        if values and values[-1] is not None and values[-1] < 30:
            signals[-1] = 1
        return signals

    def populate_exit_trend(self, candles, metadata):
        """RSI 高于 70 时给出平多信号。"""
        values = rsi([candle.close for candle in candles], 14)
        signals = [0] * len(candles)
        if values and values[-1] is not None and values[-1] > 70:
            signals[-1] = 1
        return signals
