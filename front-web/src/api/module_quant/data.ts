import { request } from "@utils";

/** 量化数据接口。 */
const API_PATH = "/quant/data";

export const QuantDataAPI = {
  /** 查询已登记的数据集 */
  list(query: QuantDatasetQuery = {}) {
    return request<ApiResponse<QuantDatasetTable[]>>({
      url: `${API_PATH}/list`,
      method: "get",
      params: query,
    });
  },

  /** 查询可导入的本地 CSV 文件 */
  files() {
    return request<ApiResponse<QuantLocalFile[]>>({
      url: `${API_PATH}/files`,
      method: "get",
    });
  },

  /** 查询数据集 K 线（图表用） */
  candles(datasetId: number, params: QuantCandlesQuery = {}) {
    return request<ApiResponse<QuantCandlesResult>>({
      url: `${API_PATH}/candles/${datasetId}`,
      method: "get",
      params,
    });
  },

  /** 下载币安 K 线 */
  download(body: QuantDownloadForm) {
    return request<ApiResponse<QuantDownloadResult>>({
      url: `${API_PATH}/download`,
      method: "post",
      data: body,
    });
  },

  /** 导入本地 CSV */
  importCsv(body: QuantImportForm) {
    return request<ApiResponse<QuantImportResult>>({
      url: `${API_PATH}/import`,
      method: "post",
      data: body,
    });
  },
};

export interface QuantDatasetQuery {
  symbol?: string;
  interval?: string;
  market?: "spot" | "futures";
  limit?: number;
}

export interface QuantDatasetTable {
  id: number;
  name: string;
  symbol: string;
  interval: string;
  source: string;
  market: "spot" | "futures";
  file_path: string;
  row_count: number;
  start_time?: string | null;
  end_time?: string | null;
  status: number;
  description?: string | null;
  created_time?: string | null;
}

export interface QuantCandlesQuery {
  limit?: number;
  tail?: boolean;
}

export interface QuantCandle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface QuantCandlesResult {
  dataset_id: number;
  name: string;
  symbol: string;
  interval: string;
  total: number;
  candles: QuantCandle[];
}

export interface QuantLocalFile {
  path: string;
  name: string;
  size: number;
  modified_time: string;
  market: "spot" | "futures";
}

export interface QuantDownloadForm {
  symbol: string;
  interval: string;
  start_time?: string;
  end_time?: string;
  days?: number;
  source?: "api" | "mirror" | "archive";
  market?: "spot" | "futures";
  erase?: boolean;
  fill_recent?: boolean;
  limit?: number;
  description?: string;
}

export interface QuantImportForm {
  file_path: string;
  symbol: string;
  interval: string;
  source?: string;
  market?: "spot" | "futures";
  description?: string;
}

export interface QuantDownloadResult {
  name: string;
  symbol: string;
  interval: string;
  source: string;
  market: string;
  file_path: string;
  downloaded: number;
  row_count: number;
  dataset_id: number | null;
  requests?: number;
  archive_files?: number;
  warnings?: string[];
}

export type QuantImportResult = QuantDownloadResult & { imported: number };

export const QUANT_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"];

export default QuantDataAPI;
