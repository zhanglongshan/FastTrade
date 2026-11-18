import { request } from "@utils";

/** 量化策略与回测接口。 */
const API_PATH = "/quant";

export const QuantBacktestAPI = {
  /** 查询自动发现的策略 */
  strategies() {
    return request<ApiResponse<QuantStrategyTable[]>>({
      url: `${API_PATH}/strategies`,
      method: "get",
    });
  },

  /** 查询回测记录 */
  list(limit = 100) {
    return request<ApiResponse<QuantBacktestTable[]>>({
      url: `${API_PATH}/backtest/list`,
      method: "get",
      params: { limit },
    });
  },

  /** 执行回测（支持多选数据集，返回每条数据集的记录） */
  run(body: QuantBacktestForm) {
    return request<ApiResponse<QuantBacktestTable[]>>({
      url: `${API_PATH}/backtest/run`,
      method: "post",
      data: body,
    });
  },

  /** 查询回测详情 */
  detail(id: number) {
    return request<ApiResponse<QuantBacktestTable>>({
      url: `${API_PATH}/backtest/detail/${id}`,
      method: "get",
    });
  },
};

export interface QuantStrategyTable {
  name: string;
  file: string;
  mode: "event" | "signal" | string;
  timeframe: string;
  doc: string;
}

export interface QuantBacktestForm {
  /** 多选数据集 ID；旧版单数据集接口仍兼容 */
  dataset_ids: number[];
  dataset_id?: number;
  strategy_name: string;
  name?: string;
  initial_cash?: number;
  fee_rate?: number;
  slippage?: number;
  description?: string;
}

export interface QuantTradeRecord {
  date: string;
  side: string;
  qty: number;
  price: number;
  fee: number;
  pnl: number | null;
  reason: string;
  signal_price: number;
}

export interface QuantEquityPoint {
  date: string;
  equity: number;
  cash: number;
  long_qty: number;
  short_qty: number;
}

export interface QuantBacktestResult {
  strategy: string;
  symbol: string;
  interval: string;
  initial_cash: number;
  final_equity: number;
  total_return: number;
  annualized_return: number;
  max_drawdown: number;
  trade_count: number;
  closed_trade_count: number;
  win_rate: number;
  trades: QuantTradeRecord[];
  equity_curve: QuantEquityPoint[];
  logs: string[];
  candle_count: number;
  warmup_candles: number;
}

export interface QuantBacktestTable {
  id: number;
  name: string;
  strategy_name: string;
  symbol: string;
  interval: string;
  market: "spot" | "futures";
  initial_cash: number;
  fee_rate: number;
  status: number;
  result: QuantBacktestResult | null;
  error: string | null;
  started_time?: string | null;
  finished_time?: string | null;
  created_time?: string | null;
}

export default QuantBacktestAPI;
