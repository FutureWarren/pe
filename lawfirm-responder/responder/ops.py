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
from datetime import datetime
from pathlib import Path

from responder.config import Settings

logger = logging.getLogger(__name__)

# 升级脚本。**它必须能把自己撤回来。**
#
# 律所侧没有 SSH，而控制台和后台线程跑在同一个进程里。一旦新版本起不来：
# 服务死 → 控制台打不开（那个「升级到最新版」按钮也就没了）→ 后台线程不跑
# → 自动升级不跑 → **我们再也推不进任何修复**。服务器就此永久失联，
# 而修复它需要有人物理接触那台机器。
#
# 而「推送即上线」意味着这条路每天都要走好几趟。所以三道闸：
#   ① 装完先在**另一个进程里**把 app 装配一遍（配置校验、数据库迁移、
#      import 全在这一步暴露）——起不来就不重启，直接回滚；
#   ② 重启后轮询 /health，60 秒内不通同样回滚并再重启回旧版；
#   ③ 全过程写进升级日志，控制台「状态」页读得到。
#
# 宁可停在旧版本，也不能停在一个起不来的新版本上。
_SCRIPT = """#!/usr/bin/env bash
set -x
cd {repo} || exit 1
PREV=$(git rev-parse HEAD) || exit 1
echo "[update] 当前版本 $PREV"

rollback() {{
  echo "[update] !!! 回滚到 $PREV"
  git reset --hard "$PREV" || exit 1
  {pip} install -q -e {repo}/lawfirm-responder
  systemctl restart responder
  exit 1
}}

git fetch origin {branch} || exit 1
git reset --hard FETCH_HEAD || exit 1
{pip} install -q -e {repo}/lawfirm-responder || rollback

# ① 起不来的版本绝不能上线：在另一个进程里把 app 完整装配一遍。
#    配置校验、数据库迁移、所有 import 都在这一步暴露。
echo "[update] 冒烟：装配 app"
{python} -c 'from responder.app import create_app; create_app()' || rollback

systemctl restart responder

# ② 重启后必须真的活过来。60 秒不通就滚回旧版并重启回去。
echo "[update] 等待 /health"
for i in $(seq 1 30); do
  sleep 2
  if curl -fsS -m 3 "http://127.0.0.1:{port}/health" >/dev/null 2>&1; then
    echo "[update] OK，新版本已在服务"
    exit 0
  fi
done
echo "[update] /health 60 秒内没通"
rollback
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
            python=settings.update_python,
            port=settings.api_port,
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)
    # 追加而不是覆盖。覆盖的话日志里永远只有最后一次，
    # 而「服务器每 5 分钟重启两次、永远出不来」这种循环恰恰只能从多次记录里看出来。
    log = open(settings.update_log, "a", encoding="utf-8")  # noqa: SIM115 (交给子进程)
    log.write(f"\n===== {datetime.now().isoformat()} 开始升级 =====\n")
    log.flush()
    subprocess.Popen(  # noqa: S603
        ["/usr/bin/env", "bash", str(script)],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # 脱离服务进程组，重启时不会自杀
    )
    logger.info("self-update started: %s", settings.update_branch)
    return {"ok": True, "started": True, "before": current_commit(settings.update_repo_dir)}


def remote_commit(settings: Settings) -> str:
    """远端分支最新提交号（短）。fetch 失败返回空串（当作「没有更新」处理）。

    只 fetch 不 merge：这里只负责「看一眼有没有新版」，真正的更新交给
    start_update 那套已经验证过的脚本，不在这里重复一遍拉取逻辑。
    """
    repo, branch = settings.update_repo_dir, settings.update_branch
    try:
        fetched = subprocess.run(  # noqa: S603
            ["git", "-C", repo, "fetch", "--quiet", "origin", branch],
            capture_output=True, text=True, timeout=60,
        )
        if fetched.returncode != 0:
            logger.warning("auto-update fetch failed: %s", fetched.stderr.strip()[:200])
            return ""
        out = subprocess.run(  # noqa: S603
            ["git", "-C", repo, "rev-parse", "--short", "FETCH_HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        logger.exception("auto-update fetch error")
        return ""


def auto_update_tick(settings: Settings, *, busy: bool = False, store=None) -> dict:
    """自动升级：发现远端有新提交就自己拉下来重启。

    存在的理由很实在：运维侧不一定够得着这台服务器（网络策略/没有 SSH），
    但服务器自己够得着 GitHub。与其让人去点按钮，不如让它自己看。

    `busy` 由调用方判断（队列非空 / 客户刚说过话）。重启会丢掉内存队列里
    尚未处理的消息，所以宁可等下一轮——升级晚五分钟没关系，客户的消息掉了有关系。
    """
    if not (settings.self_update_enabled and settings.auto_update_enabled):
        return {"checked": False, "reason": "disabled"}
    if busy:
        return {"checked": False, "reason": "busy"}
    local = current_commit(settings.update_repo_dir)
    remote = remote_commit(settings)
    if not remote or remote == local:
        return {"checked": True, "updated": False, "commit": local}
    # **这个版本刚试过、并且失败回滚了，就别再试。**
    # 回滚做的正是 `git reset --hard $PREV`，于是 local != remote 立刻又成立，
    # 五分钟后原样再来一遍：服务器每 5 分钟重启两次、每次一分多钟不可用，
    # 而且**永远出不来**。这些窗口里企微回调没人应答（重试几次就放弃）、
    # 内存队列里的消息全丢，整个过程零告警——律所侧只会隐约觉得
    # 「这两天怎么好些人没回」。
    if store is not None and _failed_before(store, remote):
        return {"checked": True, "updated": False, "commit": local,
                "reason": f"{remote[:8]} 上次起不来，已跳过"}
    logger.info("auto-update: %s → %s", local or "?", remote)
    result = start_update(settings)
    return {"checked": True, "updated": bool(result.get("ok")), "from": local, "to": remote}


_FAILED_NOTE = "update_failed_sha"


def _failed_before(store, sha: str) -> bool:
    try:
        return sha in (store.get_note(_FAILED_NOTE) or "")
    except Exception:
        return False


def mark_update_failed(store, sha: str, reason: str = "") -> None:
    """记下这个版本起不来。只留最近几个——表越长越没人看。

    由 worker 在「升级跑完但版本没变」时调用：脚本自己回滚之后本地 HEAD 退回旧版，
    从外面看就是「升过了但还是旧的」，那就是失败。
    """
    if not sha:
        return
    try:
        prev = [x for x in (store.get_note(_FAILED_NOTE) or "").split(",") if x]
        if sha in prev:
            return
        store.set_note(_FAILED_NOTE, ",".join(([sha] + prev)[:5]))
        store.set_note(
            "update_blocked",
            f"{datetime.now().strftime('%m-%d %H:%M')} 新版本 {sha[:8]} 起不来，"
            f"已自动回滚并停止重试{('：' + reason) if reason else ''}。"
            f"修好之后推一个新提交即可（同一个提交不会再试）。",
        )
    except Exception:
        logger.exception("mark update failed: %s", sha)


def clear_update_failures(store) -> None:
    """人工点「升级到最新版」＝明确表示要再试一次，把黑名单清掉。"""
    try:
        store.set_note(_FAILED_NOTE, "")
        store.set_note("update_blocked", "")
    except Exception:
        logger.exception("clear update failures")


def update_log_tail(settings: Settings, lines: int = 40) -> str:
    try:
        return "\n".join(
            Path(settings.update_log).read_text(encoding="utf-8").splitlines()[-lines:]
        )
    except OSError:
        return "（暂无升级日志）"
