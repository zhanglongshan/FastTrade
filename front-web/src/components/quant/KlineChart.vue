<!-- 量化 K 线图表：K 线 + MA/EMA + 成交量 + RSI/MFI，三市场（web3/A股/期货）共用 -->
<template>
  <!-- 外层固定高度并由自身裁切，避免父级 flex/卡片把它压扁后 canvas 溢出裁切错位 -->
  <div class="quant-chart" :style="{ height: `${height}px` }">
    <div v-show="hasData" ref="chartEl" class="quant-chart__canvas"></div>
    <el-empty v-if="!hasData" description="暂无 K 线数据" :image-size="80" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { echarts } from "@/plugins/echarts.ts";

interface Candle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

const props = withDefaults(
  defineProps<{
    candles: Candle[];
    height?: number;
    showMa?: boolean;
    showVolume?: boolean;
    showEma?: boolean;
    showRsi?: boolean;
    showMfi?: boolean;
    /** 初始显示窗口宽度（百分比），100 表示全览 */
    zoomWidth?: number;
  }>(),
  { height: 460, showMa: true, showVolume: true, showEma: true, showRsi: true, showMfi: true, zoomWidth: 60 },
);

const chartEl = ref<HTMLDivElement | null>(null);
const hasData = computed(() => props.candles.length > 0);
let chart: ReturnType<typeof echarts.init> | null = null;
let observer: ResizeObserver | null = null;

const MA_COLORS: Record<number, string> = { 5: "#f5a623", 10: "#4a90e2", 20: "#9013fe" };
const EMA_COLORS: Record<number, string> = { 50: "#e871ab", 100: "#17a2b8", 200: "#8e44ad" };
const RSI_COLOR = "#d39b00";
const MFI_COLOR = "#2e7d32";
const UP = "#ef5350"; // 中国习惯：涨红
const DOWN = "#26a69a"; // 跌绿

/** 价格格式化：按量级保留合适小数位。 */
const fmtPrice = (value: number): string => {
  const abs = Math.abs(value);
  if (abs >= 1000) return value.toFixed(2);
  if (abs >= 1) return value.toFixed(4);
  if (abs >= 0.01) return value.toFixed(6);
  return value.toPrecision(6);
};

/** 成交量格式化（K/M/B）。 */
const fmtVolume = (value: number): string => {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(2)}K`;
  return value.toFixed(2);
};

/** 简单移动平均（滑动窗口，O(n)）。 */
const movingAverage = (values: number[], period: number): (number | null)[] => {
  const result: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i] ?? 0;
    if (i >= period) sum -= values[i - period] ?? 0;
    result.push(i >= period - 1 ? Number((sum / period).toFixed(6)) : null);
  }
  return result;
};

/** 指数移动平均 EMA（首值用 SMA 种子，alpha=2/(period+1)）。 */
const exponentialAverage = (values: number[], period: number): (number | null)[] => {
  const result: (number | null)[] = [];
  if (!values.length) return result;
  const alpha = 2 / (period + 1);
  let prev = values[0]!;
  result.push(period <= 1 ? prev : null);
  for (let i = 1; i < values.length; i += 1) {
    if (i < period - 1) {
      // 前 period-1 根用递增 SMA 作为种子
      let seed = 0;
      for (let j = 0; j <= i; j += 1) seed += values[j]!;
      prev = seed / (i + 1);
      result.push(null);
    } else if (i === period - 1) {
      let seed = 0;
      for (let j = 0; j < period; j += 1) seed += values[j]!;
      prev = seed / period;
      result.push(Number(prev.toFixed(6)));
    } else {
      prev = values[i]! * alpha + prev * (1 - alpha);
      result.push(Number(prev.toFixed(6)));
    }
  }
  return result;
};

/** RSI（Wilder 平滑，默认 14）。 */
const relativeStrengthIndex = (values: number[], period = 14): (number | null)[] => {
  const result: (number | null)[] = new Array(values.length).fill(null);
  if (values.length <= period) return result;
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 1; i <= period; i += 1) {
    const diff = values[i]! - values[i - 1]!;
    if (diff > 0) avgGain += diff;
    else avgLoss -= diff;
  }
  avgGain /= period;
  avgLoss /= period;
  const calc = (gain: number, loss: number): number => (loss === 0 ? 100 : 100 - 100 / (1 + gain / loss));
  result[period] = Number(calc(avgGain, avgLoss).toFixed(2));
  for (let i = period + 1; i < values.length; i += 1) {
    const diff = values[i]! - values[i - 1]!;
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    result[i] = Number(calc(avgGain, avgLoss).toFixed(2));
  }
  return result;
};

/** MFI 资金流量指标（典型价 TP=(H+L+C)/3 × 成交量，Wilder 平滑，默认 14）。 */
const moneyFlowIndex = (candles: Candle[], period = 14): (number | null)[] => {
  const result: (number | null)[] = new Array(candles.length).fill(null);
  if (candles.length <= period) return result;
  const tp = candles.map((c) => (c.high + c.low + c.close) / 3);
  const mf = candles.map((c, i) => tp[i]! * (c.volume || 0));
  let pos = 0;
  let neg = 0;
  for (let i = 1; i <= period; i += 1) {
    if (tp[i]! > tp[i - 1]!) pos += mf[i]!;
    else if (tp[i]! < tp[i - 1]!) neg += mf[i]!;
  }
  const calc = (p: number, n: number): number => (n === 0 ? 100 : 100 - 100 / (1 + p / n));
  result[period] = Number(calc(pos, neg).toFixed(2));
  for (let i = period + 1; i < candles.length; i += 1) {
    if (tp[i]! > tp[i - 1]!) {
      pos = pos - pos / period + mf[i]!;
      neg = neg - neg / period;
    } else if (tp[i]! < tp[i - 1]!) {
      pos = pos - pos / period;
      neg = neg - neg / period + mf[i]!;
    } else {
      pos = pos - pos / period;
      neg = neg - neg / period;
    }
    result[i] = Number(calc(pos, neg).toFixed(2));
  }
  return result;
};

/** 渲染 / 重绘图表。 */
const render = () => {
  const el = chartEl.value;
  const candles = props.candles;
  if (!el || !candles.length) {
    chart?.dispose();
    chart = null;
    return;
  }
  if (!chart) chart = echarts.init(el);

  const dates = candles.map((item) => item.date.slice(0, 16).replace("T", " "));
  // echarts candlestick 数据顺序：[开盘, 收盘, 最低, 最高]
  const ohlc = candles.map((item) => [item.open, item.close, item.low, item.high]);
  const closes = candles.map((item) => item.close);
  const volumes = candles.map((item) => ({
    value: item.volume,
    itemStyle: { color: item.close >= item.open ? UP : DOWN, opacity: 0.65 },
  }));

  // 指标预计算，供 series 与 tooltip 复用
  const maValues: Record<number, (number | null)[]> = {};
  if (props.showMa) for (const p of [5, 10, 20]) maValues[p] = movingAverage(closes, p);
  const emaValues: Record<number, (number | null)[]> = {};
  if (props.showEma) for (const p of [50, 100, 200]) emaValues[p] = exponentialAverage(closes, p);
  const rsiValues = props.showRsi ? relativeStrengthIndex(closes, 14) : null;
  const mfiValues = props.showMfi ? moneyFlowIndex(candles, 14) : null;
  const hasOsc = Boolean(rsiValues || mfiValues); // RSI/MFI 共用一个 0-100 副图

  const series: Record<string, unknown>[] = [
    {
      type: "candlestick",
      name: "K线",
      data: ohlc,
      xAxisIndex: 0,
      yAxisIndex: 0,
      barMaxWidth: 18,
      itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN, borderWidth: 1 },
    },
  ];
  if (props.showMa) {
    for (const period of [5, 10, 20]) {
      series.push({
        type: "line", name: `MA${period}`, data: maValues[period],
        xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, connectNulls: true,
        lineStyle: { width: 1.2, color: MA_COLORS[period] }, emphasis: { disabled: true },
      });
    }
  }
  if (props.showEma) {
    for (const period of [50, 100, 200]) {
      series.push({
        type: "line", name: `EMA${period}`, data: emaValues[period],
        xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, connectNulls: true,
        lineStyle: { width: 1.4, color: EMA_COLORS[period] }, emphasis: { disabled: true },
      });
    }
  }
  const volumeAxis = props.showVolume ? 1 : -1;
  if (props.showVolume) {
    series.push({
      type: "bar", name: "成交量", data: volumes,
      xAxisIndex: volumeAxis, yAxisIndex: volumeAxis, barMaxWidth: 18,
    });
  }
  const oscAxis = hasOsc ? (props.showVolume ? 2 : 1) : -1;
  if (rsiValues) {
    series.push({
      type: "line", name: "RSI14", data: rsiValues,
      xAxisIndex: oscAxis, yAxisIndex: oscAxis, smooth: false, showSymbol: false, connectNulls: false,
      lineStyle: { width: 1.3, color: RSI_COLOR }, emphasis: { disabled: true },
      markLine: {
        silent: true, symbol: "none",
        label: { fontSize: 9, color: "#999" },
        lineStyle: { color: "#ddd", type: "dashed", width: 1 },
        data: [{ yAxis: 70 }, { yAxis: 30 }],
      },
    });
  }
  if (mfiValues) {
    series.push({
      type: "line", name: "MFI14", data: mfiValues,
      xAxisIndex: oscAxis, yAxisIndex: oscAxis, smooth: false, showSymbol: false, connectNulls: false,
      lineStyle: { width: 1.3, color: MFI_COLOR }, emphasis: { disabled: true },
    });
  }

  // 动态 grid：主图(0) + 成交量(可选) + 震荡指标(可选)，底部留 dataZoom 空间
  const grid: Record<string, unknown>[] = [];
  const xAxis: Record<string, unknown>[] = [];
  const yAxis: Record<string, unknown>[] = [];
  const zoomIndexes: number[] = [0];

  // 主图 grid：有副图时压缩高度，否则占满
  const mainBottom = props.showVolume || hasOsc ? (hasOsc ? (props.showVolume ? "48%" : "34%") : "34%") : 52;
  grid.push({ left: 64, right: 20, top: 36, bottom: mainBottom });
  xAxis.push({
    type: "category", data: dates, gridIndex: 0, boundaryGap: true,
    axisLabel: { show: false }, axisLine: { lineStyle: { color: "#ccc" } }, axisTick: { show: false },
  });
  yAxis.push({
    scale: true, gridIndex: 0, splitLine: { lineStyle: { opacity: 0.18 } },
    axisLabel: { fontSize: 10, formatter: (value: number) => fmtPrice(value) },
  });

  if (props.showVolume) {
    const volTop = hasOsc ? "54%" : "68%";
    grid.push({ left: 64, right: 20, top: volTop, height: hasOsc ? "12%" : "15%" });
    xAxis.push({
      type: "category", data: dates, gridIndex: 1, boundaryGap: true,
      axisLabel: { show: !hasOsc, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: "#ccc" } }, axisTick: { show: false },
    });
    yAxis.push({
      scale: true, gridIndex: 1, splitNumber: 2,
      axisLabel: { fontSize: 10, formatter: (value: number) => fmtVolume(value) },
      splitLine: { show: false },
    });
    zoomIndexes.push(1);
  }

  if (hasOsc) {
    grid.push({ left: 64, right: 20, top: "73%", height: "13%" });
    xAxis.push({
      type: "category", data: dates, gridIndex: oscAxis, boundaryGap: true,
      axisLabel: { fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: "#ccc" } }, axisTick: { show: false },
    });
    yAxis.push({
      min: 0, max: 100, gridIndex: oscAxis, splitNumber: 2,
      axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { opacity: 0.12 } },
    });
    zoomIndexes.push(oscAxis);
  }

  const zoomStart = candles.length > 1 ? Math.max(0, 100 - Math.min(100, props.zoomWidth)) : 0;

  chart.setOption(
    {
      animation: false,
      // 十字光标跨所有 grid 联动
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#666" } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        confine: true,
        backgroundColor: "rgba(255,255,255,0.96)",
        borderColor: "#e0e0e0",
        borderWidth: 1,
        padding: 10,
        textStyle: { color: "#333", fontSize: 12 },
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params : [params];
          const first = list.find((item: any) => item?.seriesName === "K线") ?? list[0];
          const index = Number((first as any)?.dataIndex ?? 0);
          const item = candles[index];
          if (!item) return "";
          const prevClose = index > 0 ? (candles[index - 1]?.close ?? item.open) : item.open;
          const change = prevClose ? ((item.close - prevClose) / prevClose) * 100 : 0;
          const rising = item.close >= item.open;
          const color = rising ? UP : DOWN;
          const sign = change >= 0 ? "+" : "";
          const line = (name: string, value: number | null | undefined, c: string): string =>
            `<span style="margin-right:10px;color:#666">${name} <b style="color:${c}">${
              value == null || Number.isNaN(value) ? "-" : fmtPrice(value)
            }</b></span>`;
          const maText = Object.entries(maValues).map(([p, v]) => line(`MA${p}`, v[index], MA_COLORS[Number(p)]!)).join("");
          const emaText = Object.entries(emaValues).map(([p, v]) => line(`EMA${p}`, v[index], EMA_COLORS[Number(p)]!)).join("");
          const oscText = [
            rsiValues ? line("RSI14", rsiValues[index], RSI_COLOR) : "",
            mfiValues ? line("MFI14", mfiValues[index], MFI_COLOR) : "",
          ].join("");
          return `<div style="line-height:1.75;min-width:180px">
            <div style="font-weight:600;margin-bottom:2px">${item.date.replace("T", " ").slice(0, 19)}</div>
            <div>开盘 <b>${fmtPrice(item.open)}</b>　最高 <b>${fmtPrice(item.high)}</b></div>
            <div>最低 <b>${fmtPrice(item.low)}</b>　收盘 <b style="color:${color}">${fmtPrice(item.close)}</b></div>
            <div>涨跌 <b style="color:${color}">${sign}${change.toFixed(2)}%</b></div>
            <div>成交量 <b>${fmtVolume(item.volume)}</b></div>
            ${maText ? `<div style="margin-top:2px">${maText}</div>` : ""}
            ${emaText ? `<div>${emaText}</div>` : ""}
            ${oscText ? `<div>${oscText}</div>` : ""}
          </div>`;
        },
      },
      legend: {
        top: 4,
        icon: "roundRect",
        itemWidth: 12,
        itemHeight: 4,
        textStyle: { fontSize: 11 },
        data: series.map((item) => item.name as string),
      },
      grid,
      xAxis,
      yAxis,
      dataZoom: [
        { type: "inside", xAxisIndex: zoomIndexes, start: zoomStart, end: 100 },
        {
          type: "slider", xAxisIndex: zoomIndexes, bottom: 8, height: 16,
          start: zoomStart, end: 100, brushSelect: false, showDetail: false,
        },
      ],
      series,
    },
    true,
  );
  // 容器尺寸可能在隐藏标签页 / 卡片布局完成前为 0，绘制后补一次尺寸校正
  chart.resize();
};

const scheduleRender = async () => {
  await nextTick();
  render();
};

watch(
  () => [props.candles, props.showMa, props.showVolume, props.showEma, props.showRsi, props.showMfi, props.zoomWidth],
  scheduleRender,
  { deep: false },
);

const handleResize = () => chart?.resize();

/** 缩放到最近一段（默认窗口宽度）。 */
const zoomToLatest = () => {
  if (!chart) return;
  const start = Math.max(0, 100 - Math.min(100, props.zoomWidth));
  chart.dispatchAction({ type: "dataZoom", start, end: 100 });
};

/** 缩放到全览。 */
const zoomToAll = () => {
  chart?.dispatchAction({ type: "dataZoom", start: 0, end: 100 });
};

defineExpose({ zoomToLatest, zoomToAll });

/** 监听画布元素尺寸（元素可能因数据到达才显示，需随 ref 变化重新绑定）。 */
watch(
  chartEl,
  (el, oldEl) => {
    if (oldEl) observer?.unobserve(oldEl);
    if (el && typeof ResizeObserver !== "undefined") {
      observer ??= new ResizeObserver(() => handleResize());
      observer.observe(el);
    }
  },
  { flush: "post" },
);

onMounted(() => {
  void scheduleRender();
  window.addEventListener("resize", handleResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", handleResize);
  observer?.disconnect();
  observer = null;
  chart?.dispose();
  chart = null;
});
</script>

<style scoped lang="scss">
.quant-chart {
  position: relative;
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  overflow: hidden;

  // 空数据时垂直居中提示
  display: flex;
  align-items: center;
  justify-content: center;

  &__canvas {
    flex: 1 1 auto;
    align-self: stretch;
    width: 100%;
    height: 100%;
    min-width: 0;
  }
}
</style>
