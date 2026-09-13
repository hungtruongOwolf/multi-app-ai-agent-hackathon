import pytest

from judge.connectors.slack import SlackClient
from judge.connectors.transport import ConnectorError


async def test_channel_lifecycle_name_taken_and_not_in_channel(settings, http, uid):
    bot = SlackClient(settings, http)
    oncall = SlackClient(settings, http, token="xoxp-oncall1")
    name = f"inc-20260913-{uid}"

    cid = await bot.create_channel(name)
    assert await bot.create_channel(name) == cid  # name_taken -> existing id
    assert await bot.find_channel(name) == cid

    ts = await bot.post(cid, f"war room IJ-KEY:{uid}")
    assert await bot.find_message_by_marker(cid, f"IJ-KEY:{uid}") == ts

    with pytest.raises(ConnectorError) as e:  # human not a member yet
        await oncall.replies(cid, ts)
    assert e.value.body["error"] == "not_in_channel"
    await oncall.join(cid)
    await oncall.post(cid, "approve 1a2b3c4d", thread_ts=ts)
    await oncall.post(cid, "reject 1a2b3c4d", thread_ts=ts)

    msgs = await bot.replies(cid, ts)
    assert msgs[0]["ts"] == ts and [m["text"] for m in msgs[1:]] == ["approve 1a2b3c4d", "reject 1a2b3c4d"]
    assert msgs[0].get("bot_id") and msgs[1]["user"] == "U_ONCALL_1"
    newer = await bot.replies(cid, ts, oldest=msgs[1]["ts"])
    assert [m["text"] for m in newer[1:]] == ["reject 1a2b3c4d"]
    assert await bot.find_message_by_marker(cid, "reject", thread_ts=ts) == msgs[2]["ts"]
    assert [m["ts"] for m in await bot.history(cid)] == [ts]  # history = top-level only

    await bot.archive(cid)
    await bot.archive(cid)  # idempotent
    with pytest.raises(ConnectorError) as e:
        await bot.post(cid, "late")
    assert e.value.body["error"] == "is_archived"
    with pytest.raises(ConnectorError) as e:  # archived names stay taken; lookup still resolves
        await SlackClient(settings, http, token="xoxp-intruder").join(cid)
    assert e.value.body["error"] == "is_archived"


async def test_invalid_channel_name_and_auth(settings, http):
    bot = SlackClient(settings, http)
    with pytest.raises(ConnectorError) as e:
        await bot.create_channel("Incident #1")
    assert e.value.body["error"] == "invalid_name_specials"
    assert await bot.auth_user() == "U_BOT"
    assert await bot.user_is_bot("U_BOT") and not await bot.user_is_bot("U_ONCALL_1")
