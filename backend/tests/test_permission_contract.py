"""权限码契约：后端 controller 的 AuthPermission 与菜单种子 sys_menu.json 保持一致。

背景（AGENTS.md 3.3）：前端按钮权限码必须与后端完全一致；权限变更即时生效。
本测试用 AST 静态解析所有 ``controller.py`` 中的 ``AuthPermission([...])`` 字符串
（含模块级字符串常量），与 ``backend/sql/sys_menu.json`` 中所有按钮 ``permission``
做双向集合比对，防止新增接口忘记登记菜单、或菜单写了不存在的权限码。

例外域：
- ``module_example``：示例代码，不进入菜单种子；
- ``module_quant*``：量化模块正在持续演进，其 controller 权限码可能先于菜单种子更新，
  这里只校验「controller 权限码属于已知量化域」，不做双向集合对齐；
- ``module_common``：文件上传/下载权限挂在系统管理菜单下，仍参与对齐。

中文说明见 docs/admin/07-权限与安全.md。
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
MENU_SEED = BACKEND_ROOT / "sql" / "sys_menu.json"

# 不要求与菜单种子严格对齐的域（见模块 docstring）
EXEMPT_DOMAINS = frozenset({"module_example"})

# 量化域前缀：controller 权限码允许存在，但不强制菜单即时同步
QUANT_PREFIXES = ("module_quant:", "module_quant_astock:", "module_quant_futures:")


def _module_level_string_constants(tree: ast.Module) -> dict[str, str]:
    """提取模块级 ``NAME = "字符串"`` 常量，供 ``AuthPermission([_NAME])`` 解析。"""
    consts: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _extract_permission_literals(call: ast.Call, consts: dict[str, str]) -> set[str]:
    """从一个 ``AuthPermission([...])`` 调用中提取所有权限码字符串。"""
    found: set[str] = set()
    for arg in call.args:
        if not isinstance(arg, (ast.List, ast.Tuple)):
            continue
        for element in arg.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                found.add(element.value.strip())
            elif isinstance(element, ast.Name) and element.id in consts:
                found.add(consts[element.id].strip())
    return found


def _controller_permissions() -> set[str]:
    """扫描所有 ``controller.py``，返回 ``AuthPermission`` 声明的权限码集合。"""
    permissions: set[str] = set()
    for path in APP_ROOT.rglob("controller.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        consts = _module_level_string_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AuthPermission":
                permissions |= _extract_permission_literals(node, consts)
    return permissions


def _menu_permissions() -> set[str]:
    """递归遍历菜单种子，返回所有非空 ``permission`` 字段。"""
    payload = json.loads(MENU_SEED.read_text(encoding="utf-8"))

    def walk(nodes: list[dict]) -> set[str]:
        result: set[str] = set()
        for node in nodes:
            permission = (node.get("permission") or "").strip()
            if permission:
                result.add(permission)
            result |= walk(node.get("children") or [])
        return result

    return walk(payload)


def _domain(permission: str) -> str:
    """取权限码的第一段作为域，例如 ``module_system:user:query`` → ``module_system``。"""
    return permission.split(":", 1)[0] if ":" in permission else permission


def _by_domain(permissions: set[str]) -> dict[str, set[str]]:
    """按域聚合权限码。"""
    result: dict[str, set[str]] = defaultdict(set)
    for permission in permissions:
        result[_domain(permission)].add(permission)
    return dict(result)


@pytest.fixture(scope="module")
def controller_permissions() -> set[str]:
    result = _controller_permissions()
    assert result, "未从 controller.py 解析到任何权限码，AST 解析逻辑可能失效"
    return result


@pytest.fixture(scope="module")
def menu_permissions() -> set[str]:
    result = _menu_permissions()
    assert result, "sys_menu.json 中未找到任何权限码，种子文件可能损坏"
    return result


def test_core_domains_are_bidirectionally_aligned(controller_permissions: set[str], menu_permissions: set[str]) -> None:
    """核心域：controller 权限码与菜单种子必须双向一致。"""
    ctrl_by_domain = _by_domain(controller_permissions)
    menu_by_domain = _by_domain(menu_permissions)

    core_domains = set(menu_by_domain) - EXEMPT_DOMAINS
    core_domains = {domain for domain in core_domains if not domain.startswith("module_quant")}

    assert core_domains, "没有可用于比对的核心域"
    for domain in sorted(core_domains):
        ctrl = ctrl_by_domain.get(domain, set())
        menu = menu_by_domain[domain]
        assert ctrl == menu, (
            f"域 {domain} 权限码不一致：\n"
            f"  controller 有但菜单无: {sorted(ctrl - menu)}\n"
            f"  菜单有但 controller 无: {sorted(menu - ctrl)}"
        )


def test_quant_permissions_are_in_known_domains(controller_permissions: set[str]) -> None:
    """量化域：controller 权限码必须落在已知前缀内（不强制菜单即时同步）。"""
    quant_permissions = {
        permission
        for permission in controller_permissions
        if permission.startswith("module_quant")
    }
    assert quant_permissions, "未发现任何量化权限码，扫描逻辑可能失效"
    unknown = {
        permission
        for permission in quant_permissions
        if not permission.startswith(QUANT_PREFIXES)
    }
    assert not unknown, f"发现未知量化权限码前缀: {sorted(unknown)}"


def test_no_duplicate_permission_strings_in_menu_seed() -> None:
    """菜单种子中同一权限码可挂在多处（如目录与按钮），但不得出现空域或不合法格式。"""
    for permission in _menu_permissions():
        parts = permission.split(":")
        assert len(parts) >= 2, f"权限码格式异常（缺少冒号分段）: {permission}"
        assert parts[0], f"权限码缺少域前缀: {permission}"
        assert all(part.strip() for part in parts), f"权限码含空分段: {permission}"
