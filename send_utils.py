"""core→平台 下发工具.

包含三类下行能力(与 NoneBot2 参考实现 GenshinUID/send_utils.py 同口径):
- gs_to_components: 把 core 下发的 GsMessage 列表转换为 AstrBot 消息组件;
- aiocqhttp_send: aiocqhttp(OneBot V11) 平台直发并捕获平台出站 message_id,
  供 recall_message_id 回执(插件侧 `bot.send(msg, wait_recall=True)`)使用;
- del_msg / execute_ban_user: `excute_delete_message` / `excute_ban_user`
  控制包的平台 API 落地, 不当普通消息发送.
"""

import asyncio
import base64
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.platform import Platform
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.context import Context

from .models import Message as GsMessage
from .models import MessageSend

if TYPE_CHECKING:
    # 仅类型标注用: 各平台适配器的具体类(get_client()/客户端属性带完整类型)
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
        AiocqhttpAdapter,
    )
    from astrbot.core.platform.sources.discord.discord_platform_adapter import (
        DiscordPlatformAdapter,
    )
    from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter
    from astrbot.core.platform.sources.telegram.tg_adapter import (
        TelegramPlatformAdapter,
    )


def store_file(path: Path, file: str) -> None:
    """把 base64 形态的文件内容落盘(供 file/video 段发送用)."""
    if file.startswith("base64://"):
        file = file[9:]
    file_content = base64.b64decode(file)
    with open(path, "wb") as f:
        _ = f.write(file_content)


async def gs_to_components(
    gsmsgs: list[GsMessage],
    bot_id: str,
    platform: Platform | None,
    temp_dir: Path,
) -> list[BaseMessageComponent]:
    """把 core 下发的 GsMessage 列表转换为 AstrBot 消息组件列表.

    image/record/video 均为双形态(base64:// 与 link://), 两种都必须处理;
    node(合并转发)仅 onebot 走原生 Nodes, 其余平台展开逐段发送;
    excute_ban_user 段为控制语义, 就地执行禁言后不进消息链.
    """
    message: list[BaseMessageComponent] = []
    for _c in gsmsgs:
        if not _c.data:
            continue
        if _c.type == "text":
            message.append(Plain(_c.data))
        elif _c.type == "image":
            if _c.data.startswith("link://"):
                message.append(Image.fromURL(_c.data[7:]))
            else:
                data = _c.data
                if data.startswith("base64://"):
                    data = data[9:]
                message.append(Image.fromBase64(data))
        elif _c.type == "record":
            if _c.data.startswith("link://"):
                message.append(Record.fromURL(_c.data[7:]))
            else:
                data = _c.data
                if data.startswith("base64://"):
                    data = data[9:]
                message.append(Record.fromBase64(data))
        elif _c.type == "video":
            if _c.data.startswith("link://"):
                message.append(Video.fromURL(_c.data[7:]))
            else:
                path = temp_dir / f"{uuid.uuid4().hex}.mp4"
                store_file(path, _c.data)
                message.append(Video.fromFileSystem(str(path)))
        elif _c.type == "node":
            if bot_id == "onebot":
                # QQ 平台用原生合并转发, 将多条消息聚合为一个气泡
                node_message: list[Node] = []
                for _node in _c.data:
                    node_message.append(
                        Node(
                            await gs_to_components(
                                [GsMessage(**_node)], bot_id, platform, temp_dir
                            )
                        )
                    )
                message.append(Nodes(node_message))
            else:
                for _node in _c.data:
                    message.extend(
                        await gs_to_components(
                            [GsMessage(**_node)], bot_id, platform, temp_dir
                        )
                    )
        elif _c.type == "file":
            file_name, file_content = _c.data.split("|", 1)
            path = temp_dir / file_name
            store_file(path, file_content)
            message.append(File(file_name, str(path)))
        elif _c.type == "at":
            message.append(At(qq=_c.data))
        elif _c.type == "excute_ban_user":
            await execute_ban_user(platform, _c.data)
        else:
            logger.warning(f"[GsCore] 不支持的下发消息段类型, 已忽略: {_c.type}")
    return message


async def aiocqhttp_send(
    platform: Platform,
    chain: MessageChain,
    is_group: bool,
    session_id: str,
) -> str | list[str] | None:
    """aiocqhttp 平台直发消息并收集平台出站 message_id.

    发送行为与 AiocqhttpMessageEvent.send_message 保持一致(转发/文件逐条发,
    普通段合并发), 区别仅在于收集各次调用返回的 message_id 供回执使用——
    上游通用发送路径不返回消息 id, 故此处借用其两个受保护的转换 helper
    以保证消息编码行为完全一致.
    无 id -> None; 单气泡 -> str; 多气泡 -> list[str], 由 core flatten.
    """
    bot = cast("AiocqhttpAdapter", platform).get_client()
    sid = int(session_id)
    ids: list[str] = []

    async def _dispatch(messages: list[dict[str, Any]]) -> None:
        if is_group:
            ret = await bot.send_group_msg(group_id=sid, message=messages)
        else:
            ret = await bot.send_private_msg(user_id=sid, message=messages)
        if isinstance(ret, dict) and ret.get("message_id") is not None:
            ids.append(str(ret["message_id"]))

    # 转发消息、文件消息不能和普通消息混在一起发送
    send_one_by_one = any(
        isinstance(seg, (Node, Nodes, File)) for seg in chain.chain
    )
    if not send_one_by_one:
        messages = await AiocqhttpMessageEvent._parse_onebot_json(  # pyright: ignore[reportPrivateUsage]
            chain
        )
        if messages:
            await _dispatch(messages)
    else:
        for seg in chain.chain:
            if isinstance(seg, (Node, Nodes)):
                if isinstance(seg, Node):
                    seg = Nodes([seg])
                payload = await seg.to_dict()
                if is_group:
                    payload["group_id"] = session_id
                    ret = await bot.call_action("send_group_forward_msg", **payload)
                else:
                    payload["user_id"] = session_id
                    ret = await bot.call_action("send_private_forward_msg", **payload)
                # 合并转发本身是一个气泡, 协议返回 message_id
                if isinstance(ret, dict) and ret.get("message_id") is not None:
                    ids.append(str(ret["message_id"]))
            elif isinstance(seg, File):
                d = await AiocqhttpMessageEvent._from_segment_to_dict(  # pyright: ignore[reportPrivateUsage]
                    seg
                )
                await _dispatch([d])
            else:
                messages = await AiocqhttpMessageEvent._parse_onebot_json(  # pyright: ignore[reportPrivateUsage]
                    MessageChain([seg])
                )
                if messages:
                    await _dispatch(messages)
                    await asyncio.sleep(0.5)

    if not ids:
        return None
    return ids[0] if len(ids) == 1 else ids


async def del_msg(context: Context, msg: MessageSend) -> None:
    """撤回已发出的消息(对应 core 下发的 excute_delete_message 控制包).

    各平台撤回入参不同: OneBot/飞书仅需消息id; Telegram/Discord 需会话定位.
    平台无对应 API 时记 warning, 不误发空消息、不抛异常.
    """
    _data = msg.content[0].data if msg.content else None
    message_id = _data.get("message_id") if isinstance(_data, dict) else None
    if message_id is None:
        return
    message_id = str(message_id)

    platform = context.get_platform_inst(msg.bot_self_id)
    if platform is None:
        logger.warning(f"[GsCore] 撤回消息失败: 未找到平台实例 {msg.bot_self_id}")
        return
    name = platform.meta().name
    try:
        if name == "aiocqhttp":
            bot = cast("AiocqhttpAdapter", platform).get_client()
            await bot.delete_msg(message_id=int(message_id))
        elif name == "telegram":
            # group_id 可能为 chat_id#thread_id 形态, 撤回仅需 chat_id
            chat_id = (msg.target_id or "").split("#")[0]
            if chat_id:
                tg_bot = cast("TelegramPlatformAdapter", platform).get_client()
                await tg_bot.delete_message(
                    chat_id=chat_id, message_id=int(message_id)
                )
        elif name == "lark":
            from lark_oapi.api.im.v1 import (  # pyright: ignore[reportMissingImports]
                DeleteMessageRequest,
            )

            request = (
                DeleteMessageRequest.builder().message_id(message_id).build()
            )
            lark_api = cast("LarkPlatformAdapter", platform).lark_api
            await lark_api.im.v1.message.adelete(request)
        elif name == "discord":
            client = cast("DiscordPlatformAdapter", platform).client
            channel_id = int((msg.target_id or "").split("_")[-1])
            channel = client.get_channel(channel_id) or await client.fetch_channel(
                channel_id
            )
            await channel.get_partial_message(int(message_id)).delete()
        else:
            logger.warning(f"[GsCore] 平台 {name} 暂不支持撤回消息")
    except Exception as e:
        logger.warning(f"[GsCore] 撤回消息失败({name}): {e}")


async def execute_ban_user(platform: Platform | None, data: Any) -> None:
    """禁言群成员(对应 core 下发的 excute_ban_user 段).

    duration 单位秒, 0 表示解除禁言; 兼容 int 与纯数字串, 非法值静默跳过.
    """
    if not isinstance(data, dict):
        return
    user_id = data.get("user_id")
    group_id = data.get("group_id")
    duration = data.get("duration")
    if user_id is None or group_id is None:
        return
    if not (
        isinstance(duration, int)
        or (isinstance(duration, str) and duration.isdigit())
    ):
        return
    if platform is None or platform.meta().name != "aiocqhttp":
        name = platform.meta().name if platform else None
        logger.warning(f"[GsCore] 平台 {name} 暂不支持禁言")
        return
    try:
        bot = cast("AiocqhttpAdapter", platform).get_client()
        await bot.set_group_ban(
            group_id=int(group_id),
            user_id=int(user_id),
            duration=int(duration),
        )
    except Exception as e:
        logger.warning(f"[GsCore] 禁言失败(aiocqhttp): {e}")
