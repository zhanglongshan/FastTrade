<!-- 量化-图表：查看已下载数据集的 K 线、成交量、均线、区间概要与明细 -->
<template>
  <!-- 卡片统一 shrink-0：页面高度由布局 flex 链锁定，不设防会被压扁裁切 K 线 -->
  <div class="flex flex-col gap-4 p-4 [&>*]:shrink-0">
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
        <el-form-item label="叠加">
          <el-checkbox v-model="showMa">均线</el-checkbox>
          <el-checkbox v-model="showEma">EMA</el-checkbox>
          <el-checkbox v-model="showVolume">成交量</el-checkbox>
          <el-checkbox v-model="showRsi">RSI</el-checkbox>
          <el-checkbox v-model="showMfi">MFI</el-checkbox>
        </el-form-item>
        <el-form-item label="缩放">
          <el-button size="small" @click="klineRef?.zoomToLatest()">最近</el-button>
          <el-button size="small" @click="klineRef?.zoomToAll()">全览</el-button>
        </el-form-item>
      </el-form>
      <div v-if="result" class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
        <span>{{ result.symbol }} · {{ result.interval }}</span>
        <span>共 {{ result.total }} 根，当前显示 {{ result.candles.length }} 根</span>
        <span v-if="summary">
          区间最高 <b class="text-gray-700">{{ fmtPrice(summary.high) }}</b> / 最低
          <b class="text-gray-700">{{ fmtPrice(summary.low) }}</b>
        </span>
        <span v-if="summary">
          区间涨跌
          <b :style="{ color: summary.change >= 0 ? RISE : FALL }">
            {{ summary.change >= 0 ? "+" : "" }}{{ summary.change.toFixed(2) }}%
          </b>
        </span>
      </div>
    </el-card>

    <el-card shadow="never" v-loading="loading">
      <KlineChart
        ref="klineRef"
        :candles="result?.candles ?? []"
        :show-ma="showMa"
        :show-ema="showEma"
        :show-volume="showVolume"
        :show-rsi="showRsi"
        :show-mfi="showMfi"
        :height="480"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">OHLCV 明细</span>
          <span class="text-xs text-gray-500">共 {{ rows.length }} 行</span>
        </div>
      </template>
      <el-table :data="pagedRows" border stripe size="small" max-height="420">
        <el-table-column prop="date" label="时间(UTC)" min-width="180" />
        <el-table-column prop="open" label="开盘" min-width="110" align="right" />
        <el-table-column prop="high" label="最高" min-width="110" align="right" />
        <el-table-column prop="low" label="最低" min-width="110" align="right" />
        <el-table-column prop="close" label="收盘" min-width="110" align="right" />
        <el-table-column label="涨跌幅" width="120" align="right">
          <template #default="{ row }">
            <span :style="{ color: row.change >= 0 ? RISE : FALL }">
              {{ row.change >= 0 ? "+" : "" }}{{ row.change.toFixed(2) }}%
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="volume" label="成交量" min-width="140" align="right" />
      </el-table>
      <div class="mt-3 flex justify-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[50, 100, 200]"
          :total="rows.length"
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
import KlineChart from "@/components/quant/KlineChart.vue";
import QuantDataAPI, { type QuantCandle, type QuantDatasetTable } from "@api/module_quant/data.ts";

const RISE = "#ef5350";
const FALL = "#26a69a";

const datasets = ref<QuantDatasetTable[]>([]);
const datasetId = ref<number | null>(null);
const limit = ref(500);
const limits = [200, 500, 1000, 2000];
const showMa = ref(true);
const showEma = ref(true);
const showVolume = ref(true);
const showRsi = ref(true);
const showMfi = ref(true);
const loading = ref(false);
const result = ref<{ symbol: string; interval: string; total: number; candles: QuantCandle[] } | null>(null);
const klineRef = ref<{ zoomToLatest: () => void; zoomToAll: () => void } | null>(null);

const page = ref(1);
const pageSize = ref(50);

/** 价格格式化，与图表 tooltip 保持一致。 */
const fmtPrice = (value: number): string => {
  const abs = Math.abs(value);
  if (abs >= 1000) return value.toFixed(2);
  if (abs >= 1) return value.toFixed(4);
  if (abs >= 0.01) return value.toFixed(6);
  return value.toPrecision(6);
};

/** 明细行：附加相对前收盘的涨跌幅。 */
const rows = computed(() => {
  const candles = result.value?.candles ?? [];
  return candles.map((item, index) => {
    const prevClose = index > 0 ? (candles[index - 1]?.close ?? item.open) : item.open;
    const change = prevClose ? ((item.close - prevClose) / prevClose) * 100 : 0;
    return { ...item, change };
  });
});

/** 前端分页，避免上千行同时渲染卡顿。 */
const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return rows.value.slice(start, start + pageSize.value);
});

/** 区间概要：最高、最低、区间涨跌幅。 */
const summary = computed(() => {
  const candles = result.value?.candles ?? [];
  if (!candles.length) return null;
  let high = Number.NEGATIVE_INFINITY;
  let low = Number.POSITIVE_INFINITY;
  for (const item of candles) {
    if (item.high > high) high = item.high;
    if (item.low < low) low = item.low;
  }
  const first = candles[0]?.open ?? 0;
  const last = candles[candles.length - 1]?.close ?? 0;
  const change = first ? ((last - first) / first) * 100 : 0;
  return { high, low, change };
});

/** 加载 K 线。 */
const loadCandles = async () => {
  if (!datasetId.value) return;
  loading.value = true;
  try {
    const { data: res } = await QuantDataAPI.candles(datasetId.value, { limit: limit.value });
    result.value = res?.data ?? null;
    page.value = 1;
  } catch {
    ElMessage.error("K 线加载失败");
  } finally {
    loading.value = false;
  }
};

/** 切换数据集。 */
const handleDatasetChange = () => {
  page.value = 1;
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

onMounted(() => {
  void fetchDatasets();
});
</script>
