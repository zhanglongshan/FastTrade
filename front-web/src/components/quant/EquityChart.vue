<!-- 净值曲线：回测结果可视化（面积渐变 + 初始资金基准线 + 收益率 tooltip） -->
<template>
  <!-- 外层固定高度并自行裁切，避免父级 flex/卡片压扁后 canvas 溢出 -->
  <div class="equity-chart" :style="{ height: `${height}px` }">
    <div v-show="hasData" ref="chartEl" class="equity-chart__canvas"></div>
    <el-empty v-if="!hasData" description="暂无净值数据" :image-size="60" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { echarts } from "@/plugins/echarts.ts";

interface EquityPoint {
  date: string;
  equity: number;
}

const props = withDefaults(
  defineProps<{
    points: EquityPoint[];
    initialCash?: number;
    height?: number;
  }>(),
  { initialCash: 0, height: 240 },
);

const chartEl = ref<HTMLDivElement | null>(null);
const hasData = computed(() => props.points.length > 0);
let chart: ReturnType<typeof echarts.init> | null = null;
let observer: ResizeObserver | null = null;

const LINE_COLOR = "#2563eb";
const RISE = "#ef5350";
const FALL = "#26a69a";

/** 数值格式化。 */
const fmt = (value: number): string =>
  Math.abs(value) >= 1000 ? value.toFixed(2) : value.toFixed(4);

const render = () => {
  const el = chartEl.value;
  const points = props.points;
  if (!el || !points.length) {
    chart?.dispose();
    chart = null;
    return;
  }
  if (!chart) chart = echarts.init(el);

  const dates = points.map((item) => item.date.slice(0, 16).replace("T", " "));
  const values = points.map((item) => item.equity);
  const base = props.initialCash || values[0] || 0;
  const zoomed = points.length > 300;

  chart.setOption(
    {
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        // 提示框限制在画布内
        confine: true,
        backgroundColor: "rgba(255,255,255,0.96)",
        borderColor: "#e0e0e0",
        borderWidth: 1,
        padding: 10,
        textStyle: { color: "#333", fontSize: 12 },
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params : [params];
          const first = list[0] as { dataIndex?: number } | undefined;
          const index = Number(first?.dataIndex ?? 0);
          const point = points[index];
          if (!point) return "";
          const change = base ? ((point.equity - base) / base) * 100 : 0;
          const color = change >= 0 ? RISE : FALL;
          return `<div style="line-height:1.7;min-width:160px">
            <div style="font-weight:600;margin-bottom:2px">${point.date.replace("T", " ").slice(0, 19)}</div>
            <div>净值 <b>${fmt(point.equity)}</b></div>
            <div>收益率 <b style="color:${color}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</b></div>
          </div>`;
        },
      },
      grid: { left: 70, right: 24, top: 24, bottom: zoomed ? 46 : 32 },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLabel: { fontSize: 10, hideOverlap: true },
        axisLine: { lineStyle: { color: "#ccc" } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { fontSize: 10, formatter: (value: number) => fmt(value) },
        splitLine: { lineStyle: { opacity: 0.18 } },
      },
      dataZoom: zoomed
        ? [
            { type: "inside", start: 0, end: 100 },
            {
              type: "slider",
              bottom: 8,
              height: 14,
              start: 0,
              end: 100,
              brushSelect: false,
              showDetail: false,
            },
          ]
        : [],
      series: [
        {
          type: "line",
          name: "净值",
          data: values,
          smooth: false,
          showSymbol: false,
          lineStyle: { width: 1.6, color: LINE_COLOR },
          areaStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(37,99,235,0.28)" },
                { offset: 1, color: "rgba(37,99,235,0.02)" },
              ],
            },
          },
          // 初始资金基准线
          markLine: base
            ? {
                silent: true,
                symbol: "none",
                label: { formatter: `初始 ${fmt(base)}`, fontSize: 10, color: "#888", position: "insideEndTop" },
                lineStyle: { color: "#999", type: "dashed", width: 1 },
                data: [{ yAxis: base }],
              }
            : undefined,
        },
      ],
    },
    true,
  );
  chart.resize();
};

const scheduleRender = async () => {
  await nextTick();
  render();
};

watch(() => [props.points, props.initialCash], scheduleRender, { deep: false });

const handleResize = () => chart?.resize();

/** 画布元素随数据出现，需跟随 ref 重新绑定尺寸监听。 */
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
.equity-chart {
  position: relative;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-width: 0;
  overflow: hidden;

  &__canvas {
    flex: 1 1 auto;
    align-self: stretch;
    width: 100%;
    height: 100%;
    min-width: 0;
  }
}
</style>
