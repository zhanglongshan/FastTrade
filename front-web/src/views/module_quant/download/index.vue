<!-- 量化-下载：币安 K 线下载与本地 CSV 导入 -->
<template>
  <div class="flex flex-col gap-4 p-4">
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">下载币安数据</span>
          <span class="text-xs text-gray-500">数据保存到项目根目录 user_data/data/binance</span>
        </div>
      </template>
      <el-form :model="downloadForm" label-width="90px" inline>
        <el-form-item label="交易对">
          <el-input v-model="downloadForm.symbol" placeholder="BTC/USDT" style="width: 160px" />
        </el-form-item>
        <el-form-item label="周期">
          <el-select v-model="downloadForm.interval" style="width: 100px">
            <el-option v-for="item in intervals" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="开始">
          <el-date-picker
            v-model="downloadForm.start_time"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="默认近30天"
            style="width: 150px"
          />
        </el-form-item>
        <el-form-item label="结束">
          <el-date-picker
            v-model="downloadForm.end_time"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="默认今天"
            style="width: 150px"
          />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="downloadForm.description" placeholder="可选" style="width: 180px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="downloadLoading" @click="handleDownload">下载</el-button>
        </el-form-item>
      </el-form>
      <div v-if="lastResult" class="mt-2 text-sm text-gray-600">
        已写入 <code>{{ lastResult.file_path }}</code>，本次新增 {{ lastResult.downloaded }} 根，合计
        {{ lastResult.row_count }} 根 K 线。
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
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import QuantDataAPI, {
  QUANT_INTERVALS,
  type QuantDatasetTable,
  type QuantDownloadResult,
  type QuantLocalFile,
} from "@api/module_quant/data.ts";

const intervals = QUANT_INTERVALS;

const downloadForm = ref({ symbol: "BTC/USDT", interval: "1h", start_time: "", end_time: "", limit: 1000, description: "" });
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
      start_time: downloadForm.value.start_time || undefined,
      end_time: downloadForm.value.end_time || undefined,
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
