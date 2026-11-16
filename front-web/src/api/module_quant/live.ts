import { request } from "@utils";

/** 量化实时策略接口：模拟盘(paper)与实盘(live)共用。 */
const API_PATH = "/quant/live";

export const QuantLiveAPI = {
  /** 查询实例列表，可按模式过滤 */
  list(mode?: "paper" | "live", limit = 100) {
    return request<ApiResponse<QuantLiveStrategyTable[]>>({
      url: `${API_PATH}/list`,
      method: "get",
      params: { mode, limit },
    });
  },

  /** 查询实例详情 */
  detail(id: number) {
    return request<ApiResponse<QuantLiveStrategyTable>>({
      url: `${API_PATH}/detail/${id}`,
      method: "get",
    });
  },

  /** 创建实例 */
  create(body: QuantLiveStrategyForm) {
    return request<ApiResponse<QuantLiveStrategyTable>>({
      url: `${API_PATH}/create`,
      method: "post",
      data: body,
    });
  },

  /** 更新实例 */
  update(id: number, body: QuantLiveStrategyUpdateForm) {
    return request<ApiResponse<QuantLiveStrategyTable>>({
      url: `${API_PATH}/update/${id}`,
      method: "put",
      data: body,
    });
  },

  /** 启动实例 */
  start(id: number) {
    return request<ApiResponse<Record<string, unknown>>>({
      url: `${API_PATH}/start/${id}`,
      method: "post",
    });
  },

  /** 停止实例 */
  stop(id: number) {
    return request<ApiResponse<Record<string, unknown>>>({
      url: `${API_PATH}/stop/${id}`,
      method: "post",
    });
  },

  /** 手动运行一次 tick */
  tick(id: number) {
    return request<ApiResponse<QuantTickResult>>({
      url: `${API_PATH}/tick/${id}`,
      method: "post",
    });
  },

  /** 查询成交流水 */
  trades(id: number, limit = 200) {
    return request<ApiResponse<QuantLiveTradeRecord[]>>({
      url: `${API_PATH}/trades/${id}`,
      method: "get",
      params: { limit },
    });
  },

  /** 删除实例 */
  remove(id: number) {
    return request<ApiResponse<Record<string, unknown>>>({
      url: `${API_PATH}/delete/${id}`,
      method: "delete",
    });
  },
};

export interface QuantAccountSnapshot {
  initial_cash: number;
  cash: number;
  equity: number;
  long_qty: number;
  short_qty: number;
  long_cost: number;
  short_proceeds: number;
  realized_pnl: number;
  total_fee: number;
}

export interface QuantTickResult {
  mode: string;
  symbol: string;
  interval: string;
  strategy: string;
  price: number;
  equity: number;
  cash: number;
  position: Record<string, number>;
  fills: Array<{
    side: string;
    qty: number;
    price: number;
    fee: number;
    pnl: number | null;
    reason: string;
  }>;
  logs: string[];
  rejected: string[];
  error: string | null;
  tick_time: string;
}

export interface QuantLiveStrategyTable {
  id: number;
  name: string;
  mode: "paper" | "live";
  strategy_name: string;
  symbol: string;
  interval: string;
  initial_cash: number;
  fee_rate: number;
  slippage: number;
  dry_run: boolean;
  allow_short: boolean;
  status: number;
  account: QuantAccountSnapshot | null;
  has_api_key: boolean;
  last_result: QuantTickResult | null;
  last_error: string | null;
  last_tick_time: string | null;
  description: string | null;
  created_time: string | null;
}

export interface QuantLiveStrategyForm {
  name: string;
  mode: "paper" | "live";
  strategy_name: string;
  symbol: string;
  interval: string;
  initial_cash?: number;
  fee_rate?: number;
  slippage?: number;
  dry_run?: boolean;
  allow_short?: boolean;
  api_key?: string | null;
  api_secret?: string | null;
  description?: string | null;
}

export interface QuantLiveStrategyUpdateForm {
  name?: string;
  fee_rate?: number;
  slippage?: number;
  dry_run?: boolean;
  allow_short?: boolean;
  api_key?: string;
  api_secret?: string;
  description?: string;
}

export interface QuantLiveTradeRecord {
  id: number;
  strategy_id: number;
  mode: string;
  symbol: string;
  side: string;
  qty: number;
  price: number;
  fee: number;
  pnl: number | null;
  reason: string | null;
  trade_time: string | null;
}

export default QuantLiveAPI;
