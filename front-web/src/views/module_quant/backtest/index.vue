<!-- 量化-回测：按市场筛选数据集，支持多选/全选批量回测，分标签展示结果 -->
<template>
  <!-- 卡片统一 shrink-0：避免布局 flex 链压扁卡片导致内容裁切 -->
  <div class="flex flex-col gap-4 p-4 [&>*]:shrink-0">
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">新建回测</span>
          <span class="text-xs text-gray-500">策略来自项目根目录 user_data/strategies</span>
        </div>
      </template>
      <el-form :model="form" label-width="100px" inline>
        <el-form-item label="市场">
          <el-radio-group v-model="market" @change="handleMarketChange">
            <el-radio-button value="spot">现货</el-radio-button>
            <el-radio-button value="futures">合约</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="数据集">
          <div class="flex items-center gap-2">
            <el-select
              v-model="selectedIds"
              multiple
              filterable
              collapse-tags
              collapse-tags-tooltip
              placeholder="可多选数据集"
              style="width: 420px"
            >
              <el-option
                v-for="item in datasetOptions"
                :key="item.id"
                :label="`${item.symbol} · ${item.interval} · ${marketText(item.market)} (${item.row_count}根)`"
                :value="item.id"
              />
            </el-select>
            <el-button size="small" :disabled="!datasetOptions.length" @click="selectAll">全选</el-button>
            <el-button size="small" :disabled="!selectedIds.length" @click="clearSelection">清空</el-button>
          </div>
        </el-form-item>
        <el-form-item label="策略">
          <el-select v-model="form.strategy_name" filterable placeholder="选择策略" style="width: 240px">
            <el-option v-for="item in strategies" :key="item.name" :label="`${item.name} (${item.mode})`" :value="item.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="初始资金">
          <el-input-number v-model="form.initial_cash" :min="1" :step="1000" style="width: 150px" />
        </el-form-item>
        <el-form-item label="手续费率">
          <el-input-number v-model="form.fee_rate" :min="0" :max="0.1" :step="0.0001" :precision="4" style="width: 150px" />
        </el-form-item>
        <el-form-item label="滑点">
          <el-input-number v-model="form.slippage" :min="0" :max="0.1" :step="0.0001" :precision="4" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="running" @click="handleRun">开始回测</el-button>
        </el-form-item>
      </el-form>
      <div class="text-xs text-gray-500">
        当前市场可选 {{ datasetOptions.length }} 个数据集，已选 {{ selectedIds.length }} 个；每个数据集独立回测并生成一条记录。
        现货禁止开空，合约允许开空。
      </div>
      <div v-if="strategies.length === 0" class="text-xs text-orange-500">
        未发现策略。请在 user_data/strategies 下添加继承 Strategy 的 .py 文件。
      </div>
    </el-card>

    <el-card v-if="results.length" shadow="never" v-loading="running">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">回测结果（{{ results.length }} 个数据集）</span>
          <span class="text-xs text-gray-500">点击标签切换查看</span>
        </div>
      </template>
      <el-tabs v-model="activeResultId" type="card">
        <el-tab-pane
          v-for="item in results"
          :key="item.id"
          :label="`${item.symbol} ${item.interval} · ${statusText(item.status)}`"
          :name="String(item.id)"
        />
      </el-tabs>

      <template v-if="activeMetrics">
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
          <div class="border rounded p-3">
            <div class="text-xs text-gray-500">最终净值</div>
            <div class="text-lg font-semibold">{{ activeMetrics.final_equity.toFixed(2) }}</div>
          </div>
          <div class="border rounded p-3">
            <div class="text-xs text-gray-500">总收益率</div>
            <div class="text-lg font-semibold">{{ formatPercent(activeMetrics.total_return) }}</div>
          </div>
          <div class="border rounded p-3">
            <div class="text-xs text-gray-500">最大回撤</div>
            <div class="text-lg font-semibold">{{ formatPercent(activeMetrics.max_drawdown) }}</div>
          </div>
          <div class="border rounded p-3">
            <div class="text-xs text-gray-500">成交 / 胜率</div>
            <div class="text-lg font-semibold">
              {{ activeMetrics.trade_count }} / {{ formatPercent(activeMetrics.win_rate) }}
            </div>
          </div>
        </div>
        <div class="mb-4">
          <div class="text-xs text-gray-500 mb-1">净值曲线（{{ activeEquity.length }} 个点）</div>
          <EquityChart :points="activeEquity" :initial-cash="activeInitialCash" :height="240" />
        </div>
        <el-table :data="activeTrades" border stripe size="small" max-height="320">
          <el-table-column prop="date" label="成交时间" min-width="180" />
          <el-table-column label="方向" width="110">
            <template #default="{ row }">{{ sideText(row.side) }}</template>
          </el-table-column>
          <el-table-column prop="qty" label="数量" width="130" align="right" />
          <el-table-column prop="price" label="成交价" width="120" align="right" />
          <el-table-column prop="fee" label="手续费" width="110" align="right" />
          <el-table-column label="盈亏" width="110" align="right">
            <template #default="{ row }">
              <span
                v-if="row.pnl !== null && row.pnl !== undefined"
                :style="{ color: row.pnl >= 0 ? '#ef5350' : '#26a69a' }"
              >
                {{ row.pnl.toFixed(2) }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" min-width="160" />
        </el-table>
        <pre class="mt-3 p-2 bg-gray-50 border rounded text-xs overflow-auto max-h-40">{{ activeLogs.join("\n") || "（策略无日志）" }}</pre>
      </template>
      <div v-else-if="activeResult?.error" class="text-red-500">回测失败：{{ activeResult.error }}</div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">历史回测</span>
          <el-button link type="primary" @click="fetchRecords">刷新</el-button>
        </div>
      </template>
      <el-table :data="pagedRecords" v-loading="listLoading" border stripe size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="名称" min-width="200" />
        <el-table-column label="市场" width="90">
          <template #default="{ row }">{{ marketText(row.market) }}</template>
        </el-table-column>
        <el-table-column prop="strategy_name" label="策略" width="160" />
        <el-table-column prop="symbol" label="交易对" width="120" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <span :class="row.status === 1 ? 'text-green-600' : row.status === 2 ? 'text-red-500' : 'text-orange-500'">
              {{ statusText(row.status) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="收益率" width="120">
          <template #default="{ row }">
            {{ row.result ? formatPercent(row.result.total_return) : "-" }}
          </template>
        </el-table-column>
        <el-table-column prop="finished_time" label="完成时间" width="170" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="handleDetail(row.id)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="mt-3 flex justify-end">
        <el-pagination
          v-model:current-page="recordPage"
          v-model:page-size="recordPageSize"
          :page-sizes="[50, 100, 200]"
          :total="records.length"
          layout="total, sizes, prev, pager, next"
          background
          small
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import EquityChart from "@/components/quant/EquityChart.vue";
import QuantBacktestAPI, {
  type QuantBacktestTable,
  type QuantStrategyTable,
} from "@api/module_quant/backtest.ts";
import QuantDataAPI, { type QuantDatasetTable } from "@api/module_quant/data.ts";

const market = ref<"spot" | "futures">("spot");
const datasets = ref<QuantDatasetTable[]>([]);
const selectedIds = ref<number[]>([]);
const strategies = ref<QuantStrategyTable[]>([]);
const records = ref<QuantBacktestTable[]>([]);
const results = ref<QuantBacktestTable[]>([]);
const activeResultId = ref("");
const form = ref({
  strategy_name: "",
  initial_cash: 10000,
  fee_rate: 0.001,
  slippage: 0.0,
});
const running = ref(false);
const listLoading = ref(false);
const recordPage = ref(1);
const recordPageSize = ref(50);

/** 当前市场的数据集选项。 */
const datasetOptions = computed(() => datasets.value.filter((item) => (item.market ?? "spot") === market.value));

/** 历史记录前端分页，避免一次渲染过多行。 */
const pagedRecords = computed(() => {
  const start = (recordPage.value - 1) * recordPageSize.value;
  return records.value.slice(start, start + recordPageSize.value);
});

/** 当前查看的回测记录。 */
const activeResult = computed(
  () => results.value.find((item) => String(item.id) === activeResultId.value) ?? results.value[0] ?? null,
);
const activeMetrics = computed(() => activeResult.value?.result ?? null);
const activeEquity = computed(() => activeMetrics.value?.equity_curve ?? []);
const activeTrades = computed(() => activeMetrics.value?.trades ?? []);
const activeLogs = computed(() => activeMetrics.value?.logs ?? []);
const activeInitialCash = computed(() => activeMetrics.value?.initial_cash ?? form.value.initial_cash);

/** 市场文案。 */
const marketText = (value?: string): string => (value === "futures" ? "合约" : "现货");

/** 状态文案。 */
const statusText = (status: number): string => (status === 1 ? "成功" : status === 2 ? "失败" : "运行中");

/** 百分比格式化。 */
const formatPercent = (value: number): string => `${(value * 100).toFixed(2)}%`;

/** 成交方向中文。 */
const sideText = (side: string): string =>
  ({ buy: "买入", sell: "卖出", sellshort: "开空", buycover: "平空" })[side] ?? side;

/** 切换市场后重置选择，默认选中该市场第一个数据集。 */
const handleMarketChange = () => {
  selectedIds.value = datasetOptions.value.length ? [datasetOptions.value[0]!.id] : [];
};

/** 全选 / 清空当前市场的数据集。 */
const selectAll = () => {
  selectedIds.value = datasetOptions.value.map((item) => item.id);
};
const clearSelection = () => {
  selectedIds.value = [];
};

/** 加载数据集与策略。 */
const fetchOptions = async () => {
  const [datasetRes, strategyRes] = await Promise.all([
    QuantDataAPI.list({ limit: 500 }),
    QuantBacktestAPI.strategies(),
  ]);
  datasets.value = datasetRes?.data?.data ?? [];
  strategies.value = strategyRes?.data?.data ?? [];
  if (!selectedIds.value.length && datasetOptions.value.length) {
    selectedIds.value = [datasetOptions.value[0]!.id];
  }
  if (!form.value.strategy_name) {
    const firstStrategy = strategies.value.at(0);
    if (firstStrategy) form.value.strategy_name = firstStrategy.name;
  }
};

/** 加载历史回测。 */
const fetchRecords = async () => {
  listLoading.value = true;
  try {
    const { data: res } = await QuantBacktestAPI.list(200);
    records.value = res?.data ?? [];
    recordPage.value = 1;
  } finally {
    listLoading.value = false;
  }
};

/** 批量提交回测：每个选中的数据集生成一条记录。 */
const handleRun = async () => {
  if (!selectedIds.value.length || !form.value.strategy_name) {
    ElMessage.warning("请选择数据集和策略");
    return;
  }
  running.value = true;
  try {
    const { data: res } = await QuantBacktestAPI.run({
      dataset_ids: [...selectedIds.value],
      strategy_name: form.value.strategy_name,
      initial_cash: form.value.initial_cash,
      fee_rate: form.value.fee_rate,
      slippage: form.value.slippage,
    });
    results.value = res?.data ?? [];
    activeResultId.value = String(results.value[0]?.id ?? "");
    ElMessage.success(res?.msg ?? `回测完成：${results.value.length} 个数据集`);
    await fetchRecords();
  } finally {
    running.value = false;
  }
};

/** 查看历史回测详情。 */
const handleDetail = async (id: number) => {
  const { data: res } = await QuantBacktestAPI.detail(id);
  const detail = res?.data;
  if (!detail) return;
  results.value = [detail];
  activeResultId.value = String(detail.id);
};

onMounted(() => {
  void fetchOptions();
  void fetchRecords();
});
</script>
