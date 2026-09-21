from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import ACCOUNT, ADMIN, GROUP
from test_platform import Bot

from qq_group_cleaner.commands import Commands
from qq_group_cleaner.config import CleanerError, Deferred, Pace, Policy, parse_settings
from qq_group_cleaner.executor import Executor
from qq_group_cleaner.platform import Adapter, PlatformError, Router


@pytest.mark.parametrize(
    "pace",
    [
        {"startup_min_seconds": -1},
        {"startup_max_seconds": 601},
        {"startup_min_seconds": 91},
        {"startup_min_seconds": True},
        {"recovery_min_seconds": 29},
        {"recovery_max_seconds": 3601},
        {"recovery_min_seconds": 901},
        {"recovery_max_seconds": "300.5"},
    ],
)
def test_invalid_cooldown_configuration(pace):
    with pytest.raises(CleanerError):
        parse_settings({"pace": pace})


@pytest.mark.parametrize("existing,seconds,expected", [(0, 7, 7), (0, 0, 0), (600, 7, 600)])
async def test_short_startup_config_and_existing_reservation(env, existing, seconds, expected):
    env.box.settings = replace(
        env.box.settings,
        enabled=False,
        pace=replace(env.box.settings.pace, startup_min_seconds=seconds, startup_max_seconds=seconds),
    )
    now = env.clock()
    await env.store.call("set", "startup_until", now + existing)
    await env.service.start()
    assert await env.store.call("get", "startup_until") == now + expected
    assert not env.adapter.kicks


@pytest.mark.parametrize(
    "key,reason",
    [
        ("startup_until", "普通启动/重载缓冲"),
        ("cooldown:" + ACCOUNT, "恢复冷却"),
        (f"batch:{ACCOUNT}:{GROUP}", "批次间隔"),
    ],
)
async def test_execution_and_status_explain_actual_wait(env, key, reason):
    plan = await env.service.build_plan(env.policy, env.adapter)
    until = env.clock() + 120
    await env.store.call("set", key, until)
    with pytest.raises(Deferred, match=reason) as caught:
        await Executor(env.service).execute(plan, env.adapter)
    assert caught.value.until == until
    assert not env.adapter.kicks
    status = await Commands(env.service).status(env.policy, env.adapter)
    assert reason in status and "北京时间" in status


@pytest.mark.parametrize("existing,expected", [(0, 120), (600, 600)])
async def test_resume_uses_recovery_config_without_shortening_existing_wait(env, existing, expected):
    env.box.settings = replace(
        env.box.settings,
        pace=replace(env.box.settings.pace, recovery_min_seconds=120, recovery_max_seconds=120),
    )
    now = env.clock()
    await env.store.call("set", "cooldown:" + ACCOUNT, now + existing)
    await env.service.resume(ACCOUNT, GROUP, ADMIN)
    assert await env.store.call("get", "cooldown:" + ACCOUNT) == now + expected


async def test_network_error_during_read_survives_short_reload(env):
    bot = Bot({"get_status": TimeoutError()})
    pace = Pace(recovery_min_seconds=120, recovery_max_seconds=120)

    def adapter():
        return Adapter("p", bot, clock=env.clock, sleep=env.clock.sleep, store=env.store, pace=lambda: pace)

    api = adapter()
    with pytest.raises(PlatformError):
        await api.online()
    until = api.recovery_until
    assert until == env.clock() + 120
    bot.responses["get_status"] = {"online": True}
    replacement = adapter()
    assert await replacement.online()
    assert replacement.recovery_until == until
    assert not env.adapter.kicks


async def test_observed_offline_survives_reload_and_cools_from_recovery(env):
    bot = Bot({"get_status": {"online": False}})
    pace = Pace(recovery_min_seconds=120, recovery_max_seconds=120)
    api = Adapter("p", bot, clock=env.clock, sleep=env.clock.sleep, store=env.store, pace=lambda: pace)
    assert not await api.online()
    env.clock.now += 600
    bot.responses["get_status"] = {"online": True}
    reloaded = Adapter("p", bot, clock=env.clock, sleep=env.clock.sleep, store=env.store, pace=lambda: pace)
    assert await reloaded.online()
    assert reloaded.recovery_until == env.clock() + 120


async def test_websocket_recovery_setting_reaches_router_and_survives_shutdown(env):
    bot = Bot({"get_login_info": {"user_id": ACCOUNT}})
    bot._wsr_api_clients = {ACCOUNT: object()}
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(id="p", name="aiocqhttp"), bot=bot)
    pace = Pace(recovery_min_seconds=120, recovery_max_seconds=120)
    router = Router(
        SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform])),
        env.store,
        pace=lambda: pace,
    )
    api = await router.resolve(Policy(GROUP))
    api.clock = env.clock
    bot._wsr_api_clients[ACCOUNT] = object()
    api.connection_stamp()
    until = api.recovery_until
    assert until == env.clock() + 120
    pace = replace(pace, recovery_min_seconds=30, recovery_max_seconds=30)
    bot._wsr_api_clients[ACCOUNT] = object()
    api.connection_stamp()
    assert api.recovery_until == until
    env.service.router = router
    await env.service.stop()
    assert await env.store.call("get", "connection-cooldown:p") == until


async def test_healthy_reload_does_not_start_connection_recovery(env):
    bot = Bot({"get_status": {"online": True}})
    for _ in range(2):
        api = Adapter("p", bot, clock=env.clock, sleep=env.clock.sleep, store=env.store)
        assert await api.online()
        assert api.recovery_until == 0


async def test_shared_queue_receives_configured_recovery_range(env):
    captured = []

    class Guard:
        deferred_error = RuntimeError

        async def run(self, **kwargs):
            captured.append(kwargs["config"])
            return await kwargs["action"]()

    env.router.guard = Guard()
    env.box.settings = replace(
        env.box.settings,
        pace=replace(env.box.settings.pace, recovery_min_seconds=120, recovery_max_seconds=240),
    )
    plan = await env.service.build_plan(env.policy, env.adapter)
    await Executor(env.service).execute(plan, env.adapter)
    assert captured
    assert all(c["recovery_min_seconds"] == 120 and c["recovery_max_seconds"] == 240 for c in captured)
