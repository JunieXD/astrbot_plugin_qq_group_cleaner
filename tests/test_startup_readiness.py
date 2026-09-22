import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import ACCOUNT, GROUP, Clock
from test_platform import Bot

from qq_group_cleaner.config import Deferred, Pace, Policy
from qq_group_cleaner.platform import Adapter, PlatformError, Router


def platform(bot):
    return SimpleNamespace(meta=lambda: SimpleNamespace(id="p", name="aiocqhttp"), bot=bot)


@pytest.mark.parametrize("existing", [0, 600])
async def test_automatic_check_waits_for_persisted_startup_buffer(env, existing):
    policy = replace(env.policy, trigger=10)
    env.box.settings = replace(
        env.box.settings,
        groups=(policy,),
        pace=replace(env.box.settings.pace, startup_min_seconds=30, startup_max_seconds=30),
    )
    now = env.clock()
    await env.store.call("set", "startup_until", now + existing)
    # Drive dispatch explicitly, without the real scheduler's 15-second timer.
    env.service.loop = AsyncMock()
    env.router.resolve = AsyncMock(wraps=env.router.resolve)
    await env.service.start()
    until = now + max(existing, 30)

    env.service.dispatch()
    await asyncio.gather(*env.service.workers.values())
    env.router.resolve.assert_not_awaited()
    assert env.service.next_check[GROUP] == until
    assert "普通启动/重载缓冲" in await env.store.call("get", "check-error:" + GROUP)

    env.clock.now = until - 1
    env.service.dispatch()
    assert not env.service.workers
    env.clock.now = until
    env.service.dispatch()
    await asyncio.gather(*env.service.workers.values())
    env.router.resolve.assert_awaited_once()
    assert await env.store.call("get", "check-error:" + GROUP) == ""
    assert not env.adapter.kicks


async def test_empty_platform_list_retries_then_checks_when_platform_is_loaded(env):
    platforms = []
    router = Router(
        SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: platforms)),
        env.store,
        clock=env.clock,
    )
    env.service.router = router
    now = env.clock()
    await env.service.check_group(env.policy)
    assert env.service.next_check[GROUP] == now + 30
    assert "QQ 接入尚未就绪" in await env.store.call("get", "check-error:" + GROUP)
    assert not env.service.failure

    bot = Bot(
        {
            "get_login_info": {"user_id": ACCOUNT},
            "get_status": {"online": True},
            "get_group_detail_info": {"member_count": 3, "max_member_count": 500, "group_all_shut": 0},
        }
    )
    platforms.append(platform(bot))
    router.adapters["p"] = Adapter("p", bot, store=env.store, clock=env.clock, sleep=env.clock.sleep)
    env.clock.now = now + 30
    env.service.dispatch()
    await asyncio.gather(*env.service.workers.values())
    assert await env.store.call("get", "check-error:" + GROUP) == ""
    assert env.clock() + 1800 <= env.service.next_check[GROUP] <= env.clock() + 2100
    assert [action for action, _ in bot.calls] == [
        "get_login_info",
        "get_status",
        "get_group_detail_info",
    ]


@pytest.mark.parametrize("previously_connected", [False, True])
async def test_disconnected_websocket_waits_without_api_calls_and_preserves_recovery(
    env, previously_connected
):
    bot = Bot({"get_login_info": {"user_id": ACCOUNT}})
    bot._wsr_api_clients = {ACCOUNT: object()} if previously_connected else {}

    def pace():
        return Pace(recovery_min_seconds=120, recovery_max_seconds=120)

    api = Adapter("p", bot, store=env.store, clock=env.clock, sleep=env.clock.sleep, pace=pace)
    bot._wsr_api_clients.clear()
    for _ in range(2):
        with pytest.raises(Deferred, match="尚未连接") as caught:
            await api.identity()
        assert caught.value.until == env.clock() + 30
    assert not bot.calls
    assert not await env.store.call("get", "reads:p", [])
    if previously_connected:
        assert await env.store.call("get", "connection-cooldown:p") == env.clock() + 120

    env.clock.now += 30
    bot._wsr_api_clients[ACCOUNT] = object()
    assert await api.identity() == ACCOUNT
    until = env.clock() + 120
    assert api.recovery_until == until
    assert await env.store.call("get", "connection-cooldown:p") == until
    reloaded = Adapter("p", bot, store=env.store, clock=env.clock, sleep=env.clock.sleep, pace=pace)
    assert await reloaded.identity() == ACCOUNT
    assert reloaded.recovery_until == until


async def test_disconnected_write_is_not_sent_or_retried():
    clock = Clock()
    bot = Bot({})
    bot._wsr_api_clients = {}
    api = Adapter("p", bot, clock=clock, sleep=clock.sleep)
    entered = []
    with pytest.raises(Deferred):
        await api.kick(GROUP, "200001", before_send=lambda: entered.append(True))
    assert not entered
    assert not bot.calls


async def test_qq_offline_check_retries_soon_without_removing_members(env):
    env.adapter.connected = False
    now = env.clock()
    await env.service.check_group(env.policy)
    assert env.service.next_check[GROUP] == now + 30
    assert "QQ 当前离线" in await env.store.call("get", "check-error:" + GROUP)
    assert not env.adapter.kicks
    assert not env.adapter.snapshot_calls


async def test_configured_account_mismatch_is_not_treated_as_startup_wait():
    bot = Bot({"get_login_info": {"user_id": ACCOUNT}})
    router = Router(SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform(bot)])))
    with pytest.raises(PlatformError, match="未找到配置的机器人 QQ"):
        await router.resolve(Policy(GROUP, bot_qq="999999"))
