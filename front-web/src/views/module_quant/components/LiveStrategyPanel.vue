<!-- 实时策略通用面板：模拟盘/实盘共用，仅 mode 与风险提示不同 -->
<template>
  <!-- 卡片统一 shrink-0：避免布局 flex 链压扁卡片导致内容裁切 -->
  <div class="flex flex-col gap-4 p-4 [&>*]:shrink-0">
    <el-alert
      v-if="mode === 'live'"
      type="warning"
      show-icon
      :closable="false"
      title="实盘风险提示"
      description="实盘默认以 dry-run 模拟运行，不会真实下单。只有显式关闭 dry-run 并配置币安 API Key/Secret 后才会真实交易；现货不支持做空。请先用模拟盘充分验证策略。"
    />

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">新建{{ mode === 'paper' ? '模拟盘' : '实盘' }}策略</span>
          <span class="text-xs text-gray-500">同一套策略接口，回测/模拟/实盘通用</span>
        </div>
      </template>
      <el-form :model="form" label-width="96px" inline>
        <el-form-item label="实例名称">
          <el-select
            v-model="form.name"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="选择已有实例或输入新名称"
            style="width: 200px"
          >
            <el-option v-for="n in existingNames" :key="n" :label="n" :value="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="策略">
          <el-select v-model="form.strategy_name" filterable placeholder="选择策略" style="width: 200px">
            <el-option v-for="item in strategies" :key="item.name" :label="`${item.name} (${item.mode})`" :value="item.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="交易对">
          <el-input v-model="form.symbol" placeholder="BTC/USDT" style="width: 130px" />
        </el-form-item>
        <el-form-item label="周期">
          <el-select v-model="form.interval" style="width: 90px">
            <el-option v-for="i in intervals" :key="i" :label="i" :value="i" />
          </el-select>
        </el-form-item>
        <el-form-item label="初始资金">
          <el-input-number v-model="form.initial_cash" :min="1" :step="1000" style="width: 140px" />
        </el-form-item>
        <el-form-item label="手续费率">
          <el-input-number v-model="form.fee_rate" :min="0" :max="0.1" :step="0.0001" :precision="4" style="width: 130px" />
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="form.allow_short" />
        </el-form-item>
        <el-form-item v-if="mode === 'live'" label="dry-run">
          <el-switch v-model="form.dry_run" />
          <span class="ml-2 text-xs text-gray-500">{{ form.dry_run ? '模拟' : '真实下单' }}</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
        </el-form-item>
      </el-form>
      <div v-if="mode === 'live' && !form.dry_run" class="grid grid-cols-1 md:grid-cols-2 gap-3 mt-1">
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" show-password placeholder="仅真实下单需要，加密存储" />
        </el-form-item>
        <el-form-item label="API Secret">
          <el-input v-model="form.api_secret" type="password" show-password placeholder="仅真实下单需要，加密存储" />
        </el-form-item>
      </div>
      <div v-if="strategies.length === 0" class="text-xs text-orange-500">
        未发现策略。请在 user_data/strategies 下添加继承 Strategy 的 .py 文件。
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">{{ mode === 'paper' ? '模拟盘' : '实盘' }}实例</span>
          <el-button link type="primary" @click="fetchList">刷新</el-button>
        </div>
      </template>
      <el-table :data="items" v-loading="listLoading" border stripe size="small">
        <el-table-column prop="id" label="ID" width="64" />
        <el-table-column prop="name" label="名称" min-width="150" />
        <el-table-column prop="strategy_name" label="策略" width="150" />
        <el-table-column prop="symbol" label="交易对" width="100" />
        <el-table-column prop="interval" label="周期" width="70" />
        <el-table-column label="净值/收益" width="150">
          <template #default="{ row }">
            <template v-if="row.account">
              <div>{{ row.account.equity.toFixed(2) }}</div>
              <div :class="row.account.equity >= row.account.initial_cash ? 'text-red-600' : 'text-green-600'" class="text-xs">
                {{ formatPercent(row.account.equity / row.account.initial_cash - 1) }}
              </div>
            </template>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <span :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最近运行" width="160">
          <template #default="{ row }">
            <div>{{ row.last_tick_time || '-' }}</div>
            <div v-if="row.last_error" class="text-xs text-red-500" :title="row.last_error">异常</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="row.status === 1" @click="handleStart(row)">启动</el-button>
            <el-button link type="warning" :disabled="row.status !== 1" @click="handleStop(row)">停止</el-button>
            <el-button link type="success" :loading="tickingId === row.id" @click="handleTick(row)">运行一次</el-button>
            <el-button link @click="handleSelect(row)">详情</el-button>
            <el-button link type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="current" shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">实例详情：{{ current.name }}</span>
          <el-button link @click="current = null; trades = []">关闭</el-button>
        </div>
      </template>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4" v-if="current.account">
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">账户净值</div>
          <div class="text-lg font-semibold">{{ current.account.equity.toFixed(2) }}</div>
        </div>
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">可用现金</div>
          <div class="text-lg font-semibold">{{ current.account.cash.toFixed(2) }}</div>
        </div>
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">多头 / 空头</div>
          <div class="text-lg font-semibold">{{ current.account.long_qty.toFixed(4) }} / {{ current.account.short_qty.toFixed(4) }}</div>
        </div>
        <div class="border rounded p-3">
          <div class="text-xs text-gray-500">已实现盈亏</div>
          <div class="text-lg font-semibold">{{ current.account.realized_pnl.toFixed(2) }}</div>
        </div>
      </div>
      <div v-if="current.last_result" class="mb-4">
        <div class="text-xs text-gray-500 mb-1">
          最近 tick：价格 {{ current.last_result.price }} · 净值 {{ current.last_result.equity }} · {{ current.last_result.tick_time }}
        </div>
        <pre class="p-2 bg-gray-50 border rounded text-xs overflow-auto max-h-32">{{ (current.last_result.logs || []).join("\n") || "（无策略日志）" }}</pre>
        <div v-if="current.last_result.error" class="text-xs text-red-500 mt-1">{{ current.last_result.error }}</div>
      </div>
      <div class="text-xs text-gray-500 mb-1">成交流水</div>
      <el-table :data="trades" border stripe size="small" max-height="300">
        <el-table-column prop="trade_time" label="时间" min-width="170" />
        <el-table-column prop="side" label="方向" width="100" />
        <el-table-column prop="qty" label="数量" width="120" />
        <el-table-column prop="price" label="价格" width="110" />
        <el-table-column prop="fee" label="手续费" width="100" />
        <el-table-column prop="pnl" label="盈亏" width="100" />
        <el-table-column prop="reason" label="原因" min-width="140" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import QuantLiveAPI, {
  type QuantLiveStrategyForm,
  type QuantLiveStrategyTable,
  type QuantLiveTradeRecord,
} from "@api/module_quant/live.ts";
import { QuantBacktestAPI, type QuantStrategyTable } from "@api/module_quant/backtest.ts";

const props = defineProps<{ mode: "paper" | "live" }>();

const intervals = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const items = ref<QuantLiveStrategyTable[]>([]);
const strategies = ref<QuantStrategyTable[]>([]);
const listLoading = ref(false);
const creating = ref(false);
const tickingId = ref<number | null>(null);
const current = ref<QuantLiveStrategyTable | null>(null);
const trades = ref<QuantLiveTradeRecord[]>([]);

const form = ref<QuantLiveStrategyForm>({
  name: "",
  mode: props.mode,
  strategy_name: "",
  symbol: "BTC/USDT",
  interval: "1h",
  initial_cash: 10000,
  fee_rate: 0.001,
  slippage: 0,
  allow_short: props.mode === "paper",
  dry_run: true,
  api_key: "",
  api_secret: "",
});

type LiveRow = Record<string, any>;

/** 已有实例名称（去重、去空），供「实例名称」下拉选择；仍可直接输入新名称 */
const existingNames = computed<string[]>(() => {
  const names = items.value.map((it) => String(it.name ?? "")).filter(Boolean);
  return Array.from(new Set(names));
});

const formatPercent = (value: number): string => `${(value * 100).toFixed(2)}%`;
const statusText = (status: number): string => (status === 1 ? "运行中" : status === 2 ? "异常" : "已停止");
const statusClass = (status: number): string =>
  status === 1 ? "text-green-600" : status === 2 ? "text-red-500" : "text-gray-500";

const fetchList = async () => {
  listLoading.value = true;
  try {
    const { data: res } = await QuantLiveAPI.list(props.mode);
    items.value = res?.data ?? [];
  } finally {
    listLoading.value = false;
  }
};

const fetchStrategies = async () => {
  const { data: res } = await QuantBacktestAPI.strategies();
  strategies.value = res?.data ?? [];
  if (!form.value.strategy_name) {
    const first = strategies.value.at(0);
    if (first) form.value.strategy_name = first.name;
  }
};

const handleCreate = async () => {
  if (!form.value.name || !form.value.strategy_name || !form.value.symbol) {
    ElMessage.warning("请填写实例名称、策略和交易对");
    return;
  }
  creating.value = true;
  try {
    await QuantLiveAPI.create(form.value);
    ElMessage.success("实例创建成功");
    await fetchList();
  } finally {
    creating.value = false;
  }
};

const handleStart = async (row: LiveRow) => {
  await QuantLiveAPI.start(row.id);
  ElMessage.success("已启动");
  await fetchList();
};

const handleStop = async (row: LiveRow) => {
  await QuantLiveAPI.stop(row.id);
  ElMessage.success("已停止");
  await fetchList();
};

const handleTick = async (row: LiveRow) => {
  tickingId.value = row.id;
  try {
    const { data: res } = await QuantLiveAPI.tick(row.id);
    if (res?.data?.error) ElMessage.error(`运行异常：${res.data.error}`);
    else ElMessage.success("运行完成");
    await fetchList();
    if (current.value?.id === row.id) await loadTrades(row.id);
  } finally {
    tickingId.value = null;
  }
};

const handleSelect = async (row: LiveRow) => {
  const { data: res } = await QuantLiveAPI.detail(row.id);
  current.value = res?.data ?? items.value.find((item) => item.id === row.id) ?? null;
  await loadTrades(row.id);
};

const loadTrades = async (id: number) => {
  const { data: res } = await QuantLiveAPI.trades(id);
  trades.value = res?.data ?? [];
};

const handleDelete = async (row: LiveRow) => {
  await ElMessageBox.confirm(`确定删除实例「${row.name}」？`, "提示", { type: "warning" });
  await QuantLiveAPI.remove(row.id);
  if (current.value?.id === row.id) current.value = null;
  ElMessage.success("已删除");
  await fetchList();
};

onMounted(() => {
  void fetchList();
  void fetchStrategies();
});
</script>
