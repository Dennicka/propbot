"""Static guard-rails preventing risky constructs from entering prod code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (REPO_ROOT / "app", REPO_ROOT / "services")
SKIP_DIR_NAMES = {"tests", "testing", "spec_archive", "__pycache__"}

ALLOW_PRINT_PATHS = {
    Path("app/cli_golden.py"),
    Path("app/tools/replay_runner.py"),
}

ALLOW_NOT_IMPLEMENTED = {
    (Path("app/market/streams/resync.py"), "_parse_snapshot"),
}

ALLOW_EXCEPTION_HANDLER_PATHS = {
    Path("app/routing/funding_router.py"),
    Path("app/analytics/pnl_attrib.py"),
    Path("app/rules/pretrade.py"),
    Path("app/config/loader.py"),
    Path("app/metrics/risk_governor.py"),
    Path("app/execution/stuck_order_resolver.py"),
    Path("app/services/autopilot.py"),
    Path("app/services/backtest_reports.py"),
    Path("app/services/positions_view.py"),
    Path("app/services/runtime.py"),
    Path("app/services/pnl_attribution.py"),
    Path("app/services/operator_dashboard.py"),
    Path("app/services/portfolio.py"),
    Path("app/ledger/pnl_sources.py"),
    Path("app/ledger/__init__.py"),
    Path("app/hedge/rebalancer.py"),
    Path("app/pnl/reporting.py"),
    Path("app/recon/daemon.py"),
    Path("app/routers/ui.py"),
    Path("app/utils/operators.py"),
    Path("services/cross_exchange_arb.py"),
}


@dataclass
class Offence:
    path: Path
    lineno: int
    message: str


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                continue
            files.append(rel)
    return files


def _load_ast(path: Path) -> ast.AST:
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    return ast.parse(source, filename=str(path))


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            return body[1:]
    return body


def _block_has_logging(body: list[ast.stmt]) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    attr = func.attr.lower()
                    if attr.startswith(
                        ("log", "warn", "error", "debug", "critical", "exception", "info")
                    ) or "log" in func.attr.lower():
                        return True
                if isinstance(func, ast.Name):
                    name = func.id.lower()
                    if name.startswith("log") or name.startswith("record"):
                        return True
    return False


def test_no_empty_pass_functions() -> None:
    offences: list[Offence] = []
    for rel_path in _iter_source_files():
        tree = _load_ast(rel_path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = _strip_docstring(node.body)
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    offences.append(
                        Offence(
                            rel_path,
                            node.lineno,
                            f"функция {node.name} содержит только pass",
                        )
                    )
    assert not offences, "Найдены заглушки pass: " + ", ".join(
        f"{item.path}:{item.lineno} {item.message}" for item in offences
    )


def test_no_not_implemented_errors() -> None:
    offences: list[Offence] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self, rel_path: Path) -> None:
            self._stack: list[str | None] = []
            self._rel_path = rel_path

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        def visit_Raise(self, node: ast.Raise) -> None:
            exc = node.exc
            if isinstance(exc, ast.Call):
                func = exc.func
            else:
                func = exc
            if isinstance(func, ast.Name) and func.id == "NotImplementedError":
                func_name = next((name for name in reversed(self._stack) if name), None)
                if (self._rel_path, func_name) not in ALLOW_NOT_IMPLEMENTED:
                    offences.append(
                        Offence(
                            self._rel_path,
                            node.lineno,
                            "raise NotImplementedError запрещён в боевом коде",
                        )
                    )

    for rel_path in _iter_source_files():
        visitor = _Visitor(rel_path)
        visitor.visit(_load_ast(rel_path))

    assert not offences, "Недопустимые NotImplementedError: " + ", ".join(
        f"{item.path}:{item.lineno}" for item in offences
    )


@pytest.mark.parametrize("token", ["TODO", "FIXME", "XXX"])
def test_no_forbidden_markers(token: str) -> None:
    offenders = []
    for rel_path in _iter_source_files():
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        if token in text:
            offenders.append(rel_path)
    assert not offenders, f"Удалите метки {token}: {', '.join(map(str, offenders))}"


def test_no_print_statements() -> None:
    offences: list[Offence] = []
    for rel_path in _iter_source_files():
        if rel_path in ALLOW_PRINT_PATHS:
            continue
        tree = _load_ast(rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "print":
                    offences.append(
                        Offence(
                            rel_path,
                            node.lineno,
                            "print запрещён в боевых путях",
                        )
                    )
    assert not offences, "Удалите print из production-кода: " + ", ".join(
        f"{item.path}:{item.lineno}" for item in offences
    )


def test_no_eval_or_exec_calls() -> None:
    offences: list[Offence] = []
    dangerous = {"eval", "exec"}
    for rel_path in _iter_source_files():
        tree = _load_ast(rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in dangerous:
                    offences.append(
                        Offence(
                            rel_path,
                            node.lineno,
                            f"вызов {node.func.id} запрещён",
                        )
                    )
    assert not offences, "Найдены eval/exec вызовы: " + ", ".join(
        f"{item.path}:{item.lineno}" for item in offences
    )


def test_no_pickle_or_unsafe_yaml_loads() -> None:
    offences: list[Offence] = []
    for rel_path in _iter_source_files():
        tree = _load_ast(rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                value = node.func.value
                if isinstance(value, ast.Name) and value.id == "pickle" and node.func.attr == "load":
                    offences.append(
                        Offence(rel_path, node.lineno, "pickle.load без audit запрещён"),
                    )
                if isinstance(value, ast.Name) and value.id == "yaml" and node.func.attr == "load":
                    offences.append(
                        Offence(rel_path, node.lineno, "используйте yaml.safe_load вместо yaml.load"),
                    )
    assert not offences, "Запрещённые pickle/yaml.load вызовы: " + ", ".join(
        f"{item.path}:{item.lineno}" for item in offences
    )


def test_except_exception_handlers_have_logging() -> None:
    offences: list[Offence] = []
    for rel_path in _iter_source_files():
        source_path = REPO_ROOT / rel_path
        lines = source_path.read_text(encoding="utf-8").splitlines()
        tree = _load_ast(rel_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                offences.append(
                    Offence(rel_path, node.lineno, "bare except запрещён")
                )
                continue
            if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                if rel_path in ALLOW_EXCEPTION_HANDLER_PATHS:
                    continue
                line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                if "#" in line:
                    continue
                if not _block_has_logging(node.body):
                    offences.append(
                        Offence(
                            rel_path,
                            node.lineno,
                            "except Exception без логирования/raise",
                        )
                    )
    assert not offences, "Добавьте логирование в except Exception: " + ", ".join(
        f"{item.path}:{item.lineno}" for item in offences
    )


def test_http_clients_have_timeouts() -> None:
    offences: list[Offence] = []
    monitored_modules = {"requests", "httpx"}
    monitored_calls = {"get", "post", "put", "delete", "head", "options", "patch"}

    for rel_path in _iter_source_files():
        tree = _load_ast(rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                value = node.func.value
                module_name: str | None = None
                if isinstance(value, ast.Name):
                    module_name = value.id
                elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                    module_name = value.value.id
                if module_name in monitored_modules and attr in monitored_calls:
                    has_timeout = any(
                        isinstance(keyword, ast.keyword) and keyword.arg == "timeout"
                        for keyword in node.keywords
                    )
                    if not has_timeout:
                        offences.append(
                            Offence(
                                rel_path,
                                node.lineno,
                                f"HTTP вызов {module_name}.{attr} без timeout",
                            )
                        )
    assert not offences, "HTTP вызовы без timeout: " + ", ".join(
        f"{item.path}:{item.lineno}" for item in offences
    )
