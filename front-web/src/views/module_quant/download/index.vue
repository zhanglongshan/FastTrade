<!-- 量化-下载：币安 K 线下载与本地 CSV 导入 -->
<template>
  <div class="flex flex-col gap-4 p-4">
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">下载币安数据</span>
          <span class="text-xs text-gray-500">现货 REST / 公开镜像 / 历史归档，保存到 user_data/data/binance</span>
        </div>
      </template>
      <el-form :model="downloadForm" label-width="90px" inline>
        <el-form-item label="交易对">
          <el-input v-model="downloadForm.symbol" placeholder="BTC/USDT" style="width: 150px" />
        </el-form-item>
        <el-form-item label="周期">
          <el-select v-model="downloadForm.interval" style="width: 90px">
            <el-option v-for="item in intervals" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="方式">
          <el-select v-model="downloadForm.source" style="width: 140px">
            <el-option label="现货 REST" value="api" />
            <el-option label="公开镜像" value="mirror" />
            <el-option label="历史归档" value="archive" />
          </el-select>
        </el-form-item>
        <el-form-item label="市场">
          <el-select v-model="downloadForm.market" style="width: 110px">
            <el-option label="现货" value="spot" />
            <el-option label="U本位合约" value="futures" />
          </el-select>
        </el-form-item>
        <el-form-item label="开始">
          <el-date-picker
            v-model="downloadForm.start_time"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="默认近30天"
            style="width: 140px"
          />
        </el-form-item>
        <el-form-item label="结束">
          <el-date-picker
            v-model="downloadForm.end_time"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="默认今天"
            style="width: 140px"
          />
        </el-form-item>
        <el-form-item label="最近天数">
          <el-input-number
            v-model="downloadForm.days"
            :min="1"
            :max="3650"
            controls-position="right"
            placeholder="留空用起止日期"
            style="width: 140px"
          />
        </el-form-item>
        <el-form-item label="清空重下">
          <el-switch v-model="downloadForm.erase" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="downloadForm.description" placeholder="可选" style="width: 160px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="downloadLoading" @click="handleDownload">下载</el-button>
        </el-form-item>
      </el-form>
      <div class="text-xs text-gray-500">
        归档方式按整月/日 ZIP 下载，历史可回溯至币种上市；归档有约 2 天延迟，默认自动用公开镜像补齐近端数据。
      </div>
      <div v-if="lastResult" class="mt-2 text-sm text-gray-600">
        已写入 <code>{{ lastResult.file_path }}</code>，本次新增 {{ lastResult.downloaded }} 根，合计
        {{ lastResult.row_count }} 根 K 线。
        <span v-if="lastResult.source === 'archive'">（归档文件 {{ lastResult.archive_files ?? 0 }} 个）</span>
        <div v-if="lastResult.warnings?.length" class="text-orange-500">
          {{ lastResult.warnings.join("；") }}
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">导入本地 CSV</span>
          <el-button link type="primary" @click="fetchFiles">刷新文件列表</el-button>
        </div>
      </template>
      <el-form :model="importForm" label-width="90px" inline>
        <el-form-item label="本地文件">
          <el-select v-model="importForm.file_path" filterable placeholder="选择 CSV" style="width: 340px">
            <el-option v-for="file in localFiles" :key="file.path" :label="`${file.name} (${formatSize(file.size)})`" :value="file.path" />
          </el-select>
        </el-form-item>
        <el-form-item label="交易对">
          <el-input v-model="importForm.symbol" placeholder="BTC/USDT" style="width: 160px" />
        </el-form-item>
        <el-form-item label="周期">
          <el-select v-model="importForm.interval" style="width: 100px">
            <el-option v-for="item in intervals" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源">
          <el-input v-model="importForm.source" style="width: 110px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="importLoading" @click="handleImport">导入</el-button>
        </el-form-item>
      </el-form>
      <div class="text-xs text-gray-500">CSV 需包含列：date, open, high, low, close, volume</div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">已登记数据集</span>
          <el-button link type="primary" @click="fetchDatasets">刷新</el-button>
        </div>
      </template>
      <el-table :data="datasets" v-loading="listLoading" border stripe size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="数据集" min-width="180" />
        <el-table-column prop="symbol" label="交易对" width="120" />
        <el-table-column prop="interval" label="周期" width="80" />
        <el-table-column prop="source" label="来源" width="100" />
        <el-table-column prop="row_count" label="K线数" width="100" />
        <el-table-column prop="file_path" label="本地文件" min-width="240" show-overflow-tooltip />
        <el-table-column prop="created_time" label="创建时间" width="170" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import QuantDataAPI, {
  QUANT_INTERVALS,
  type QuantDatasetTable,
  type QuantDownloadResult,
  type QuantDownloadForm,
  type QuantLocalFile,
} from "@api/module_quant/data.ts";

const intervals = QUANT_INTERVALS;

const downloadForm = ref<QuantDownloadForm>({
  symbol: "BTC/USDT",
  interval: "1h",
  source: "api",
  market: "spot",
  start_time: "",
  end_time: "",
  limit: 1000,
  description: "",
});

// 公开镜像只提供现货数据
watch(
  () => downloadForm.value.source,
  (source) => {
    if (source === "mirror") downloadForm.value.market = "spot";
  },
);
const importForm = ref({ file_path: "", symbol: "BTC/USDT", interval: "1h", source: "local" });
const datasets = ref<QuantDatasetTable[]>([]);
const localFiles = ref<QuantLocalFile[]>([]);
const lastResult = ref<QuantDownloadResult | null>(null);
const downloadLoading = ref(false);
const importLoading = ref(false);
const listLoading = ref(false);

/** 格式化文件大小。 */
const formatSize = (size: number): string => {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
};

/** 查询数据集列表。 */
const fetchDatasets = async () => {
  listLoading.value = true;
  try {
    const { data: res } = await QuantDataAPI.list({ limit: 100 });
    datasets.value = res?.data ?? [];
  } finally {
    listLoading.value = false;
  }
};

/** 查询可导入文件。 */
const fetchFiles = async () => {
  const { data: res } = await QuantDataAPI.files();
  localFiles.value = res?.data ?? [];
  if (!importForm.value.file_path) {
    const firstFile = localFiles.value.at(0);
    if (firstFile) importForm.value.file_path = firstFile.path;
  }
};

/** 提交下载任务。 */
const handleDownload = async () => {
  downloadLoading.value = true;
  try {
    const { data: res } = await QuantDataAPI.download({
      symbol: downloadForm.value.symbol,
      interval: downloadForm.value.interval,
      source: downloadForm.value.source,
      market: downloadForm.value.market,
      start_time: downloadForm.value.start_time || undefined,
      end_time: downloadForm.value.end_time || undefined,
      days: downloadForm.value.days || undefined,
      erase: downloadForm.value.erase,
      limit: downloadForm.value.limit,
      description: downloadForm.value.description || undefined,
    });
    lastResult.value = res?.data ?? null;
    ElMessage.success("下载完成");
    await fetchDatasets();
  } finally {
    downloadLoading.value = false;
  }
};

/** 提交导入任务。 */
const handleImport = async () => {
  if (!importForm.value.file_path) {
    ElMessage.warning("请先选择本地 CSV 文件");
    return;
  }
  importLoading.value = true;
  try {
    await QuantDataAPI.importCsv(importForm.value);
    ElMessage.success("导入完成");
    await fetchDatasets();
  } finally {
    importLoading.value = false;
  }
};

onMounted(() => {
  void fetchDatasets();
  void fetchFiles();
});
</script>
