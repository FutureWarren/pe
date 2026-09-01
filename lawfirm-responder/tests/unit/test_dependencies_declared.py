"""每一个第三方 import 都要写进 pyproject 的依赖里。

这条是部署那天当场抓出来的：`gateway/mp.py` 里 `import requests`，
而 `requests` 从来没写进依赖——本地能跑，是因为别的包顺带把它装上了。
干净环境里的表现是**服务在启动那一刻就炸**（`responder.app` 都 import 不进去），
而这在本地怎么测都测不出来。

它跟「静默失败」正好相反：吵得很，但**只在第一次真部署时才吵**——
也就是最不方便的那一刻。所以用一条测试把它提前到本地。
"""

import ast
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]

# 包名（pip 装的）与模块名（import 的）对不上的那几个。
MODULE_TO_DIST = {
    "Crypto": "pycryptodome",
    "pydantic_settings": "pydantic-settings",
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
}

# 本仓库自己的顶层包
LOCAL = {"responder", "tests", "scripts"}


def _declared() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    proj = data["project"]
    raw = list(proj.get("dependencies", []))
    for extra in proj.get("optional-dependencies", {}).values():
        raw += list(extra)
    out = set()
    for spec in raw:
        name = spec.split(";")[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(sep)[0]
        out.add(name.strip().lower().replace("_", "-"))
    return out


def _imported() -> dict[str, str]:
    """顶层模块名 → 第一个 import 它的文件（报错时要说清楚在哪）。"""
    found: dict[str, str] = {}
    for path in sorted((ROOT / "responder").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `from . import x` 的 module 是 None，level>0 是相对导入
                names = [node.module] if (node.module and not node.level) else []
            else:
                continue
            for full in names:
                top = full.split(".")[0]
                found.setdefault(top, str(path.relative_to(ROOT)))
    return found


def test_every_third_party_import_is_declared():
    """**干净环境里起不来，本地一点征兆都没有。**"""
    declared = _declared()
    missing = []
    for mod, where in _imported().items():
        if mod in sys.stdlib_module_names or mod in LOCAL or mod.startswith("_"):
            continue
        dist = MODULE_TO_DIST.get(mod, mod).lower().replace("_", "-")
        if dist not in declared:
            missing.append(f"{where} 里 import {mod}（需要在 pyproject 里写上 {dist}）")
    assert not missing, "这些第三方包没写进依赖：\n  " + "\n  ".join(missing)


def test_one_http_client_not_two():
    """全仓库统一用 httpx。

    两个 HTTP 客户端不只是多一个依赖：超时、代理、重试、异常类型全是两套，
    于是「网络出问题时会怎样」这个问题在不同文件里有不同答案。
    """
    assert "requests" not in _imported(), "又混进来一个 requests，请改用 httpx"
