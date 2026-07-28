"""远程升级：拉取新版代码并重启服务，无需登录服务器。

为什么需要：控制台走 HTTP 能读数据、切模式，但部署要执行 shell（拉代码、
装依赖、重启进程），此前只能人工登录服务器粘命令。本模块把这条通道补上。

安全边界：命令完全写死（仓库目录、分支、pip 路径均来自配置，不接受任何
请求参数），因此持有 admin_token 者能做的是「部署本仓库该分支的新提交」，
而非执行任意命令。

进程模型：重启会杀掉当前服务进程，所以升级脚本必须脱离服务的进程组
（start_new_session）在后台跑，否则会在 systemctl restart 时自杀，
留下装了一半的环境。
"""

import logging
import subprocess
from pathlib import Path

from responder.config import Settings

logger = logging.getLogger(__name__)

_SCRIPT = """#!/usr/bin/env bash
set -x
cd {repo} || exit 1
git fetch origin {branch} || exit 1
git reset --hard FETCH_HEAD || exit 1
{pip} install -q -e {repo}/lawfirm-responder || exit 1
systemctl restart responder
"""


def current_commit(repo_dir: str) -> str:
    """当前部署的提交号（短），用于远程确认升级是否真的生效。"""
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def start_update(settings: Settings) -> dict:
    """异步触发升级；立即返回，由调用方轮询 /health 的 commit 确认结果。"""
    if not settings.self_update_enabled:
        return {"ok": False, "error": "远程升级已关闭"}
    script = Path(settings.update_log).with_suffix(".sh")
    script.write_text(
        _SCRIPT.format(
            repo=settings.update_repo_dir,
            branch=settings.update_branch,
            pip=settings.update_pip,
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)
    log = open(settings.update_log, "w", encoding="utf-8")  # noqa: SIM115 (交给子进程)
    subprocess.Popen(  # noqa: S603
        ["/usr/bin/env", "bash", str(script)],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # 脱离服务进程组，重启时不会自杀
    )
    logger.info("self-update started: %s", settings.update_branch)
    return {"ok": True, "started": True, "before": current_commit(settings.update_repo_dir)}


def update_log_tail(settings: Settings, lines: int = 40) -> str:
    try:
        return "\n".join(
            Path(settings.update_log).read_text(encoding="utf-8").splitlines()[-lines:]
        )
    except OSError:
        return "（暂无升级日志）"
