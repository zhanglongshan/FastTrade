"""策略自动发现：扫描 user_data/strategies/*.py 下的策略类。"""

import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from app.config.path_conf import PROJECT_ROOT, QUANT_STRATEGY_DIR
from app.core.exceptions import CustomException
from app.core.logger import logger

from .api import Strategy

# 让策略文件可以写 `from quant_api import Strategy`
_QUANT_API_MODULE: ModuleType = importlib.import_module("app.modules.quant.strategy.api")
sys.modules.setdefault("quant_api", _QUANT_API_MODULE)


@dataclass
class StrategyInfo:
    """策略描述。"""

    name: str
    file: str
    mode: str
    timeframe: str
    doc: str
    cls: type[Strategy]


def _relative(path: Path) -> str:
    """转成相对项目根目录路径。"""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _module_from_path(path: Path) -> ModuleType:
    """按文件路径加载模块，每次重新执行以便策略改动即时生效。"""
    module_name = f"fasttrade_user_strategy_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载策略文件：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _detect_mode(cls: type[Strategy]) -> str:
    """判断策略模式：事件回调或信号列。"""
    overrides_bar = cls.on_bar is not Strategy.on_bar
    overrides_entry = cls.populate_entry_trend is not Strategy.populate_entry_trend
    overrides_exit = cls.populate_exit_trend is not Strategy.populate_exit_trend
    if overrides_bar:
        return "event"
    if overrides_entry or overrides_exit:
        return "signal"
    return "unknown"


def load_strategies(directory: Path | None = None) -> list[StrategyInfo]:
    """扫描目录并返回全部可回测策略。

    参数:
    - directory: 策略目录，默认 user_data/strategies。

    返回:
    - list[StrategyInfo]: 策略描述列表，按类名排序。
    """
    root = directory or QUANT_STRATEGY_DIR
    root.mkdir(parents=True, exist_ok=True)
    strategies: list[StrategyInfo] = []

    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _module_from_path(path)
        except Exception as exc:  # noqa: BLE001 - 单个策略损坏不应阻塞其余策略
            logger.warning(f"⚠️ 策略文件加载失败 {path.name}：{exc}")
            continue

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Strategy or not issubclass(obj, Strategy):
                continue
            if obj.__module__ != module.__name__:
                continue
            mode = _detect_mode(obj)
            if mode == "unknown":
                continue
            strategies.append(
                StrategyInfo(
                    name=obj.name or obj.__name__,
                    file=_relative(path),
                    mode=mode,
                    timeframe=str(getattr(obj, "timeframe", "") or ""),
                    doc=(inspect.getdoc(obj) or "").strip(),
                    cls=obj,
                )
            )

    return sorted(strategies, key=lambda item: item.name)


def load_strategy(name: str, directory: Path | None = None) -> StrategyInfo:
    """按策略类名查找策略，找不到时抛业务异常。"""
    for info in load_strategies(directory):
        if info.name == name or info.cls.__name__ == name:
            return info
    raise CustomException(msg=f"未找到策略：{name}。请检查 user_data/strategies 目录及策略类名")
