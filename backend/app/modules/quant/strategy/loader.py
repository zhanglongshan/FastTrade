"""策略自动发现：扫描 user_data/strategies/*.py 下的策略类。| Strategy auto-discovery: scan strategy classes under user_data/strategies/*.py.

目的 | Purpose:
    动态加载用户目录中的策略文件，识别继承自 Strategy 的有效策略类，
    并以 StrategyInfo 描述返回，供 API 列举与回测按名称加载。
    Dynamically load user strategy files, identify valid classes inheriting from Strategy,
    and return them as StrategyInfo descriptors for API listing and name-based backtest loading.

主要流程 | Main flow:
    1. 将 api 模块注册为 quant_api 别名，方便策略文件导入 | Register the api module as the quant_api alias for convenient imports in strategy files.
    2. 扫描目录下所有 .py 文件并动态执行 | Scan all .py files under the directory and execute them dynamically.
    3. 用反射找出 Strategy 子类并判断模式 | Use reflection to find Strategy subclasses and detect their mode.
    4. 按名称排序返回，或按名称精确查找 | Return name-sorted results, or look up a strategy by exact name.
"""

import importlib  # 导入 importlib | Import importlib for importing modules by name
import importlib.util  # 导入 importlib.util | Import importlib.util for loading modules from file paths
import inspect  # 导入 inspect | Import inspect for reflection over classes and docstrings
import sys  # 导入 sys | Import sys for the global module registry
from dataclasses import dataclass  # 导入 dataclass 装饰器 | Import dataclass decorator for the StrategyInfo container
from pathlib import Path  # 导入 Path | Import Path for filesystem path operations
from types import ModuleType  # 导入 ModuleType | Import ModuleType for module type annotations

from app.config.path_conf import PROJECT_ROOT, QUANT_STRATEGY_DIR  # 导入项目根目录与策略目录常量 | Import project root and strategy directory constants
from app.core.exceptions import CustomException  # 导入业务异常类 | Import the custom business exception class
from app.core.logger import logger  # 导入日志器 | Import the logger for warnings

from .api import Strategy  # 导入策略基类 | Import the Strategy base class

# 让策略文件可以写 `from quant_api import Strategy` | Allow strategy files to write `from quant_api import Strategy`
_QUANT_API_MODULE: ModuleType = importlib.import_module("app.modules.quant.strategy.api")  # 导入 api 模块作为别名目标 | Import the api module as the alias target
sys.modules.setdefault("quant_api", _QUANT_API_MODULE)  # 注册别名（若未存在）| Register the alias if not already present


@dataclass  # 可变数据类：描述一条发现到的策略 | Mutable dataclass describing one discovered strategy
class StrategyInfo:
    """策略描述 | Strategy descriptor.

    目的 | Purpose:
        封装一个被发现的策略类的元信息，供 API 序列化与回测加载。
        Encapsulate metadata of a discovered strategy class for API serialization and backtest loading.

    关键属性 | Key attributes:
        - name: 策略显示名 | Display name.
        - file: 相对项目根的路径 | Path relative to project root.
        - mode: 模式（event/signal/unknown）| Mode (event/signal/unknown).
        - timeframe: 交易周期 | Trading timeframe.
        - doc: 类文档字符串 | Class docstring.
        - cls: 策略类对象本身 | The strategy class object itself.
    """

    name: str  # 策略显示名称 | Strategy display name
    file: str  # 策略文件相对路径 | Strategy file relative path
    mode: str  # 策略运行模式 | Strategy mode
    timeframe: str  # 策略周期 | Strategy timeframe
    doc: str  # 策略文档字符串 | Strategy documentation string
    cls: type[Strategy]  # 策略类对象 | The strategy class object


def _relative(path: Path) -> str:  # 转为相对路径 | Convert to a relative path
    """转成相对项目根目录路径。| Convert a path to one relative to the project root.

    Purpose:
        将绝对路径转为相对项目根的路径，便于展示；越界时回退为绝对路径。
        Convert an absolute path to a path relative to the project root for display; fall back to absolute if outside.
    Parameters:
        - path: 待转换路径 | Path to convert.
    Returns:
        str: 相对路径字符串 | Relative path string.
    Flow:
        尝试 relative_to，成功返回相对串；ValueError 时返回绝对串 | Try relative_to and return relative string; on ValueError return absolute string.
    """
    try:  # 尝试相对化 | Attempt relativization
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))  # 相对项目根的路径 | Path relative to project root
    except ValueError:  # 不在项目根内 | Path is outside the project root
        return str(path.resolve())  # 回退为绝对路径 | Fall back to absolute path


def _module_from_path(path: Path) -> ModuleType:  # 按路径加载模块 | Load a module from a file path
    """按文件路径加载模块，每次重新执行以便策略改动即时生效。| Load a module from a file path, re-executing each time so strategy edits take effect immediately.

    Purpose:
        为策略文件生成唯一模块名并动态执行，返回模块对象。
        Generate a unique module name for the strategy file, execute it dynamically and return the module object.
    Parameters:
        - path: 策略文件路径 | Strategy file path.
    Returns:
        ModuleType: 执行后的模块对象 | The executed module object.
    Flow:
        生成唯一名 -> 创建 spec -> 校验 loader -> 建模块并注册到 sys.modules -> 执行代码 -> 返回 | Generate unique name -> create spec -> validate loader -> create module and register in sys.modules -> execute code -> return.
    """
    module_name = f"fasttrade_user_strategy_{path.stem}_{abs(hash(str(path)))}"  # 基于文件名与路径哈希的唯一模块名 | Unique module name from file stem and path hash
    spec = importlib.util.spec_from_file_location(module_name, path)  # 从文件路径创建模块规格 | Create module spec from file path
    if spec is None or spec.loader is None:  # 校验规格与 loader 可用 | Validate spec and loader are available
        raise ImportError(f"无法加载策略文件：{path}")  # 加载失败抛异常 | Raise error when loading fails
    module = importlib.util.module_from_spec(spec)  # 由规格创建模块对象 | Create module object from spec
    sys.modules[module_name] = module  # 注册到 sys.modules | Register in sys.modules
    spec.loader.exec_module(module)  # 执行模块代码 | Execute the module code
    return module  # 返回加载后的模块 | Return the loaded module


def _detect_mode(cls: type[Strategy]) -> str:  # 判断策略模式 | Detect strategy mode
    """判断策略模式：事件回调或信号列。| Detect the strategy mode: event callback or signal columns.

    Purpose:
        通过比较子类方法是否覆盖基类方法，判断策略采用的模式。
        Determine the mode by checking which base methods the subclass overrides.
    Parameters:
        - cls: 策略类 | Strategy class.
    Returns:
        str: "event" / "signal" / "unknown" 之一 | One of "event" / "signal" / "unknown".
    Flow:
        分别检测三个回调是否被覆盖，按优先级返回事件/信号/未知 | Detect whether each callback is overridden, then return event/signal/unknown by priority.
    """
    overrides_bar = cls.on_bar is not Strategy.on_bar  # 是否覆盖 on_bar | Whether on_bar is overridden
    overrides_entry = cls.populate_entry_trend is not Strategy.populate_entry_trend  # 是否覆盖开仓信号 | Whether entry signal is overridden
    overrides_exit = cls.populate_exit_trend is not Strategy.populate_exit_trend  # 是否覆盖平仓信号 | Whether exit signal is overridden
    if overrides_bar:  # 事件模式优先 | Event mode takes priority
        return "event"  # 返回事件模式 | Return event mode
    if overrides_entry or overrides_exit:  # 任意信号列覆盖 | Any signal-column override
        return "signal"  # 返回信号模式 | Return signal mode
    return "unknown"  # 无有效覆盖，返回未知 | No valid override, return unknown


def load_strategies(directory: Path | None = None) -> list[StrategyInfo]:  # 扫描目录加载全部策略 | Scan a directory and load all strategies
    """扫描目录并返回全部可回测策略。| Scan a directory and return all backtestable strategies.

    参数 | Parameters:
        - directory: 策略目录，默认 user_data/strategies | Strategy directory, defaults to user_data/strategies.

    返回 | Returns:
        - list[StrategyInfo]: 策略描述列表，按类名排序 | List of strategy descriptors sorted by class name.

    Flow:
        确保目录存在 -> 遍历所有 .py 文件并动态加载 -> 反射出 Strategy 子类 -> 按模式过滤 -> 组装并排序返回 | Ensure directory exists -> iterate and load all .py files -> reflect Strategy subclasses -> filter by mode -> assemble and sort results.
    """
    root = directory or QUANT_STRATEGY_DIR  # 确定扫描根目录 | Determine the scan root directory
    root.mkdir(parents=True, exist_ok=True)  # 确保目录存在（幂等）| Ensure the directory exists (idempotent)
    strategies: list[StrategyInfo] = []  # 收集发现的策略 | Collect discovered strategies

    for path in sorted(root.rglob("*.py")):  # 递归遍历所有 .py 文件（排序保证确定性）| Recursively iterate all .py files (sorted for determinism)
        if path.name.startswith("_"):  # 跳过下划线开头文件（如 __init__.py）| Skip files starting with underscore (e.g. __init__.py)
            continue  # 跳过该文件 | Skip this file
        try:  # 尝试加载单个策略文件 | Try loading a single strategy file
            module = _module_from_path(path)  # 动态加载为模块 | Dynamically load as a module
        except Exception as exc:  # noqa: BLE001 - 单个策略损坏不应阻塞其余策略 | noqa: BLE001 - one broken strategy must not block the rest
            logger.warning(f"⚠️ 策略文件加载失败 {path.name}：{exc}")  # 记录加载失败警告 | Log a load-failure warning
            continue  # 跳过损坏文件 | Skip the broken file

        for _, obj in inspect.getmembers(module, inspect.isclass):  # 反射模块中的所有类 | Reflect over all classes in the module
            if obj is Strategy or not issubclass(obj, Strategy):  # 排除基类与非子类 | Exclude the base class and non-subclasses
                continue  # 跳过该类 | Skip this class
            if obj.__module__ != module.__name__:  # 排除从别处导入的类 | Exclude classes imported from elsewhere
                continue  # 跳过该类 | Skip this class
            mode = _detect_mode(obj)  # 判断策略模式 | Detect the strategy mode
            if mode == "unknown":  # 跳过模式未知的类 | Skip classes with unknown mode
                continue  # 跳过该类 | Skip this class
            strategies.append(  # 追加策略描述 | Append the strategy descriptor
                StrategyInfo(  # 构造策略描述对象 | Construct the StrategyInfo object
                    name=obj.name or obj.__name__,  # 显示名优先用 name 属性，否则类名 | Use name attribute or fall back to class name
                    file=_relative(path),  # 相对项目根的文件路径 | File path relative to project root
                    mode=mode,  # 策略模式 | Strategy mode
                    timeframe=str(getattr(obj, "timeframe", "") or ""),  # 周期属性转字符串，缺省为空 | Convert timeframe attribute to string, default empty
                    doc=(inspect.getdoc(obj) or "").strip(),  # 取类文档并去空白 | Get class docstring and strip whitespace
                    cls=obj,  # 保存策略类对象 | Store the strategy class object
                )  # 策略描述构造结束 | End of StrategyInfo construction
            )  # 追加结束 | End of append

    return sorted(strategies, key=lambda item: item.name)  # 按名称排序后返回 | Return sorted by name


def load_strategy(name: str, directory: Path | None = None) -> StrategyInfo:  # 按名称查找策略 | Look up a strategy by name
    """按策略类名查找策略，找不到时抛业务异常。| Look up a strategy by class name, raising a business exception if not found.

    Purpose:
        在已发现的策略中查找指定名称，返回其描述。
        Find a strategy by name among discovered strategies and return its descriptor.
    Parameters:
        - name: 策略显示名或类名 | Strategy display name or class name.
        - directory: 策略目录，默认 user_data/strategies | Strategy directory, defaults to user_data/strategies.
    Returns:
        StrategyInfo: 匹配的策略描述 | The matching strategy descriptor.
    Flow:
        遍历所有策略 -> 名称或类名匹配则返回 -> 遍历结束未找到则抛异常 | Iterate all strategies -> return on name or class-name match -> raise exception if none found.
    """
    for info in load_strategies(directory):  # 遍历已发现的策略 | Iterate discovered strategies
        if info.name == name or info.cls.__name__ == name:  # 名称或类名匹配 | Match by display name or class name
            return info  # 返回匹配的策略 | Return the matching strategy
    raise CustomException(msg=f"未找到策略：{name}。请检查 user_data/strategies 目录及策略类名")  # 未找到抛业务异常 | Raise business exception when not found
