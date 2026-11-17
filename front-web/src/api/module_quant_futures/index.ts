import { request } from "@utils";

/** 期货（CTP）量化接口。 */
const API_PATH = "/quant-futures";

export const QuantFuturesAPI = {
  datasets(params: MarketDatasetQuery = {}) {
    return request<ApiResponse<MarketDataset[]>>({ url: `${API_PATH}/datasets`, method: "get", params });
  },
  files() {
    return request<ApiResponse<MarketLocalFile[]>>({ url: `${API_PATH}/files`, method: "get" });
  },
  importCsv(body: MarketImportForm) {
    return request<ApiResponse<MarketImportResult>>({ url: `${API_PATH}/import`, method: "post", data: body });
  },
  candles(datasetId: number, params: { limit?: number } = {}) {
    return request<ApiResponse<MarketCandles>>({ url: `${API_PATH}/candles/${datasetId}`, method: "get", params });
  },
  runBacktest(body: MarketBacktestForm) {
    return request<ApiResponse<MarketBacktest>>({ url: `${API_PATH}/backtest`, method: "post", data: body });
  },
  backtests(params: { limit?: number } = {}) {
    return request<ApiResponse<MarketBacktest[]>>({ url: `${API_PATH}/backtests`, method: "get", params });
  },
  createLive(body: MarketLiveForm) {
    return request<ApiResponse<MarketLive>>({ url: `${API_PATH}/live`, method: "post", data: body });
  },
  lives(params: { limit?: number } = {}) {
    return request<ApiResponse<MarketLive[]>>({ url: `${API_PATH}/live`, method: "get", params });
  },
  tick(strategyId: number, body: MarketTickForm) {
    return request<ApiResponse<MarketLive>>({ url: `${API_PATH}/live/${strategyId}/tick`, method: "post", data: body });
  },
};

export interface MarketDatasetQuery { limit?: number }
export interface MarketLocalFile { path: string; name: string; size: number; modified_time?: string | null }
export interface MarketDataset {
  id: number; name: string; symbol: string; interval: string; source: string;
  file_path: string; row_count: number; start_time?: string | null;
  end_time?: string | null; status: number; description?: string | null;
}
export interface MarketCandle {
  date: string; open: number; high: number; low: number; close: number; volume: number;
}
export interface MarketCandles {
  dataset_id: number; name: string; symbol: string; interval: string;
  total: number; candles: MarketCandle[];
}
export interface MarketImportForm {
  file_path: string; symbol: string; interval: string; source?: string; description?: string;
}
export interface MarketImportResult { id: number; name: string; row_count: number; file_path: string }
export interface MarketBacktestForm {
  dataset_id: number; strategy_name: string; name?: string;
  initial_cash?: number; fee_rate?: number; slippage?: number; description?: string;
}
export interface MarketBacktest {
  id: number; name: string; strategy_name: string; symbol: string; interval: string;
  initial_cash: number; fee_rate: number; status: number;
  result?: Record<string, unknown> | null; error?: string | null;
  started_time?: string | null; finished_time?: string | null; created_time?: string | null;
}
export interface MarketLiveForm {
  name: string; mode: "paper" | "live"; strategy_name: string; symbol: string; interval: string;
  initial_cash?: number; fee_rate?: number; slippage?: number;
  dry_run?: boolean; allow_short?: boolean; broker_config?: Record<string, unknown>; description?: string;
}
export interface MarketLive {
  id: number; name: string; mode: string; strategy_name: string; symbol: string; interval: string;
  initial_cash: number; fee_rate: number; slippage: number; dry_run: boolean; allow_short: boolean;
  status: number; account?: Record<string, unknown> | null; last_result?: Record<string, unknown> | null;
  last_error?: string | null; last_tick_time?: string | null; description?: string | null;
}
export interface MarketTickForm { price?: number; candles_file?: string; reason?: string }

export default QuantFuturesAPI;
