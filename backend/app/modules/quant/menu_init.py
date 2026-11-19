"""量化菜单初始化：向既有数据库幂等补入三套量化菜单树 | Quant menu initialization: idempotently add three quant menu trees to an existing database.

新库的菜单来自 `sql/sys_menu.json`（已包含 web3-量化 / 量化-A股 / 量化-期货），
New databases get their menus from `sql/sys_menu.json` (already including web3 Quant / Quant A-share / Quant Futures).
既有库因种子数据「表已有数据则跳过」机制不会自动获得新菜单。本模块提供幂等命令。
Existing databases do not automatically receive new menus due to the seed data "skip when table is non-empty" mechanism. This module provides an idempotent command.

用法 / Usage:
    uv run python main.py quant init-menu --env=dev

主要流程 / Main flow:
1. 先软删除旧的 /quant 目录及其子项，避免与 web3-量化重复 | Soft-delete the legacy /quant directory and its children to avoid duplication with web3 Quant.
2. 遍历 QUANT_MENU_TREE，对目录与子页面执行存在即同步、缺失即创建的幂等补入 | Iterate QUANT_MENU_TREE and idempotently sync existing or create missing directories and pages.
3. 返回新建与跳过数量供 CLI 输出 | Return created and skipped counts for the CLI to print.
"""

from sqlalchemy import select  # 导入 select，用于构造 ORM 查询 | Import select to build ORM queries
from sqlalchemy.ext.asyncio import AsyncSession  # 导入异步会话类型，用于函数参数注解 | Import the async session type for function parameter annotation

from app.core.base_model import MappedBase  # 导入映射基类，供模型发现使用 | Import the mapped base class used for model discovery
from app.modules.system.menu.model import MenuModel  # 导入菜单 ORM 模型 | Import the menu ORM model
from app.utils.import_util import ImportUtil  # 导入导入工具类，用于发现并加载模型 | Import the utility used to discover and load models

# 导入全部模型，确保 MenuModel.roles 的 secondary 等跨模块关系可解析 | Import all models so cross-module relationships such as MenuModel.roles secondary can be resolved
ImportUtil.find_models(MappedBase)  # 触发模型发现与注册 | Trigger model discovery and registration


def _page(name, permission, route_name, route_path, component, icon, order, description):
    """构造页面节点 | Build a page node.

    用途 / Purpose: 把页面参数封装为菜单字典（type=2 页面）| Wrap page parameters into a menu dict (type=2 page).

    参数 / Parameters:
        name: 页面名称，同时作为标题 | Page name, also used as the title.
        permission: 权限标识字符串 | Permission identifier string.
        route_name: 前端路由名称 | Frontend route name.
        route_path: 前端路由路径 | Frontend route path.
        component: 前端组件路径 | Frontend component path.
        icon: 菜单图标标识 | Menu icon identifier.
        order: 排序序号 | Sort order number.
        description: 菜单描述 | Menu description.

    返回 / Returns:
        表示页面节点的字典 | A dict representing the page node.

    流程 / Logic: 返回包含固定 type=2、status=0、keep_alive=True 等默认值的字典 | Return a dict with fixed defaults such as type=2, status=0 and keep_alive=True.
    """
    return {  # 构造并返回页面字典 | Build and return the page dict
        "name": name,  # 页面名称 | Page name
        "type": 2,  # 节点类型 2 表示页面 | Node type 2 means page
        "icon": icon,  # 菜单图标 | Menu icon
        "order": order,  # 排序序号 | Sort order
        "permission": permission,  # 权限标识 | Permission identifier
        "route_name": route_name,  # 路由名称 | Route name
        "route_path": route_path,  # 路由路径 | Route path
        "component_path": component,  # 组件路径 | Component path
        "status": 0,  # 状态 0，正常显示 | Status 0, shown normally
        "keep_alive": True,  # 是否缓存页面组件 | Whether to keep the page component alive
        "hidden": False,  # 是否隐藏菜单 | Whether the menu is hidden
        "always_show": False,  # 是否总是显示 | Whether to always show
        "title": name,  # 显示标题，与名称相同 | Display title, same as name
        "affix": False,  # 是否固定在标签栏 | Whether to affix to the tab bar
        "redirect": None,  # 页面无重定向 | No redirect for a page
        "description": description,  # 菜单描述 | Menu description
    }  # 页面字典构造结束 | End of the page dict


def _directory(name, route_name, route_path, icon, order, description, children):
    """构造目录节点 | Build a directory node.

    用途 / Purpose: 把目录参数与子菜单封装为菜单字典（type=1 目录）| Wrap directory parameters and children into a menu dict (type=1 directory).

    参数 / Parameters:
        name: 目录名称，同时作为标题 | Directory name, also used as the title.
        route_name: 前端路由名称 | Frontend route name.
        route_path: 前端路由路径 | Frontend route path.
        icon: 菜单图标标识 | Menu icon identifier.
        order: 排序序号 | Sort order number.
        description: 菜单描述 | Menu description.
        children: 子页面节点列表 | List of child page nodes.

    返回 / Returns:
        表示目录节点的字典，含 _children 内部键 | A dict representing the directory node, with an internal _children key.

    流程 / Logic: 返回 type=1、always_show=True 的字典，重定向指向第一个子项，并用 _children 暂存子菜单 | Return a type=1, always_show=True dict whose redirect points to the first child and which stores children under _children.
    """
    return {  # 构造并返回目录字典 | Build and return the directory dict
        "name": name,  # 目录名称 | Directory name
        "type": 1,  # 节点类型 1 表示目录 | Node type 1 means directory
        "icon": icon,  # 菜单图标 | Menu icon
        "order": order,  # 排序序号 | Sort order
        "permission": None,  # 目录本身不绑定权限 | Directory itself has no permission
        "route_name": route_name,  # 路由名称 | Route name
        "route_path": route_path,  # 路由路径 | Route path
        "component_path": None,  # 目录无组件路径 | Directory has no component path
        "status": 0,  # 状态 0，正常显示 | Status 0, shown normally
        "keep_alive": True,  # 是否缓存 | Whether to keep alive
        "hidden": False,  # 是否隐藏 | Whether hidden
        "always_show": True,  # 目录总是显示 | Directory always shown
        "title": name,  # 显示标题 | Display title
        "affix": False,  # 是否固定标签 | Whether affixed
        "redirect": f"{route_path}/{children[0]['route_path']}",  # 重定向到第一个子项路径 | Redirect to the first child's route path
        "scope": "web",  # 作用域为 web | Scope is web
        "description": description,  # 菜单描述 | Menu description
        "_children": children,  # 内部键，保存子菜单供后续入库使用 | Internal key holding child menus for later persistence
    }  # 目录字典构造结束 | End of the directory dict


WEB3_MENU = _directory(  # 构造 web3-量化菜单树 | Build the web3 Quant menu tree
    "web3-量化", "Web3Quant", "/web3-quant", "ri:bar-chart-2-line", 8,  # 目录基础参数：名称、路由名、路径、图标、排序 | Directory base params: name, route name, path, icon, order
    "Web3 量化：币安数据下载、回测、模拟盘与实盘",  # 目录描述 | Directory description
    [  # 子页面列表 | List of child pages
        _page("web3-量化-下载", "module_quant:data:query", "QuantDownload", "download",  # 下载页面：名称、权限、路由名、路径 | Download page: name, permission, route name, path
              "module_quant/download/index", "ri:download-2-line", 1, "币安数据下载与本地 CSV 导入"),  # 下载页面：组件、图标、排序、描述 | Download page: component, icon, order, description
        _page("web3-量化-图表", "module_quant:data:query", "QuantChart", "chart",  # 图表页面：名称、权限、路由名、路径 | Chart page: name, permission, route name, path
              "module_quant/chart/index", "ri:bar-chart-line", 2, "已下载数据的 K 线与成交量图表"),  # 图表页面：组件、图标、排序、描述 | Chart page: component, icon, order, description
        _page("web3-量化-回测", "module_quant:backtest:query", "QuantBacktest", "backtest",  # 回测页面：名称、权限、路由名、路径 | Backtest page: name, permission, route name, path
              "module_quant/backtest/index", "ri:line-chart-line", 3, "策略回测配置与结果查看"),  # 回测页面：组件、图标、排序、描述 | Backtest page: component, icon, order, description
        _page("web3-量化-模拟盘", "module_quant:live:query", "QuantPaper", "paper",  # 模拟盘页面：名称、权限、路由名、路径 | Paper page: name, permission, route name, path
              "module_quant/paper/index", "ri:play-circle-line", 4, "虚拟资金实时策略验证"),  # 模拟盘页面：组件、图标、排序、描述 | Paper page: component, icon, order, description
        _page("web3-量化-实盘", "module_quant:live:query", "QuantLive", "live",  # 实盘页面：名称、权限、路由名、路径 | Live page: name, permission, route name, path
              "module_quant/live/index", "ri:exchange-2-line", 5, "币安实盘交易（默认 dry-run）"),  # 实盘页面：组件、图标、排序、描述 | Live page: component, icon, order, description
    ],  # 子页面列表结束 | End of the child page list
)  # web3-量化菜单树构造结束 | End of the web3 Quant menu tree


def _market_menu(name, route_name, route_path, icon, order, description, prefix, perm_prefix):
    """构造 A股/期货市场的菜单树（五个页面） | Build a market menu tree for A-share or futures (five pages).

    用途 / Purpose: 按市场参数生成一套结构统一的量化菜单树 | Generate a uniformly structured quant menu tree from market parameters.

    参数 / Parameters:
        name: 菜单名称，同时作为标题 | Menu name, also used as the title.
        route_name: 前端路由名称 | Frontend route name.
        route_path: 前端路由路径 | Frontend route path.
        icon: 菜单图标标识 | Menu icon identifier.
        order: 排序序号 | Sort order number.
        description: 菜单描述 | Menu description.
        prefix: 子路由名称前缀（如 QuantAStock）| Child route name prefix (e.g. QuantAStock).
        perm_prefix: 权限与组件路径前缀（如 module_quant_astock）| Permission and component path prefix (e.g. module_quant_astock).

    返回 / Returns:
        表示市场目录节点的字典，含五个子页面 | A dict representing the market directory node with five child pages.

    流程 / Logic: 根据 specs 列表推导生成五个子页面，再交给 _directory 组装为目录 | Derive five child pages from the specs list comprehension, then assemble them into a directory via _directory.
    """
    # component 前缀必须与前端 views 目录名一致（module_quant_astock / module_quant_futures）， | The component prefix must match the frontend views directory name (module_quant_astock / module_quant_futures),
    # 不能由路由前缀 lower() 推导（QuantAStock -> quantastock 会丢下划线）。 | it cannot be derived by lowercasing the route prefix (QuantAStock -> quantastock loses the underscore).
    component_prefix = perm_prefix  # 组件路径前缀，取值 module_quant_astock / module_quant_futures | Component path prefix, one of module_quant_astock / module_quant_futures
    specs = [  # 五个子页面的规格列表：键、图标、描述 | Spec list for the five child pages: key, icon, description
        ("data", "ri:download-2-line", "数据下载与本地 CSV 导入"),  # 数据下载页面规格 | Data download page spec
        ("chart", "ri:bar-chart-line", "已下载数据的 K 线与成交量图表"),  # 图表页面规格 | Chart page spec
        ("backtest", "ri:line-chart-line", "策略回测配置与结果查看"),  # 回测页面规格 | Backtest page spec
        ("paper", "ri:play-circle-line", "虚拟资金实时策略验证"),  # 模拟盘页面规格 | Paper page spec
        ("live", "ri:exchange-2-line", "实盘交易（可插拔柜台适配层）"),  # 实盘页面规格 | Live page spec
    ]  # 子页面规格列表结束 | End of the child page spec list
    children = [  # 由规格列表推导生成子页面节点 | Build child page nodes from the spec list comprehension
        _page(f"{name}-{key}", f"{perm_prefix}:{key}:query", f"{prefix}{key.title()}", key,  # 页面名称、权限、路由名与路径 | Page name, permission, route name and path
              f"{component_prefix}/{key}/index", icon, idx, desc)  # 组件路径、图标、排序与描述 | Component path, icon, order and description
        for idx, (key, icon, desc) in enumerate(specs, start=1)  # 从 1 开始枚举规格作为排序序号 | Enumerate specs from 1 as the sort order
    ]  # 子页面节点列表结束 | End of the child page node list
    return _directory(name, route_name, route_path, icon, order, description, children)  # 组装并返回市场目录节点 | Assemble and return the market directory node


QUANT_MENU_TREE: list[dict] = [  # 三套量化菜单树常量列表 | Constant list of the three quant menu trees
    WEB3_MENU,  # web3-量化菜单树 | Web3 Quant menu tree
    _market_menu("量化-A股", "QuantAStock", "/quant-astock", "ri:stock-line", 9,  # A 股菜单：名称、路由名、路径、图标、排序 | A-share menu: name, route name, path, icon, order
                 "A 股量化：中泰 XTP 柜台适配（默认离线/模拟）", "QuantAStock", "module_quant_astock"),  # A 股菜单：描述、前缀、权限前缀 | A-share menu: description, prefix, permission prefix
    _market_menu("量化-期货", "QuantFutures", "/quant-futures", "ri:bar-chart-box-line", 10,  # 期货菜单：名称、路由名、路径、图标、排序 | Futures menu: name, route name, path, icon, order
                 "期货量化：CTP 柜台适配（默认离线/模拟）", "QuantFutures", "module_quant_futures"),  # 期货菜单：描述、前缀、权限前缀 | Futures menu: description, prefix, permission prefix
]  # 三套菜单树列表结束 | End of the three menu tree list


async def ensure_quant_menus(db: AsyncSession) -> dict[str, int]:
    """幂等补入三套量化菜单树 | Idempotently add the three quant menu trees.

    用途 / Purpose: 把量化菜单树以幂等方式写入既有数据库，可重复执行 | Write the quant menu trees into an existing database idempotently, safe to re-run.

    参数 / Parameters:
        db: 异步数据库会话，用于查询与写入菜单 | The async database session used to query and write menus.

    返回 / Returns:
        包含 created 与 skipped 计数的字典 | A dict with created and skipped counts.

    流程 / Logic: 先软删除旧 /quant 目录及子项，再遍历菜单树，对目录与子页面存在则同步关键字段、缺失则创建，最后返回计数 | Soft-delete the legacy /quant directory and children, then iterate the menu trees syncing existing or creating missing directories and pages, and finally return counts.
    """
    created = 0  # 新建菜单计数器 | Counter for created menus
    skipped = 0  # 已存在跳过计数器 | Counter for skipped (already existing) menus

    # 清理旧的「量化」目录（route_path=/quant），避免与 web3-量化 重复 | Clean up the legacy Quant directory (route_path=/quant) to avoid duplication with web3 Quant
    legacy = (  # 查询旧的 /quant 未删除目录列表 | Query the list of non-deleted legacy /quant directories
        await db.execute(  # 执行查询 | Execute the query
            select(MenuModel).where(  # 构造菜单查询条件 | Build the menu query conditions
                MenuModel.route_path == "/quant",  # 匹配旧目录路径 | Match the legacy directory path
                MenuModel.is_deleted.is_(False),  # 仅未删除记录 | Only non-deleted records
            )  # 查询条件结束 | End of query conditions
        )  # 查询执行结束 | End of query execution
    ).scalars().all()  # 取回全部匹配行 | Retrieve all matching rows
    for node in legacy:  # 遍历每个旧目录节点 | Iterate over each legacy directory node
        node.is_deleted = True  # 软删除旧目录 | Soft-delete the legacy directory
        for child in (  # 查询该目录的未删除子项 | Query the non-deleted children of this directory
            await db.execute(  # 执行查询 | Execute the query
                select(MenuModel).where(  # 构造子项查询条件 | Build the child query conditions
                    MenuModel.parent_id == node.id,  # 匹配父节点 ID | Match the parent node ID
                    MenuModel.is_deleted.is_(False),  # 仅未删除记录 | Only non-deleted records
                )  # 查询条件结束 | End of query conditions
            )  # 查询执行结束 | End of query execution
        ).scalars().all():  # 取回全部匹配子行 | Retrieve all matching child rows
            child.is_deleted = True  # 软删除子菜单 | Soft-delete the child menu
    if legacy:  # 若存在旧目录需要处理 | If any legacy directory was processed
        await db.flush()  # 刷新会话使软删除生效 | Flush the session to apply the soft deletions

    for node in QUANT_MENU_TREE:  # 遍历三套菜单树 | Iterate over the three menu trees
        children = node.get("_children", [])  # 取出子菜单列表，默认空列表 | Extract the child menu list, defaulting to empty
        payload = {k: v for k, v in node.items() if k != "_children"}  # 排除内部键 _children 得到入库字段 | Exclude the internal _children key to get persistence fields

        existing = (  # 按路由路径查询已存在的目录节点 | Query an existing directory node by route path
            await db.execute(  # 执行查询 | Execute the query
                select(MenuModel).where(  # 构造目录查询条件 | Build the directory query conditions
                    MenuModel.route_path == payload["route_path"],  # 匹配目录路由路径 | Match the directory route path
                    MenuModel.is_deleted.is_(False),  # 仅未删除记录 | Only non-deleted records
                )  # 查询条件结束 | End of query conditions
            )  # 查询执行结束 | End of query execution
        ).scalar_one_or_none()  # 取回至多一个结果 | Retrieve at most one result

        if existing:  # 若目录已存在 | If the directory already exists
            # 已存在也同步关键字段，修复早期版本写入的错误 component_path / permission | Sync key fields even when it exists, fixing wrong component_path / permission written by early versions
            for field in ("component_path", "permission", "route_name", "title", "redirect"):  # 遍历需要同步的关键字段 | Iterate over key fields to sync
                if payload.get(field) is not None and getattr(existing, field, None) != payload[field]:  # 仅当新值非空且与旧值不同时更新 | Update only when the new value is non-null and differs from the old one
                    setattr(existing, field, payload[field])  # 同步字段值到已存在记录 | Sync the field value onto the existing record
            skipped += 1  # 已存在计数加一 | Increment the skipped counter
            parent = existing  # 复用已存在目录作为父节点 | Reuse the existing directory as the parent
        else:  # 若目录不存在 | If the directory does not exist
            parent = MenuModel(**payload)  # 由字段构造新目录 ORM 对象 | Build a new directory ORM object from the fields
            db.add(parent)  # 将新目录加入会话 | Add the new directory to the session
            await db.flush()  # 刷新会话以获取主键 | Flush the session to obtain the primary key
            created += 1  # 新建计数加一 | Increment the created counter

        # 父节点已存在时也补齐缺失的子菜单（支持版本升级追加页面） | Also backfill missing child menus when the parent exists (supports adding pages on upgrade)
        for child in children:  # 遍历每个子页面 | Iterate over each child page
            child_existing = (  # 按路由路径与父 ID 查询已存在的子菜单 | Query an existing child menu by route path and parent ID
                await db.execute(  # 执行查询 | Execute the query
                    select(MenuModel).where(  # 构造子菜单查询条件 | Build the child menu query conditions
                        MenuModel.route_path == child["route_path"],  # 匹配子菜单路由路径 | Match the child route path
                        MenuModel.parent_id == parent.id,  # 匹配父节点 ID | Match the parent node ID
                        MenuModel.is_deleted.is_(False),  # 仅未删除记录 | Only non-deleted records
                    )  # 查询条件结束 | End of query conditions
                )  # 查询执行结束 | End of query execution
            ).scalar_one_or_none()  # 取回至多一个结果 | Retrieve at most one result
            if child_existing:  # 若子菜单已存在 | If the child menu already exists
                # 同步已存在子菜单的关键字段，修正历史错误路径 | Sync key fields of the existing child menu, fixing historical wrong paths
                for field in ("component_path", "permission", "route_name", "title"):  # 遍历需要同步的关键字段 | Iterate over key fields to sync
                    if child.get(field) is not None and getattr(child_existing, field, None) != child[field]:  # 仅当新值非空且与旧值不同时更新 | Update only when the new value is non-null and differs from the old one
                        setattr(child_existing, field, child[field])  # 同步字段值到已存在子菜单 | Sync the field value onto the existing child menu
                skipped += 1  # 已存在计数加一 | Increment the skipped counter
                continue  # 跳过创建，处理下一个子菜单 | Skip creation and move to the next child
            db.add(MenuModel(**child, parent_id=parent.id))  # 构造并加入新子菜单，绑定父 ID | Build and add a new child menu bound to the parent ID
            created += 1  # 新建计数加一 | Increment the created counter
        await db.flush()  # 刷新会话使本轮菜单变更生效 | Flush the session to apply this round of menu changes

    return {"created": created, "skipped": skipped}  # 返回新建与跳过计数 | Return the created and skipped counts
