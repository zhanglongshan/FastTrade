<!-- 量化-图表：查看已下载数据集的 K 线、成交量与明细 -->
<template>
  <div class="flex flex-col gap-4 p-4">
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">K 线图表</span>
          <el-button link type="primary" @click="fetchDatasets">刷新数据集</el-button>
        </div>
      </template>
      <el-form label-width="80px" inline>
        <el-form-item label="数据集">
          <el-select
            v-model="datasetId"
            filterable
            placeholder="选择已登记的数据集"
            style="width: 320px"
            @change="handleDatasetChange"
          >
            <el-option
              v-for="item in datasets"
              :key="item.id"
              :label="`${item.name} (${item.row_count} 根)`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="显示条数">
          <el-select v-model="limit" style="width: 130px" @change="loadCandles">
            <el-option v-for="n in limits" :key="n" :label="`最后 ${n} 根`" :value="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="均线">
          <el-checkbox v-model="showMa">MA5 / MA10 / MA20</el-checkbox>
        </el-form-item>
      </el-form>
      <div v-if="result" class="text-xs text-gray-500">
        {{ result.symbol }} · {{ result.interval }} · 共 {{ result.total }} 根，当前显示 {{ result.candles.length }} 根
      </div>
    </el-card>

    <el-card shadow="never">
      <div ref="chartEl" class="kline-chart" v-loading="loading"></div>
      <el-empty v-if="!datasetId && !loading" description="请先选择一个数据集" />
    </el-card>

    <el-card shadow="never">
      <template #header><span class="font-semibold">OHLCV 明细</span></template>
      <el-table :data="result?.candles ?? []" border stripe size="small" max-height="420">
        <el-table-column prop="date" label="时间(UTC)" min-width="200" />
        <el-table-column prop="open" label="开盘" min-width="110" />
        <el-table-column prop="high" label="最高" min-width="110" />
        <el-table-column prop="low" label="最低" min-width="110" />
        <el-table-column prop="close" label="收盘" min-width="110" />
        <el-table-column prop="volume" label="成交量" min-width="130" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, nextTick } from "vue";
import { ElMessage } from "element-plus";
import { echarts } from "@/plugins/echarts.ts";
import QuantDataAPI, { type QuantCandle, type QuantDatasetTable } from "@api/module_quant/data.ts";

const chartEl = ref<HTMLDivElement | null>(null);
const datasets = ref<QuantDatasetTable[]>([]);
const datasetId = ref<number | null>(null);
const limit = ref(500);
const limits = [200, 500, 1000, 2000];
const showMa = ref(true);
const loading = ref(false);
const result = ref<{ symbol: string; interval: string; total: number; candles: QuantCandle[] } | null>(null);

let chart: ReturnType<typeof echarts.init> | null = null;

/** 简单移动平均。 */
const ma = (values: number[], period: number): (number | null)[] => {
  return values.map((_, i) => {
    if (i < period - 1) return null;
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += values[j] ?? 0;
    return Number((sum / period).toFixed(4));
  });
};

/** 渲染图表。 */
const renderChart = () => {
  if (!chartEl.value || !result.value?.candles.length) return;
  if (!chart) chart = echarts.init(chartEl.value);

  const candles = result.value.candles;
  const dates = candles.map((item) => item.date.slice(0, 16).replace("T", " "));
  // echarts candlestick 数据顺序：[开盘, 收盘, 最低, 最高]
  const ohlc = candles.map((item) => [item.open, item.close, item.low, item.high]);
  const closes = candles.map((item) => item.close);
  // 中国习惯：涨红跌绿
  const volumes = candles.map((item) => ({
    value: item.volume,
    itemStyle: { color: item.close >= item.open ? "#ef5350" : "#26a69a" },
  }));

  const series: Record<string, unknown>[] = [
    {
      type: "candlestick",
      name: "K线",
      data: ohlc,
      xAxisIndex: 0,
      yAxisIndex: 0,
      itemStyle: { color: "#ef5350", color0: "#26a69a", borderColor: "#ef5350", borderColor0: "#26a69a" },
    },
    { type: "bar", name: "成交量", data: volumes, xAxisIndex: 1, yAxisIndex: 1 },
  ];
  if (showMa.value) {
    for (const [period, color] of [[5, "#f5a623"], [10, "#4a90e2"], [20, "#9013fe"]] as const) {
      series.push({
        type: "line",
        name: `MA${period}`,
        data: ma(closes, period),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color },
      });
    }
  }

  chart.setOption(
    {
      animation: false,
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: { data: series.map((item) => item.name as string), top: 0 },
      grid: [
        { left: 60, right: 20, top: 40, height: "55%" },
        { left: 60, right: 20, top: "76%", height: "16%" },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, boundaryGap: true, axisLabel: { show: false } },
        { type: "category", data: dates, gridIndex: 1, boundaryGap: true },
      ],
      yAxis: [
        { scale: true, gridIndex: 0, splitLine: { lineStyle: { opacity: 0.2 } } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 60, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], top: "94%", height: 18, start: 60, end: 100 },
      ],
      series,
    },
    true,
  );
};

/** 加载 K 线。 */
const loadCandles = async () => {
  if (!datasetId.value) return;
  loading.value = true;
  try {
    const { data: res } = await QuantDataAPI.candles(datasetId.value, { limit: limit.value });
    result.value = res?.data ?? null;
    await nextTick();
    renderChart();
  } catch {
    ElMessage.error("K 线加载失败");
  } finally {
    loading.value = false;
  }
};

/** 切换数据集。 */
const handleDatasetChange = () => {
  void loadCandles();
};

/** 查询数据集列表。 */
const fetchDatasets = async () => {
  const { data: res } = await QuantDataAPI.list({ limit: 200 });
  datasets.value = res?.data ?? [];
  if (!datasetId.value && datasets.value.length) {
    datasetId.value = datasets.value[0]?.id ?? null;
    await loadCandles();
  }
};

/** 窗口尺寸变化时重绘。 */
const handleResize = () => chart?.resize();

onMounted(() => {
  void fetchDatasets();
  window.addEventListener("resize", handleResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", handleResize);
  chart?.dispose();
  chart = null;
});
</script>

<style scoped>
.kline-chart {
  width: 100%;
  height: 460px;
}
</style>
