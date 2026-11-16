import type { App } from "vue";
import { createPinia } from "pinia";
import piniaPluginPersistedstate from "pinia-plugin-persistedstate";
import { router } from "@/router";
import { refreshMenuAndRoutes } from "@/router/refresh.ts";
import { useUserStore } from "./modules/user.store.ts";
import { useDictStore } from "./modules/dict.store.ts";
import { useNoticeStore } from "./modules/notice.store.ts";
import { useConfigStore } from "./modules/config.store.ts";
import { useWorktabStore } from "./modules/worktab.store.ts";

const store = createPinia();

store.use(piniaPluginPersistedstate);

export function initStore(app: App<Element>) {
  app.use(store);
}

export * from "./modules/app.store.ts";
export * from "./modules/config.store.ts";
export * from "./modules/dict.store.ts";
export * from "./modules/menu.store.ts";
export * from "./modules/notice.store.ts";
export * from "./modules/setting.store.ts";
export * from "./modules/table.store.ts";
export * from "./modules/user.store.ts";
export * from "./modules/worktab.store.ts";

export { store };

export interface RefreshCacheOptions {
  dictTypes?: string[];
  refreshUser?: boolean;
  refreshRoutes?: boolean;
  refreshConfig?: boolean;
  refreshNotice?: boolean;
  clearTags?: boolean;
  clearDictBefore?: boolean;
}

export async function refreshAppCaches(opts: RefreshCacheOptions = {}) {
  const {
    dictTypes,
    refreshUser = true,
    refreshRoutes = true,
    refreshConfig = true,
    refreshNotice = true,
    clearTags = false,
    clearDictBefore = false,
  } = opts;

  const userStore = useUserStore(store);
  const dictStore = useDictStore(store);
  const noticeStore = useNoticeStore(store);
  const configStore = useConfigStore(store);

  const tasks: Promise<any>[] = [];

  if (refreshUser) {
    tasks.push(userStore.getUserInfo());
  }
  if (refreshConfig) {
    tasks.push(configStore.getConfig(true));
  }
  if (refreshNotice) {
    tasks.push(noticeStore.getNotice(true));
  }
  if (dictTypes && dictTypes.length > 0) {
    if (clearDictBefore) dictStore.clearDictData();
    tasks.push(dictStore.getDict(dictTypes));
  }

  await Promise.allSettled(tasks);

  if (refreshRoutes) {
    await refreshMenuAndRoutes(router);
  }

  if (clearTags) {
    useWorktabStore(store).clearAll();
  }
}
