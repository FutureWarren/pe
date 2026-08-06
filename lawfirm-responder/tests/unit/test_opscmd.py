"""运维指令：服务器主动去仓库取活干，结果回传企业微信。

存在的理由是两边够不着：律所侧没有 SSH，运维侧出网受组织策略限制。
但服务器够得着 GitHub——把方向反过来，运维就不必再让律所方在浏览器上手点。
今天为了取一个访问令牌折腾了半小时，就是这条通道缺失的代价。
"""

import json

import pytest

from responder.config import Settings
from responder.opscmd import Runner
from responder.store.db import Store


class Snd:
    def __init__(self):
        self.direct: list[tuple[str, str]] = []

    def send_direct_text(self, userid, text):
        self.direct.append((userid, text))
        return True


class FakeKf:
    def __init__(self, servicers=()):
        self.servicers = list(servicers)
        self.added: list[list[str]] = []

    def available(self):
        return True

    def account_list(self):
        return [{"open_kfid": "wk-1", "name": "在线咨询"}]

    def servicer_add(self, open_kfid, userids):
        self.added.append(list(userids))
        self.servicers = sorted(set(self.servicers) | set(userids))
        return {"errcode": 0}

    def servicer_list(self, open_kfid):
        return list(self.servicers)


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    db = str(tmp_path / "ops.db")
    settings = Settings(
        db_path=db, update_repo_dir=str(repo), admin_token="old-token-xyz",
        default_notify_userid="wei", public_base_url="http://1.2.3.4",
    )
    store = Store(db)
    return repo, store, settings


def write_commands(repo, commands):
    (repo / "ops" / "commands.json").write_text(
        json.dumps({"commands": commands}, ensure_ascii=False), encoding="utf-8"
    )


def test_reset_token_generates_one_and_sends_it_to_wecom(env):
    """今天的死结：令牌丢了 → 进不去控制台 → 改不了令牌。这条指令是出口。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token"}])
    snd = Snd()

    Runner(settings, store, sender=snd).run_pending()

    assert settings.admin_token != "old-token-xyz"
    assert len(settings.admin_token) >= 12
    to, text = snd.direct[0]
    assert to == "wei"
    assert settings.admin_token in text
    assert "http://1.2.3.4/ui#t=" in text  # 顺手给一条免输入链接


def test_new_token_never_lands_in_the_repo(env):
    """仓库是公开的。指令文件里出现令牌，等于把令牌贴到公网上。

    所以这条指令**不接受指定值**——由服务器生成，只走企微私信送出去。
    """
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token", "token": "我指定的"}])
    Runner(settings, store, sender=Snd()).run_pending()

    assert settings.admin_token != "我指定的"
    assert settings.admin_token not in (repo / "ops" / "commands.json").read_text(
        encoding="utf-8"
    )


def test_commands_run_exactly_once(env):
    """自动升级每 5 分钟拉一次仓库。没有幂等，令牌会每五分钟被重置一次。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token"}])
    snd = Snd()
    r = Runner(settings, store, sender=snd)

    r.run_pending()
    first = settings.admin_token
    r.run_pending()
    r.run_pending()

    assert settings.admin_token == first
    assert len(snd.direct) == 1


def test_missing_file_is_not_an_error(env):
    """绝大多数时候仓库里根本没有待办指令，这条路径必须安静。"""
    _, store, settings = env
    assert Runner(settings, store, sender=Snd()).run_pending() == []


def test_unknown_op_is_skipped_and_reported(env):
    """未来版本的指令跑在旧服务器上时，不认识就跳过——不能崩。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "从未见过的动作"}])
    snd = Snd()
    out = Runner(settings, store, sender=snd).run_pending()
    assert "不认识" in out[0]["result"]
    assert store.command_done("c1")  # 记下来，别每轮都重试


def test_one_bad_command_does_not_block_the_next(env):
    repo, store, settings = env
    write_commands(repo, [
        {"id": "bad", "op": "add_kf_servicers"},   # 没配客服，会走失败分支
        {"id": "good", "op": "reset_admin_token"},
    ])
    Runner(settings, store, sender=Snd()).run_pending()
    assert store.command_done("good")


def test_add_servicers_command(env):
    """会话转接的硬前置，做成指令后律所侧一下都不用点。"""
    repo, store, settings = env
    store.upsert_lawyer("wei", {"name": "魏", "role": "lawyer", "active": True})
    write_commands(repo, [{"id": "c1", "op": "add_kf_servicers"}])
    kf = FakeKf()
    snd = Snd()

    Runner(settings, store, sender=snd, kf_client=kf).run_pending()

    assert kf.added == [["wei"]]
    assert "已就位 1 位" in snd.direct[0][1]


def test_report_command_answers_the_questions_we_keep_asking(env):
    """远程排障时最常问的那几项，一条指令全带回来。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "report"}])
    snd = Snd()
    Runner(settings, store, sender=snd, kf_client=FakeKf()).run_pending()
    text = snd.direct[0][1]
    for key in ("运行模式", "进线事件累计", "律师名册", "对外地址"):
        assert key in text


def test_send_failure_does_not_make_a_harmless_command_retry_forever(env):
    """汇报类指令送不出去就算了，不能每 5 分钟重跑一次刷屏。

    可回滚的指令（重置令牌）走的是另一套：见下面「送不到就别改」那一组。
    """
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "report"}])

    class Broken:
        def send_direct_text(self, userid, text):
            raise RuntimeError("企微挂了")

    Runner(settings, store, sender=Broken()).run_pending()
    assert store.command_done("c1")


def test_falls_back_to_first_lawyer_when_no_notify_target(env):
    """没配兜底接收人时也得有人收到，否则结果等于石沉大海。"""
    repo, store, settings = env
    settings.default_notify_userid = ""
    store.upsert_lawyer("zhang", {"name": "张", "role": "lawyer", "active": True})
    write_commands(repo, [{"id": "c1", "op": "report"}])
    snd = Snd()
    Runner(settings, store, sender=snd).run_pending()
    assert snd.direct[0][0] == "zhang"


# --------------------------------------------- 送不到就别改：锁死比不改坏得多
# 这条指令唯一的危险不是被滥用，而是「改成功了但通知没送到」——
# 旧令牌当场失效、新令牌没人知道，等于把律所锁在自己的系统外面。
def test_reset_does_nothing_when_there_is_nobody_to_tell(env):
    """名册为空、兜底接收人也没配，是很常见的初始状态（今天就是）。"""
    repo, store, settings = env
    settings.default_notify_userid = ""
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token"}])

    Runner(settings, store, sender=Snd()).run_pending()

    assert settings.admin_token == "old-token-xyz"  # 一动没动


def test_reset_rolls_back_when_the_message_fails_to_send(env):
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token"}])

    class Broken:
        def send_direct_text(self, userid, text):
            raise RuntimeError("企微挂了")

    Runner(settings, store, sender=Broken()).run_pending()
    assert settings.admin_token == "old-token-xyz"
    # 且不落库——企微恢复后下一轮自动重来，不需要人再提交一次指令
    assert not store.command_done("c1")


def test_reset_rolls_back_when_wecom_reports_failure(env):
    """企微接口返回失败（而不是抛异常）同样算没送到。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token"}])

    class Refuses:
        def send_direct_text(self, userid, text):
            return False

    Runner(settings, store, sender=Refuses()).run_pending()
    assert settings.admin_token == "old-token-xyz"


def test_command_can_name_its_own_recipient(env):
    """名册还没建起来时，指令自带收件人就是唯一能走通的路。"""
    repo, store, settings = env
    settings.default_notify_userid = ""
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token", "to": "future"}])
    snd = Snd()

    Runner(settings, store, sender=snd).run_pending()

    assert snd.direct[0][0] == "future"
    assert settings.admin_token != "old-token-xyz"


def test_add_lawyer_also_fixes_the_silent_notification_hole(env):
    """名册为空 + 兜底接收人为空 = 线索照样入库评分，但交接单一个人也发不到。

    这是最坏的一种失败：控制台里一切正常，律师那边什么都没有。
    实测过——客户留了电话，系统安安静静地把它存进了数据库。
    """
    repo, store, settings = env
    settings.default_notify_userid = ""
    write_commands(repo, [{
        "id": "c1", "op": "add_lawyer", "to": "future",
        "userid": "future", "name": "魏涞", "specialties": "劳动仲裁",
    }])

    Runner(settings, store, sender=Snd()).run_pending()

    assert store.get_lawyer("future")["name"] == "魏涞"
    assert settings.default_notify_userid == "future"


def test_add_lawyer_does_not_steal_an_existing_notify_target(env):
    """已经配好接收人的，不能被一条加人指令悄悄改掉。"""
    repo, store, settings = env  # fixture 里是 wei
    write_commands(repo, [{"id": "c1", "op": "add_lawyer", "userid": "zhang"}])
    Runner(settings, store, sender=Snd()).run_pending()
    assert settings.default_notify_userid == "wei"


# ------------------------------------------- 「没收到」必须能远程分辨出原因
def test_delivery_failure_is_recorded_where_it_can_be_read(env):
    """律所侧没有服务器日志。发失败只写日志，现象就只剩「什么都没收到」，
    跟「功能还没上线」分不开——今天正是卡在这一步。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "report"}])

    class Refuses:
        last_error = "message/send: 81013 user not found"

        def send_direct_text(self, userid, text):
            return False

    Runner(settings, store, sender=Refuses()).run_pending()
    assert "81013" in store.get_note("ops_error")


def test_successful_delivery_clears_the_error(env):
    repo, store, settings = env
    store.set_note("ops_error", "上一次的旧错误")
    write_commands(repo, [{"id": "c1", "op": "report"}])
    Runner(settings, store, sender=Snd()).run_pending()
    assert store.get_note("ops_error") == ""


def test_notes_do_not_count_as_executed_commands(env):
    """小记跟指令共用一张表，但不能把「执行了几条」这个数搞乱。"""
    _, store, settings = env
    store.set_note("ops_error", "x")
    assert store.count_commands_done() == 0


def test_underscore_ids_are_reserved_for_notes(env):
    """指令占用 `_` 前缀会跟小记互相覆盖，直接不执行。"""
    repo, store, settings = env
    write_commands(repo, [{"id": "_ops_error", "op": "report"}])
    snd = Snd()
    assert Runner(settings, store, sender=snd).run_pending() == []
    assert not snd.direct


def test_reset_without_recipient_stays_pending_for_retry(env):
    """「没能通知到任何人」是待办，不是完成。

    踩过这个坑：名册还空着时这条指令跑了一轮、什么也没做、却被记成做完；
    等收件人配好之后它再也不会执行，人就一直等不到那条令牌。
    """
    repo, store, settings = env
    settings.default_notify_userid = ""
    write_commands(repo, [{"id": "c1", "op": "reset_admin_token"}])
    snd = Snd()

    Runner(settings, store, sender=snd).run_pending()
    assert not store.command_done("c1"), "留着，等有收件人了再跑"

    # 收件人配好之后，同一条指令应该自己跑起来，不需要换 id 重提
    settings.default_notify_userid = "future"
    Runner(settings, store, sender=snd).run_pending()
    assert store.command_done("c1")
    assert settings.admin_token != "old-token-xyz"
    assert snd.direct and settings.admin_token in snd.direct[-1][1]


def test_report_lists_each_kf_account(env):
    """客服账号可能不止一个，而应用未必对每个都有权限（企微 48007）。

    客户扫到的要是那个没权限的账号，AI 根本收不到消息——现象是
    「客户说没人理」，从我们这边什么都看不出来。所以逐个报接待人数。
    """
    repo, store, settings = env
    write_commands(repo, [{"id": "c1", "op": "report"}])
    snd = Snd()
    Runner(settings, store, sender=snd, kf_client=FakeKf(servicers=("a", "b"))).run_pending()
    assert "客服账号「在线咨询」接待人 2 位" in snd.direct[0][1]


def test_backfill_memory_covers_conversations_the_sweep_can_never_see(env):
    """定时扫描只回看 24 小时。功能上线前就安静下来的会话落在窗口外，
    永远等不到那次扫描——而它们恰恰是最早那批客户，最可能回访。"""
    import json
    from datetime import datetime, timedelta

    from responder.models import ClientStatus, GroupProfile

    repo, store, settings = env
    gid = "kf:wk:old"
    store.upsert_group(GroupProfile(
        group_id=gid, kf_open_kfid="wk", kf_external_userid="old",
        client_status=ClientStatus.PROSPECT, case_type="劳动仲裁",
    ))
    store.upsert_lead(gid, {
        "intent": "warm", "contact": "137",
        "key_facts": json.dumps(["拖欠三个月工资"], ensure_ascii=False),
    })
    with store._conn() as conn:  # 半个月前的老会话，早已不在扫描窗口内
        conn.execute("UPDATE leads SET created_at=?",
                     ((datetime.now() - timedelta(days=15)).isoformat(),))

    write_commands(repo, [{"id": "c1", "op": "backfill_memory"}])
    Runner(settings, store, sender=Snd()).run_pending()

    assert "劳动仲裁" in store.get_group(gid).memory


def test_backfill_does_not_overwrite_existing_memory(env):
    repo, store, settings = env
    from responder.models import ClientStatus, GroupProfile

    store.upsert_group(GroupProfile(group_id="kf:wk:x", kf_open_kfid="wk",
                                    kf_external_userid="x",
                                    client_status=ClientStatus.PROSPECT))
    store.set_memory("kf:wk:x", "人工写好的记忆")
    write_commands(repo, [{"id": "c1", "op": "backfill_memory"}])
    Runner(settings, store, sender=Snd()).run_pending()
    assert store.get_group("kf:wk:x").memory == "人工写好的记忆"
