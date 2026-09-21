from types import SimpleNamespace

import pytest
from conftest import ACCOUNT, GROUP, Clock

from qq_group_cleaner.config import Policy
from qq_group_cleaner.platform import Adapter, PlatformError, Router


class Bot:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        result = self.responses[action]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result()
        return result


def adapter(responses):
    clock = Clock()
    return Adapter("platform", Bot(responses), sleep=clock.sleep, clock=clock)


@pytest.mark.parametrize(
    "result", [{"status": "failed", "retcode": 1}, {"status": "ok", "retcode": 1}, TimeoutError("secret-url")]
)
async def test_failed_responses_are_sanitized(result):
    api = adapter({"get_login_info": result})
    with pytest.raises(PlatformError) as caught:
        await api.identity()
    assert "secret-url" not in str(caught.value)


async def test_wrapped_and_unwrapped_identity_and_switch():
    api = adapter({"get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": ACCOUNT}}})
    assert await api.identity() == ACCOUNT
    api.bot.responses["get_login_info"] = {"user_id": "999999"}
    with pytest.raises(PlatformError, match="发生变化"):
        await api.identity()


async def test_kick_only_uses_ordinary_removal_and_never_retries():
    api = adapter({"set_group_kick": TimeoutError()})
    with pytest.raises(PlatformError):
        await api.kick(GROUP, "200001")
    assert api.bot.calls == [
        ("set_group_kick", {"group_id": int(GROUP), "user_id": 200001, "reject_add_request": False})
    ]


@pytest.mark.parametrize(
    "items,count",
    [
        ([{"user_id": "200001"}] * 2, 2),
        ([{"user_id": "200001"}], 2),
        ([{"user_id": "200001", "group_id": "999999"}], 1),
    ],
)
async def test_invalid_or_incomplete_lists_block_execution(items, count):
    api = adapter(
        {
            "get_group_member_list": items,
            "get_group_detail_info": {"member_count": count, "max_member_count": 500},
        }
    )
    with pytest.raises(PlatformError):
        await api.snapshot(GROUP)


async def test_changed_lists_block_and_same_snapshot_accepted():
    items = iter([[{"user_id": "200001"}], [{"user_id": "200002"}]])
    api = adapter(
        {
            "get_group_member_list": lambda: next(items),
            "get_group_detail_info": {"member_count": 1, "max_member_count": 500},
        }
    )
    with pytest.raises(PlatformError):
        await api.snapshot(GROUP)
    api.bot.responses["get_group_member_list"] = [{"user_id": "200001"}]
    info, members = await api.snapshot(GROUP)
    assert info.count == 1 and members[0].user_id == "200001"


async def test_read_budget_and_status():
    api = adapter({"get_status": {"online": True}})
    assert await api.online()
    api.reads.extend([api.clock()] * 180)
    with pytest.raises(PlatformError, match="上限"):
        await api.online()


async def test_ambiguous_and_duplicate_accounts_rejected():
    def platform(pid, account):
        return SimpleNamespace(
            meta=lambda: SimpleNamespace(id=pid, name="aiocqhttp"),
            bot=Bot({"get_login_info": {"user_id": account}}),
        )

    platforms = [platform("a", ACCOUNT), platform("b", ACCOUNT)]
    router = Router(SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: platforms)))
    with pytest.raises(PlatformError, match="多个平台"):
        await router.resolve(Policy(GROUP))
    platforms[1] = platform("b", "999999")
    router.adapters.clear()
    with pytest.raises(PlatformError, match="唯一"):
        await router.resolve(Policy(GROUP))
    router.adapters.clear()
    assert (await router.resolve(Policy(GROUP, bot_qq=ACCOUNT))).account == ACCOUNT
