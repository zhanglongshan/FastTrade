"""量化菜单初始化：向既有数据库幂等补入三套量化菜单树。

新库的菜单来自 `sql/sys_menu.json`（已包含 web3-量化 / 量化-A股 / 量化-期货），
既有库因种子数据「表已有数据则跳过」机制不会自动获得新菜单。本模块提供幂等命令。

用法:
    uv run python main.py quant init-menu --env=dev
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_model import MappedBase
from app.modules.system.menu.model import MenuModel
from app.utils.import_util import ImportUtil

# 导入全部模型，确保 MenuModel.roles 的 secondary 等跨模块关系可解析
ImportUtil.find_models(MappedBase)


def _page(name, permission, route_name, route_path, component, icon, order, description):
    """构造页面节点。"""
    return {
        "name": name,
        "type": 2,
        "icon": icon,
        "order": order,
        "permission": permission,
        "route_name": route_name,
        "route_path": route_path,
        "component_path": component,
        "status": 0,
        "keep_alive": True,
        "hidden": False,
        "always_show": False,
        "title": name,
        "affix": False,
        "redirect": None,
        "description": description,
    }


def _directory(name, route_name, route_path, icon, order, description, children):
    """构造目录节点。"""
    return {
        "name": name,
        "type": 1,
        "icon": icon,
        "order": order,
        "permission": None,
        "route_name": route_name,
        "route_path": route_path,
        "component_path": None,
        "status": 0,
        "keep_alive": True,
        "hidden": False,
        "always_show": True,
        "title": name,
        "affix": False,
        "redirect": f"{route_path}/{children[0]['route_path']}",
        "scope": "web",
        "description": description,
        "_children": children,
    }


WEB3_MENU = _directory(
    "web3-量化", "Web3Quant", "/web3-quant", "ri:bar-chart-2-line", 8,
    "Web3 量化：币安数据下载、回测、模拟盘与实盘",
    [
        _page("web3-量化-下载", "module_quant:data:query", "QuantDownload", "download",
              "module_quant/download/index", "ri:download-2-line", 1, "币安数据下载与本地 CSV 导入"),
        _page("web3-量化-图表", "module_quant:data:query", "QuantChart", "chart",
              "module_quant/chart/index", "ri:bar-chart-line", 2, "已下载数据的 K 线与成交量图表"),
        _page("web3-量化-回测", "module_quant:backtest:query", "QuantBacktest", "backtest",
              "module_quant/backtest/index", "ri:line-chart-line", 3, "策略回测配置与结果查看"),
        _page("web3-量化-模拟盘", "module_quant:live:query", "QuantPaper", "paper",
              "module_quant/paper/index", "ri:play-circle-line", 4, "虚拟资金实时策略验证"),
        _page("web3-量化-实盘", "module_quant:live:query", "QuantLive", "live",
              "module_quant/live/index", "ri:exchange-2-line", 5, "币安实盘交易（默认 dry-run）"),
    ],
)


def _market_menu(name, route_name, route_path, icon, order, description, prefix, perm_prefix):
    """构造 A股/期货市场的菜单树（五个页面）。"""
    # component 前缀必须与前端 views 目录名一致（module_quant_astock / module_quant_futures），
    # 不能由路由前缀 lower() 推导（QuantAStock -> quantastock 会丢下划线）。
    component_prefix = perm_prefix  # module_quant_astock / module_quant_futures
    specs = [
        ("data", "ri:download-2-line", "数据下载与本地 CSV 导入"),
        ("chart", "ri:bar-chart-line", "已下载数据的 K 线与成交量图表"),
        ("backtest", "ri:line-chart-line", "策略回测配置与结果查看"),
        ("paper", "ri:play-circle-line", "虚拟资金实时策略验证"),
        ("live", "ri:exchange-2-line", "实盘交易（可插拔柜台适配层）"),
    ]
    children = [
        _page(f"{name}-{key}", f"{perm_prefix}:{key}:query", f"{prefix}{key.title()}", key,
              f"{component_prefix}/{key}/index", icon, idx, desc)
        for idx, (key, icon, desc) in enumerate(specs, start=1)
    ]
    return _directory(name, route_name, route_path, icon, order, description, children)


QUANT_MENU_TREE: list[dict] = [
    WEB3_MENU,
    _market_menu("量化-A股", "QuantAStock", "/quant-astock", "ri:stock-line", 9,
                 "A 股量化：中泰 XTP 柜台适配（默认离线/模拟）", "QuantAStock", "module_quant_astock"),
    _market_menu("量化-期货", "QuantFutures", "/quant-futures", "ri:bar-chart-box-line", 10,
                 "期货量化：CTP 柜台适配（默认离线/模拟）", "QuantFutures", "module_quant_futures"),
]


async def ensure_quant_menus(db: AsyncSession) -> dict[str, int]:
    """幂等补入三套量化菜单树。

    既有库从「量化」升级到「web3-量化」时，旧菜单的 route_path=/quant 已不存在，
    因此会把旧目录及其子项软删除，再按新定义补入，避免留下重复菜单。
    """
    created = 0
    skipped = 0

    # 清理旧的「量化」目录（route_path=/quant），避免与 web3-量化 重复
    legacy = (
        await db.execute(
            select(MenuModel).where(
                MenuModel.route_path == "/quant",
                MenuModel.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    for node in legacy:
        node.is_deleted = True
        for child in (
            await db.execute(
                select(MenuModel).where(
                    MenuModel.parent_id == node.id,
                    MenuModel.is_deleted.is_(False),
                )
            )
        ).scalars().all():
            child.is_deleted = True
    if legacy:
        await db.flush()

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
            # 已存在也同步关键字段，修复早期版本写入的错误 component_path / permission
            for field in ("component_path", "permission", "route_name", "title", "redirect"):
                if payload.get(field) is not None and getattr(existing, field, None) != payload[field]:
                    setattr(existing, field, payload[field])
            skipped += 1
            parent = existing
        else:
            parent = MenuModel(**payload)
            db.add(parent)
            await db.flush()
            created += 1

        # 父节点已存在时也补齐缺失的子菜单（支持版本升级追加页面）
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
                # 同步已存在子菜单的关键字段，修正历史错误路径
                for field in ("component_path", "permission", "route_name", "title"):
                    if child.get(field) is not None and getattr(child_existing, field, None) != child[field]:
                        setattr(child_existing, field, child[field])
                skipped += 1
                continue
            db.add(MenuModel(**child, parent_id=parent.id))
            created += 1
        await db.flush()

    return {"created": created, "skipped": skipped}
