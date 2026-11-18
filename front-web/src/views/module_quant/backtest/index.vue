<!-- 量化-回测：选择数据集与策略执行回测，展示收益与成交明细 -->
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
        <el-form-item label="数据集">
          <el-select v-model="form.dataset_id" filterable placeholder="选择数据集" style="width: 300px">
            <el-option
              v-for="item in datasets"
              :key="item.id"
              :label="`${item.symbol} ${item.interval} · ${item.row_count}根 · ${item.name}`"
              :value="item.id"
            />
          </el-select>
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
      <div v-if="strategies.length === 0" class="text-xs text-orange-500">
        未发现策略。请在 user_data/strategies 下添加继承 Strategy 的 .py 文件。
      </div>
    </el-card>

    <el-card v-if="current?.result" shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">回测结果：{{ current.name }}</span>
          <span class="text-xs text-gray-500">{{ current.symbol }} · {{ current.interval }} · {{ current.strategy_name }}</span>
        </div>
      </template>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">最终净值</div>
          <div class="text-lg font-semibold">{{ current.result.final_equity.toFixed(2) }}</div>
        </div>
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">总收益率</div>
          <div class="text-lg font-semibold">{{ formatPercent(current.result.total_return) }}</div>
        </div>
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">最大回撤</div>
          <div class="text-lg font-semibold">{{ formatPercent(current.result.max_drawdown) }}</div>
        </div>
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">成交 / 胜率</div>
          <div class="text-lg font-semibold">{{ current.result.trade_count }} / {{ formatPercent(current.result.win_rate) }}</div>
        </div>
      </div>
      <div class="mb-4">
        <div class="text-xs text-gray-500 mb-1">净值曲线（{{ current.result.equity_curve.length }} 个点）</div>
        <EquityChart
          :points="current.result.equity_curve"
          :initial-cash="current.result.initial_cash"
          :height="240"
        />
      </div>
      <el-table :data="current.result.trades" border stripe size="small" max-height="320">
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
      <pre class="mt-3 p-2 bg-gray-50 border rounded text-xs overflow-auto max-h-40">{{ current.result.logs.join("\n") || "（策略无日志）" }}</pre>
    </el-card>

    <el-card v-else-if="current?.error" shadow="never">
      <div class="text-red-500">回测失败：{{ current.error }}</div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">历史回测</span>
          <el-button link type="primary" @click="fetchRecords">刷新</el-button>
        </div>
      </template>
      <el-table :data="records" v-loading="listLoading" border stripe size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="名称" min-width="200" />
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
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import EquityChart from "@/components/quant/EquityChart.vue";
import QuantBacktestAPI, {
  type QuantBacktestForm,
  type QuantBacktestTable,
  type QuantStrategyTable,
} from "@api/module_quant/backtest.ts";
import QuantDataAPI, { type QuantDatasetTable } from "@api/module_quant/data.ts";

const form = ref<QuantBacktestForm>({
  dataset_id: 0,
  strategy_name: "",
  initial_cash: 10000,
  fee_rate: 0.001,
  slippage: 0.0,
});
const datasets = ref<QuantDatasetTable[]>([]);
const strategies = ref<QuantStrategyTable[]>([]);
const records = ref<QuantBacktestTable[]>([]);
const current = ref<QuantBacktestTable | null>(null);
const running = ref(false);
const listLoading = ref(false);

/** 状态文案。 */
const statusText = (status: number): string => (status === 1 ? "成功" : status === 2 ? "失败" : "运行中");

/** 百分比格式化。 */
const formatPercent = (value: number): string => `${(value * 100).toFixed(2)}%`;

/** 成交方向中文。 */
const sideText = (side: string): string =>
  ({ buy: "买入", sell: "卖出", sellshort: "开空", buycover: "平空" })[side] ?? side;

/** 加载数据集与策略。 */
const fetchOptions = async () => {
  const [datasetRes, strategyRes] = await Promise.all([QuantDataAPI.list({ limit: 100 }), QuantBacktestAPI.strategies()]);
  datasets.value = datasetRes?.data?.data ?? [];
  strategies.value = strategyRes?.data?.data ?? [];
  if (!form.value.dataset_id) {
    const firstDataset = datasets.value.at(0);
    if (firstDataset) form.value.dataset_id = firstDataset.id;
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
    const { data: res } = await QuantBacktestAPI.list();
    records.value = res?.data ?? [];
  } finally {
    listLoading.value = false;
  }
};

/** 提交回测。 */
const handleRun = async () => {
  if (!form.value.dataset_id || !form.value.strategy_name) {
    ElMessage.warning("请选择数据集和策略");
    return;
  }
  running.value = true;
  try {
    const { data: res } = await QuantBacktestAPI.run(form.value);
    current.value = res?.data ?? null;
    ElMessage.success("回测完成");
    await fetchRecords();
  } finally {
    running.value = false;
  }
};

/** 查看历史回测详情。 */
const handleDetail = async (id: number) => {
  const { data: res } = await QuantBacktestAPI.detail(id);
  current.value = res?.data ?? null;
};

onMounted(() => {
  void fetchOptions();
  void fetchRecords();
});
</script>
