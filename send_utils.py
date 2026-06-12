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
import random
import uuid
from pathlib import Path
from types import SimpleNamespace
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
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
        QQOfficialPlatformAdapter,
    )
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
    send_one_by_one = any(isinstance(seg, (Node, Nodes, File)) for seg in chain.chain)
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


async def qqofficial_send(
    platform: Platform,
    chain: MessageChain,
    is_group: bool,
    session_id: str,
) -> None:
    """qq_official 平台直发, 复用被动回复 msg_id 规避"主动消息无权限".

    背景: AstrBot 通用发送路径(context.send_message → send_by_session)对
    qq_official 有两个会退化为"主动消息"的问题——
      1) 私聊(C2C)分支强制丢弃 msg_id 当主动推送, 未开通主动消息权限的 Bot
         直接报"主动消息失败, 无权限";
      2) 群聊分支每发一条会把"Bot 自己发出的消息 id"写回会话缓存, 后续消息
         以该 id 作被动回复锚点(对自身出站消息无效), 同样退化为主动消息——
         gscore 单次指令常下发多条消息, 故群聊从第二条起即报无权限。
    这里绕过通用路径直发: 始终以"框架在收包时缓存的最近一条入站(用户)消息
    id"作被动回复锚点, 且不回写缓存, 使群聊/私聊连续多条消息都走被动回复。

    发送行为(媒体拆分/上传/分场景 API)对齐 QQOfficialPlatformAdapter.
    _send_by_session_common, 唯一差异是保留 msg_id、不回写会话缓存。
    """
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    # 富媒体需逐条拆分发送(普通段合并), 与上游 _split_message_chain_by_media 一致
    chains = QQOfficialMessageEvent._split_message_chain_by_media(chain)  # pyright: ignore[reportPrivateUsage]
    if len(chains) > 1:
        for split in chains:
            await qqofficial_send(platform, split, is_group, session_id)
        return

    (
        plain_text,
        image_base64,
        image_path,
        record_file_path,
        video_file_source,
        file_source,
        file_name,
    ) = await QQOfficialMessageEvent._parse_to_qqofficial(chain)  # pyright: ignore[reportPrivateUsage]
    if not (
        plain_text
        or image_base64
        or image_path
        or record_file_path
        or video_file_source
        or file_source
    ):
        return

    adapter = cast("QQOfficialPlatformAdapter", platform)
    bot = adapter.get_client()
    # 框架收包时缓存的最近一条入站(用户)消息 id; 直发不经 send_by_session,
    # 不会被"Bot 自身出站 id"污染, 因而连续多条消息都能命中有效被动锚点
    msg_id = adapter._session_last_message_id.get(session_id)  # pyright: ignore[reportPrivateUsage]
    # 复用上游富媒体上传/C2C 发送(仅依赖 self.bot), 借 SimpleNamespace 充当 self;
    # 经 object 中转规避 reportInvalidCast(两类型无重叠, 此处为有意的鸭子类型)
    helper = cast(QQOfficialMessageEvent, cast(object, SimpleNamespace(bot=bot)))
    payload: dict[str, Any] = {"content": plain_text, "msg_id": msg_id}

    if is_group:
        scene = adapter._session_scene.get(session_id)  # pyright: ignore[reportPrivateUsage]
        if scene == "group":
            payload["msg_seq"] = random.randint(1, 10000)
            if image_base64:
                payload["media"] = await helper.upload_group_and_c2c_image(
                    image_base64,
                    QQOfficialMessageEvent.IMAGE_FILE_TYPE,
                    group_openid=session_id,
                )
                payload["msg_type"] = 7
            if record_file_path:
                media = await helper.upload_group_and_c2c_media(
                    record_file_path,
                    QQOfficialMessageEvent.VOICE_FILE_TYPE,
                    group_openid=session_id,
                )
                if media:
                    payload["media"] = media
                    payload["msg_type"] = 7
            if video_file_source:
                media = await helper.upload_group_and_c2c_media(
                    video_file_source,
                    QQOfficialMessageEvent.VIDEO_FILE_TYPE,
                    group_openid=session_id,
                )
                if media:
                    payload["media"] = media
                    payload["msg_type"] = 7
                    payload.pop("msg_id", None)
            if file_source:
                media = await helper.upload_group_and_c2c_media(
                    file_source,
                    QQOfficialMessageEvent.FILE_FILE_TYPE,
                    file_name=file_name,
                    group_openid=session_id,
                )
                if media:
                    payload["media"] = media
                    payload["msg_type"] = 7
                    payload.pop("msg_id", None)
            _ = await bot.api.post_group_message(group_openid=session_id, **payload)
        else:
            # 频道(guild)文本消息: 用 file_image 直传, 不受群/私聊主动消息限制
            if image_path:
                payload["file_image"] = image_path
            _ = await bot.api.post_message(channel_id=session_id, **payload)
    else:
        payload["msg_seq"] = random.randint(1, 10000)
        if image_base64:
            payload["media"] = await helper.upload_group_and_c2c_image(
                image_base64,
                QQOfficialMessageEvent.IMAGE_FILE_TYPE,
                openid=session_id,
            )
            payload["msg_type"] = 7
        if record_file_path:
            media = await helper.upload_group_and_c2c_media(
                record_file_path,
                QQOfficialMessageEvent.VOICE_FILE_TYPE,
                openid=session_id,
            )
            if media:
                payload["media"] = media
                payload["msg_type"] = 7
        if video_file_source:
            media = await helper.upload_group_and_c2c_media(
                video_file_source,
                QQOfficialMessageEvent.VIDEO_FILE_TYPE,
                openid=session_id,
            )
            if media:
                payload["media"] = media
                payload["msg_type"] = 7
        if file_source:
            media = await helper.upload_group_and_c2c_media(
                file_source,
                QQOfficialMessageEvent.FILE_FILE_TYPE,
                file_name=file_name,
                openid=session_id,
            )
            if media:
                payload["media"] = media
                payload["msg_type"] = 7
        _ = await helper.post_c2c_message(openid=session_id, **payload)


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
                await tg_bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        elif name == "lark":
            from lark_oapi.api.im.v1 import (  # pyright: ignore[reportMissingImports]
                DeleteMessageRequest,
            )

            request = DeleteMessageRequest.builder().message_id(message_id).build()
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
        isinstance(duration, int) or (isinstance(duration, str) and duration.isdigit())
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
