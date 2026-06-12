"""平台元事件(meta event)映射与上报构造.

把 AstrBot 各平台适配器的通知类事件(进群/退群/戳一戳)统一映射为 gsuid_core
的 meta 命名约定, 由本模块构造 content=[Message('meta-<事件名>', data)] 的
MessageReceive, 交回 main.py 上报, 最终由 core 的 `@sv.on_meta(...)` 触发器分发.

约定(与 NoneBot2 参考实现 GenshinUID/meta_event.py 同口径):
- 标准事件仅三种, data 字段跨平台统一, 插件侧用 `ev.get_meta(key)` 读取:
  - user_join_group: user_id / group_id / operator_id (+平台补充字段)
  - user_exit_group: user_id / group_id / operator_id (+平台补充字段)
  - poke:            user_id(发起者) / target_id(被戳者) / group_id(仅群聊)
- 每个平台一个 `_xxx_to_meta(event) -> Optional[MetaEvent]` 映射函数,
  返回 None 表示该事件无需作为 meta 上报(交由其它处理器或忽略).
- id 值一律 str() 归一.

AstrBot 侧目前仅 aiocqhttp(OneBot V11) 平台会把 notice 类事件下发给插件,
其余平台适配器未建模成员变动/戳一戳事件; 待框架支持后在 _META_MAPPERS
中补充对应映射函数即可.
"""

from collections.abc import Callable
from typing import Any, Literal, NamedTuple

from astrbot.api.event import AstrMessageEvent

from .models import Message, MessageReceive

UserType = Literal["group", "direct", "channel", "sub_channel"]


class MetaEvent(NamedTuple):
    """单条 meta 事件的归一化结果.

    event_name: 不含 `meta-` 前缀的事件名(如 `user_exit_group`).
    data:       meta 段 data 字典, 含该事件特有字段, 推荐带 `user_id`/`group_id`.
    user_type:  会话类型覆盖; None 时按 data 中是否有 group_id 推断 group/direct.
    """

    event_name: str
    data: dict[str, Any]
    user_type: UserType | None = None


def _s(value: Any) -> str:
    """统一把平台侧 id/数值归一为字符串, None -> 空串."""
    return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# aiocqhttp(OneBot V11)
# ---------------------------------------------------------------------------
def _aiocqhttp_to_meta(event: AstrMessageEvent) -> MetaEvent | None:
    # aiocqhttp 的 Event 是 dict 子类, 原始事件挂在 message_obj.raw_message 上
    raw_data = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw_data, dict) or raw_data.get("post_type") != "notice":
        return None
    ntype = raw_data.get("notice_type")
    if ntype == "group_increase":
        return MetaEvent(
            "user_join_group",
            {
                "user_id": _s(raw_data.get("user_id")),
                "group_id": _s(raw_data.get("group_id")),
                "operator_id": _s(raw_data.get("operator_id")),
                "sub_type": raw_data.get("sub_type", ""),
            },
        )
    if ntype == "group_decrease":
        return MetaEvent(
            "user_exit_group",
            {
                "user_id": _s(raw_data.get("user_id")),
                "group_id": _s(raw_data.get("group_id")),
                "operator_id": _s(raw_data.get("operator_id")),
                "sub_type": raw_data.get("sub_type", ""),
            },
        )
    if ntype == "notify" and raw_data.get("sub_type") == "poke":
        data: dict[str, Any] = {
            "user_id": _s(raw_data.get("user_id")),
            "target_id": _s(raw_data.get("target_id")),
        }
        if raw_data.get("group_id") is not None:
            data["group_id"] = _s(raw_data.get("group_id"))
        return MetaEvent("poke", data)
    return None


# 平台名(event.get_platform_name()) -> 映射函数
_META_MAPPERS: dict[str, Callable[[AstrMessageEvent], MetaEvent | None]] = {
    "aiocqhttp": _aiocqhttp_to_meta,
}


def build_meta_receive(
    event: AstrMessageEvent,
    bot_id: str,
    bot_self_id: str,
    pm: int,
) -> MessageReceive | None:
    """把平台通知类事件转为 meta 上报包; 不支持的事件返回 None.

    bot_id / bot_self_id / pm 由调用方按消息上报的同一口径算好后传入,
    保证 core 侧 area / 黑白名单 / 数据归属与普通消息一致. 顶层
    user_id/group_id 从 meta data 回读(与 core 回填口径一致).
    """
    mapper = _META_MAPPERS.get(event.get_platform_name())
    if mapper is None:
        return None

    result = mapper(event)
    if result is None:
        return None

    data = result.data
    group_id = data.get("group_id") or None
    user_id = data.get("user_id") or ""

    user_type: UserType
    if result.user_type is not None:
        user_type = result.user_type
    else:
        user_type = "group" if group_id else "direct"

    return MessageReceive(
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        user_type=user_type,
        group_id=group_id,
        user_id=user_id,
        sender={},
        content=[Message(f"meta-{result.event_name}", data)],
        msg_id="",
        user_pm=pm,
    )
