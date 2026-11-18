<!-- A股/期货量化共享工作区：数据、图表、回测、模拟盘、实盘 -->
<template>
  <!-- 卡片统一 shrink-0：避免布局 flex 链压扁卡片导致图表/表格裁切 -->
  <div class="flex flex-col gap-4 p-4 [&>*]:shrink-0">
    <el-alert
      :title="`${label} · 柜台适配器：${broker}`"
      type="info"
      :closable="false"
      show-icon
      description="默认离线 CSV / 模拟撮合；接入中泰 XTP 或 CTP SDK 后可切换为真实报单。"
    />

    <el-card shadow="never">
      <el-tabs v-model="activeTab">
        <!-- 数据导入 -->
        <el-tab-pane label="数据" name="data">
          <el-form :model="importForm" label-width="90px" inline>
            <el-form-item label="本地文件">
              <el-select v-model="importForm.file_path" filterable placeholder="选择 CSV" style="width: 320px">
                <el-option v-for="f in localFiles" :key="f.path" :label="f.name" :value="f.path" />
              </el-select>
            </el-form-item>
            <el-form-item label="代码">
              <el-input v-model="importForm.symbol" :placeholder="symbolExample" style="width: 160px" />
            </el-form-item>
            <el-form-item label="周期">
              <el-input v-model="importForm.interval" style="width: 90px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="busy" @click="handleImport">导入</el-button>
              <el-button @click="fetchDatasets">刷新</el-button>
            </el-form-item>
          </el-form>
          <div class="text-xs text-gray-500">CSV 需包含列：date, open, high, low, close, volume；文件需位于 user_data/data 下。</div>
          <el-table :data="datasets" border stripe size="small" class="mt-3" max-height="360">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="name" label="数据集" min-width="180" />
            <el-table-column prop="symbol" label="代码" width="140" />
            <el-table-column prop="interval" label="周期" width="80" />
            <el-table-column prop="row_count" label="K线数" width="100" />
            <el-table-column prop="file_path" label="文件" min-width="200" show-overflow-tooltip />
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button link type="primary" @click="openChart(row.id)">图表</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 图表 -->
        <el-tab-pane label="图表" name="chart">
          <el-form label-width="90px" inline>
            <el-form-item label="数据集">
              <el-select
                v-model="chartDatasetId"
                filterable
                placeholder="选择数据集"
                style="width: 300px"
                @change="handleChartDatasetChange"
              >
                <el-option v-for="d in datasets" :key="d.id" :label="`${d.name} (${d.row_count} 根)`" :value="d.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="显示条数">
              <el-select v-model="chartLimit" style="width: 120px" @change="loadCandles">
                <el-option v-for="n in chartLimits" :key="n" :label="`最后 ${n} 根`" :value="n" />
              </el-select>
            </el-form-item>
            <el-form-item label="叠加">
              <el-checkbox v-model="chartShowMa">均线</el-checkbox>
              <el-checkbox v-model="chartShowVolume">成交量</el-checkbox>
            </el-form-item>
            <el-form-item label="缩放">
              <el-button size="small" @click="chartRef?.zoomToLatest()">最近</el-button>
              <el-button size="small" @click="chartRef?.zoomToAll()">全览</el-button>
            </el-form-item>
          </el-form>
          <KlineChart
            ref="chartRef"
            :candles="chartCandles"
            :show-ma="chartShowMa"
            :show-volume="chartShowVolume"
            :height="420"
          />
          <el-table :data="chartRows" border stripe size="small" max-height="320" class="mt-3">
            <el-table-column prop="date" label="时间" min-width="180" />
            <el-table-column prop="open" label="开" align="right" />
            <el-table-column prop="high" label="高" align="right" />
            <el-table-column prop="low" label="低" align="right" />
            <el-table-column prop="close" label="收" align="right" />
            <el-table-column label="涨跌幅" width="100" align="right">
              <template #default="{ row }">
                <span :style="{ color: row.change >= 0 ? '#ef5350' : '#26a69a' }">
                  {{ row.change >= 0 ? "+" : "" }}{{ row.change.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="volume" label="量" align="right" />
          </el-table>
        </el-tab-pane>

        <!-- 回测 -->
        <el-tab-pane label="回测" name="backtest">
          <el-form :model="backtestForm" label-width="90px" inline>
            <el-form-item label="数据集">
              <el-select v-model="backtestForm.dataset_id" style="width: 220px">
                <el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="策略">
              <el-input v-model="backtestForm.strategy_name" placeholder="SmaCrossStrategy" style="width: 180px" />
            </el-form-item>
            <el-form-item label="初始资金">
              <el-input-number v-model="backtestForm.initial_cash" :min="1" style="width: 140px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="busy" @click="handleBacktest">运行回测</el-button>
            </el-form-item>
          </el-form>
          <el-table :data="backtests" border stripe size="small" max-height="360">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="name" label="名称" min-width="180" />
            <el-table-column prop="strategy_name" label="策略" width="160" />
            <el-table-column prop="status" label="状态" width="80" />
            <el-table-column label="收益率" width="120">
              <template #default="{ row }">
                {{ row.result?.total_return !== undefined ? (row.result.total_return * 100).toFixed(2) + '%' : '-' }}
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 模拟盘 / 实盘 -->
        <el-tab-pane :label="modeLabel" name="live">
          <el-form :model="liveForm" label-width="90px" inline>
            <el-form-item label="名称">
              <el-input v-model="liveForm.name" style="width: 160px" />
            </el-form-item>
            <el-form-item label="模式">
              <el-select v-model="liveForm.mode" style="width: 110px">
                <el-option label="模拟盘" value="paper" />
                <el-option label="实盘" value="live" />
              </el-select>
            </el-form-item>
            <el-form-item label="策略">
              <el-input v-model="liveForm.strategy_name" style="width: 160px" />
            </el-form-item>
            <el-form-item label="代码">
              <el-input v-model="liveForm.symbol" :placeholder="symbolExample" style="width: 150px" />
            </el-form-item>
            <el-form-item label="周期">
              <el-input v-model="liveForm.interval" style="width: 80px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="busy" @click="handleCreateLive">创建</el-button>
            </el-form-item>
          </el-form>
          <el-table :data="lives" border stripe size="small" max-height="360">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="name" label="名称" min-width="140" />
            <el-table-column prop="mode" label="模式" width="90" />
            <el-table-column prop="symbol" label="代码" width="130" />
            <el-table-column prop="status" label="状态" width="80" />
            <el-table-column label="操作" width="140">
              <template #default="{ row }">
                <el-button link type="primary" @click="handleTick(row as MarketLive)">运行一次</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="text-xs text-gray-500 mt-2">
            运行 tick 默认使用柜台适配器取价（未配置 prices 时会提示）；可导入数据集后用 price 参数驱动。
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRoute } from "vue-router";
import KlineChart from "@/components/quant/KlineChart.vue";
import QuantAStockAPI from "@api/module_quant_astock/index.ts";
import QuantFuturesAPI from "@api/module_quant_futures/index.ts";
import type {
  MarketDataset, MarketCandle, MarketBacktest, MarketLive, MarketLocalFile,
} from "@api/module_quant_astock/index.ts";

const props = defineProps<{ market: "astock" | "futures"; initialTab?: string }>();

const api = computed(() => (props.market === "astock" ? QuantAStockAPI : QuantFuturesAPI));
const label = computed(() => (props.market === "astock" ? "A股（中泰XTP）" : "期货（CTP）"));
const broker = computed(() => (props.market === "astock" ? "XTP" : "CTP"));
const symbolExample = computed(() => (props.market === "astock" ? "600519.SH" : "rb2410.SHFE"));

const route = useRoute();
const tabFromRoute = computed(() => {
  const last = route.path.split("/").filter(Boolean).at(-1);
  return ["data", "chart", "backtest", "paper", "live"].includes(last ?? "") ? last! : (props.initialTab ?? "data");
});
const activeTab = ref("data");
const modeLabel = computed(() => (activeTab.value === "paper" ? "模拟盘" : "实盘"));

const busy = ref(false);
const datasets = ref<MarketDataset[]>([]);
const localFiles = ref<MarketLocalFile[]>([]);
const backtests = ref<MarketBacktest[]>([]);
const lives = ref<MarketLive[]>([]);
const chartDatasetId = ref<number | null>(null);
const chartCandles = ref<MarketCandle[]>([]);
const chartLimit = ref(500);
const chartLimits = [200, 500, 1000, 2000];
const chartShowMa = ref(true);
const chartShowVolume = ref(true);
const chartRef = ref<{ zoomToLatest: () => void; zoomToAll: () => void } | null>(null);

/** 图表明细行：附加相对前收盘的涨跌幅。 */
const chartRows = computed(() => {
  const candles = chartCandles.value;
  return candles.map((item, index) => {
    const prevClose = index > 0 ? (candles[index - 1]?.close ?? item.open) : item.open;
    const change = prevClose ? ((item.close - prevClose) / prevClose) * 100 : 0;
    return { ...item, change };
  });
});

const importForm = ref({ file_path: "", symbol: "", interval: "1d", source: "local" });
const backtestForm = ref({ dataset_id: 0 as number, strategy_name: "SmaCrossStrategy", initial_cash: 10000 });
const liveForm = ref({
  name: "", mode: "paper" as "paper" | "live", strategy_name: "SmaCrossStrategy",
  symbol: "", interval: "1d",
});

const fetchFiles = async () => {
  const { data: res } = await api.value.files();
  localFiles.value = res?.data ?? [];
  if (!importForm.value.file_path && localFiles.value.length) {
    importForm.value.file_path = localFiles.value[0]!.path;
  }
};

const fetchDatasets = async () => {
  const { data: res } = await api.value.datasets({ limit: 200 });
  datasets.value = res?.data ?? [];
  if (!backtestForm.value.dataset_id && datasets.value.length) {
    backtestForm.value.dataset_id = datasets.value[0]!.id;
  }
};

const handleImport = async () => {
  if (!importForm.value.file_path || !importForm.value.symbol) {
    ElMessage.warning("请选择文件并填写代码");
    return;
  }
  busy.value = true;
  try {
    await api.value.importCsv(importForm.value);
    ElMessage.success("导入完成");
    await fetchDatasets();
  } finally {
    busy.value = false;
  }
};

const loadCandles = async () => {
  if (!chartDatasetId.value) return;
  busy.value = true;
  try {
    const { data: res } = await api.value.candles(chartDatasetId.value, { limit: chartLimit.value });
    chartCandles.value = res?.data?.candles ?? [];
  } finally {
    busy.value = false;
  }
};

/** 切换图表数据集。 */
const handleChartDatasetChange = () => {
  void loadCandles();
};

const openChart = (id: number) => {
  chartDatasetId.value = id;
  activeTab.value = "chart";
  void loadCandles();
};

const fetchBacktests = async () => {
  const { data: res } = await api.value.backtests({ limit: 100 });
  backtests.value = res?.data ?? [];
};

const handleBacktest = async () => {
  busy.value = true;
  try {
    await api.value.runBacktest(backtestForm.value);
    ElMessage.success("回测完成");
    await fetchBacktests();
  } finally {
    busy.value = false;
  }
};

const fetchLives = async () => {
  const { data: res } = await api.value.lives({ limit: 100 });
  lives.value = res?.data ?? [];
};

const handleCreateLive = async () => {
  busy.value = true;
  try {
    await api.value.createLive(liveForm.value);
    ElMessage.success("实例已创建");
    await fetchLives();
  } finally {
    busy.value = false;
  }
};

const handleTick = async (row: MarketLive) => {
  busy.value = true;
  try {
    const { data: res } = await api.value.tick(row.id, { reason: "manual" });
    if (res?.data?.last_error) ElMessage.warning(res.data.last_error);
    else ElMessage.success("tick 完成");
    await fetchLives();
  } finally {
    busy.value = false;
  }
};

onMounted(() => {
  activeTab.value = tabFromRoute.value === "paper" || tabFromRoute.value === "live" ? "live" : tabFromRoute.value;
  void fetchFiles();
  void fetchDatasets();
  void fetchBacktests();
  void fetchLives();
});
</script>
