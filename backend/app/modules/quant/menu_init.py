"""量化菜单初始化：向已初始化的数据库幂等补入「量化」菜单树。

新库的菜单来自 `sql/sys_menu.json`（已包含量化菜单），但既有库因种子数据
「表已有数据则跳过」的机制不会自动获得新菜单。本模块提供一个幂等命令，
供既有库执行一次即可在侧栏显示「量化 → 量化-下载 / 量化-回测」。

用法:
    uv run python main.py quant init-menu --env=dev
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_model import MappedBase
from app.modules.system.menu.model import MenuModel
from app.utils.import_util import ImportUtil

# 导入全部模型，确保 MenuModel.roles 的 secondary="sys_role_menus" 等跨模块关系可解析
ImportUtil.find_models(MappedBase)

# 量化菜单树：目录 + 两个页面。scope=web 保证管理端可见（超管自动可见；
# 非超管需在角色管理中勾选，或执行本命令后由管理员分配）。
QUANT_MENU_TREE: list[dict] = [
    {
        "name": "量化",
        "type": 1,
        "icon": "ri:bar-chart-2-line",
        "order": 8,
        "permission": None,
        "route_name": "Quant",
        "route_path": "/quant",
        "component_path": None,
        "status": 0,
        "keep_alive": True,
        "hidden": False,
        "always_show": True,
        "title": "量化",
        "affix": False,
        "redirect": "/quant/download",
        "scope": "web",
        "description": "量化工具：数据下载与策略回测",
        "_children": [
            {
                "name": "量化-下载",
                "type": 2,
                "icon": "ri:download-2-line",
                "order": 1,
                "permission": "module_quant:data:query",
                "route_name": "QuantDownload",
                "route_path": "download",
                "component_path": "module_quant/download/index",
                "status": 0,
                "keep_alive": True,
                "hidden": False,
                "always_show": False,
                "title": "量化-下载",
                "affix": False,
                "redirect": None,
                "description": "币安数据下载与本地 CSV 导入",
            },
            {
                "name": "量化-回测",
                "type": 2,
                "icon": "ri:line-chart-line",
                "order": 2,
                "permission": "module_quant:backtest:query",
                "route_name": "QuantBacktest",
                "route_path": "backtest",
                "component_path": "module_quant/backtest/index",
                "status": 0,
                "keep_alive": True,
                "hidden": False,
                "always_show": False,
                "title": "量化-回测",
                "affix": False,
                "redirect": None,
                "description": "策略回测配置与结果查看",
            },
            {
                "name": "量化-模拟盘",
                "type": 2,
                "icon": "ri:play-circle-line",
                "order": 3,
                "permission": "module_quant:live:query",
                "route_name": "QuantPaper",
                "route_path": "paper",
                "component_path": "module_quant/paper/index",
                "status": 0,
                "keep_alive": True,
                "hidden": False,
                "always_show": False,
                "title": "量化-模拟盘",
                "affix": False,
                "redirect": None,
                "description": "虚拟资金实时策略验证",
            },
            {
                "name": "量化-实盘",
                "type": 2,
                "icon": "ri:line-chart-line",
                "order": 4,
                "permission": "module_quant:live:query",
                "route_name": "QuantLive",
                "route_path": "live",
                "component_path": "module_quant/live/index",
                "status": 0,
                "keep_alive": True,
                "hidden": False,
                "always_show": False,
                "title": "量化-实盘",
                "affix": False,
                "redirect": None,
                "description": "币安实盘交易（默认 dry-run）",
            },
        ],
    }
]


async def ensure_quant_menus(db: AsyncSession) -> dict[str, int]:
    """幂等补入量化菜单树。

    参数:
    - db: 异步数据库会话。

    返回:
    - dict: 统计信息 {"created": 新建数, "skipped": 已存在数}。
    """
    created = 0
    skipped = 0

    for node in QUANT_MENU_TREE:
        children = node.get("_children", [])
        payload = {k: v for k, v in node.items() if k != "_children"}

        existing = (
            await db.execute(
                select(MenuModel).where(
                    MenuModel.route_path == payload["route_path"],
                    MenuModel.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()

        if existing:
            skipped += 1
            parent = existing
        else:
            parent = MenuModel(**payload)
            db.add(parent)
            await db.flush()
            created += 1

        # 无论父节点是新建还是已存在，都补齐缺失的子菜单
        for child in children:
            child_existing = (
                await db.execute(
                    select(MenuModel).where(
                        MenuModel.route_path == child["route_path"],
                        MenuModel.parent_id == parent.id,
                        MenuModel.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if child_existing:
                skipped += 1
                continue
            db.add(MenuModel(**child, parent_id=parent.id))
            created += 1
        await db.flush()

    return {"created": created, "skipped": skipped}
