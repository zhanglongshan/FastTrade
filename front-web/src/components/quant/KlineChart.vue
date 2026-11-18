<!-- 量化 K 线图表：K 线 + 成交量 + 均线，三市场（web3/A股/期货）共用 -->
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
    /** 初始显示窗口宽度（百分比），100 表示全览 */
    zoomWidth?: number;
  }>(),
  { height: 460, showMa: true, showVolume: true, zoomWidth: 60 },
);

const chartEl = ref<HTMLDivElement | null>(null);
const hasData = computed(() => props.candles.length > 0);
let chart: ReturnType<typeof echarts.init> | null = null;
let observer: ResizeObserver | null = null;

const MA_COLORS: Record<number, string> = { 5: "#f5a623", 10: "#4a90e2", 20: "#9013fe" };
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

  // 均线预计算，供 series 与 tooltip 复用
  const maValues: Record<number, (number | null)[]> = {};
  if (props.showMa) {
    for (const period of [5, 10, 20]) maValues[period] = movingAverage(closes, period);
  }

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
        type: "line",
        name: `MA${period}`,
        data: maValues[period],
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        showSymbol: false,
        connectNulls: true,
        lineStyle: { width: 1.2, color: MA_COLORS[period] },
        emphasis: { disabled: true },
      });
    }
  }
  if (props.showVolume) {
    series.push({
      type: "bar",
      name: "成交量",
      data: volumes,
      xAxisIndex: 1,
      yAxisIndex: 1,
      barMaxWidth: 18,
    });
  }

  const hasVolume = props.showVolume;
  const zoomStart = candles.length > 1 ? Math.max(0, 100 - Math.min(100, props.zoomWidth)) : 0;

  chart.setOption(
    {
      animation: false,
      // 十字光标跨 K 线与成交量联动
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#666" } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        // 提示框限制在画布内，避免溢出到相邻卡片
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
          const maText = Object.entries(maValues)
            .map(([period, values]) => {
              const value = values[index];
              return `<span style="margin-right:10px;color:#666">MA${period} <b style="color:${
                MA_COLORS[Number(period)]
              }">${value == null ? "-" : fmtPrice(value)}</b></span>`;
            })
            .join("");
          return `<div style="line-height:1.75;min-width:180px">
            <div style="font-weight:600;margin-bottom:2px">${item.date.replace("T", " ").slice(0, 19)}</div>
            <div>开盘 <b>${fmtPrice(item.open)}</b>　最高 <b>${fmtPrice(item.high)}</b></div>
            <div>最低 <b>${fmtPrice(item.low)}</b>　收盘 <b style="color:${color}">${fmtPrice(item.close)}</b></div>
            <div>涨跌 <b style="color:${color}">${sign}${change.toFixed(2)}%</b></div>
            <div>成交量 <b>${fmtVolume(item.volume)}</b></div>
            ${maText ? `<div style="margin-top:2px">${maText}</div>` : ""}
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
      // 用底部百分比定位，避免固定像素在缩放条/不同高度下互相压盖
      grid: hasVolume
        ? [
            { left: 64, right: 20, top: 36, bottom: "34%" },
            { left: 64, right: 20, height: "15%", bottom: "12%" },
          ]
        : [{ left: 64, right: 20, top: 36, bottom: 52 }],
      xAxis: hasVolume
        ? [
            {
              type: "category",
              data: dates,
              gridIndex: 0,
              boundaryGap: true,
              axisLabel: { show: false },
              axisLine: { lineStyle: { color: "#ccc" } },
              axisTick: { show: false },
            },
            {
              type: "category",
              data: dates,
              gridIndex: 1,
              boundaryGap: true,
              axisLabel: { fontSize: 10, hideOverlap: true },
              axisLine: { lineStyle: { color: "#ccc" } },
              axisTick: { show: false },
            },
          ]
        : [
            {
              type: "category",
              data: dates,
              gridIndex: 0,
              boundaryGap: true,
              axisLabel: { fontSize: 10, hideOverlap: true },
              axisLine: { lineStyle: { color: "#ccc" } },
              axisTick: { show: false },
            },
          ],
      yAxis: hasVolume
        ? [
            {
              scale: true,
              gridIndex: 0,
              splitLine: { lineStyle: { opacity: 0.18 } },
              axisLabel: { fontSize: 10, formatter: (value: number) => fmtPrice(value) },
            },
            {
              scale: true,
              gridIndex: 1,
              splitNumber: 2,
              axisLabel: { fontSize: 10, formatter: (value: number) => fmtVolume(value) },
              splitLine: { show: false },
            },
          ]
        : [
            {
              scale: true,
              gridIndex: 0,
              splitLine: { lineStyle: { opacity: 0.18 } },
              axisLabel: { fontSize: 10, formatter: (value: number) => fmtPrice(value) },
            },
          ],
      dataZoom: [
        {
          type: "inside",
          xAxisIndex: hasVolume ? [0, 1] : [0],
          start: zoomStart,
          end: 100,
        },
        {
          type: "slider",
          xAxisIndex: hasVolume ? [0, 1] : [0],
          bottom: 8,
          height: 16,
          start: zoomStart,
          end: 100,
          brushSelect: false,
          showDetail: false,
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
  () => [props.candles, props.showMa, props.showVolume, props.zoomWidth],
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
