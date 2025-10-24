import asyncio
import base64
import os
from base64 import b64encode
from pathlib import Path
from typing import List, Optional, Set

import aiofiles
import websockets.client
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Nodes,
    Node,
    Plain,
    Reply,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.event_message_type import EventMessageType
from msgspec import json as msgjson
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from .models import Message as GsMessage
from .models import MessageReceive, MessageSend

gsconnecting = False


@register(
    "astrbot_plugin_gscore_adapter",
    "KimigaiiWuyi",
    "用于链接SayuCore（早柚核心）的适配器！适用于多种游戏功能, 原神、星铁、绝区零、鸣朝、雀魂等游戏的最佳工具箱！",
    "0.6",
)
class GsCoreAdapter(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.is_connect = False
        self.BOT_ID = self.config.BOT_ID
        self.IP = self.config.IP
        self.PORT = self.config.PORT

    async def async_connect(
        self,
    ):
        self.is_alive = True
        self.ws_url = f'ws://{self.IP}:{self.PORT}/ws/{self.BOT_ID}'
        logger.info(f'Bot_ID: {self.BOT_ID}连接至[gsuid-core]: {self.ws_url}...')
        self.ws = await websockets.client.connect(  # type: ignore
            self.ws_url, max_size=2**26, open_timeout=60, ping_timeout=60
        )
        logger.info(f'与[gsuid-core]成功连接! Bot_ID: {self.BOT_ID}')
        self.is_connect = True
        self.msg_list = asyncio.queues.Queue()
        self.pending = []

    async def connect(self):
        global gsconnecting
        if not gsconnecting and not self.is_connect:
            gsconnecting = True
            try:
                await self.async_connect()
                logger.info('[gsuid-core]: 发起一次连接')
                await self.start()
                gsconnecting = False
            except ConnectionRefusedError:
                gsconnecting = False
                logger.error(
                    '[链接错误] Core服务器连接失败...请确认是否根据文档安装【早柚核心】！'
                )


    async def _normalize_image_component(self, img: Image) -> Optional[str]:
        """
        将 Image 组件统一为可发送到 Core 的字符串：
        - http(s) 链接原样返回
        - 本地文件读取为 base64://xxxxx
        - 失败返回 None
        """
        img_path = getattr(img, "path", None) or getattr(img, "url", None)
        if not img_path:
            return None

        img_path = str(img_path)

        if img_path.startswith('http://') or img_path.startswith('https://'):
            return img_path

        candidate = Path(img_path)
        if not candidate.exists():
            candidate = Path(__file__).parent / img_path

        if candidate.exists():
            try:
                async with aiofiles.open(candidate, 'rb') as f:
                    img_data = await f.read()
                base64_data = b64encode(img_data).decode('utf-8')
                return f'base64://{base64_data}'
            except Exception as e:
                logger.warning(f'读取本地图片失败: {candidate} | err={e}')
                return None
        else:
            logger.warning(f'图片路径不存在: {img_path}')
            return None

    async def _extract_images_from_any_chain(self, chain) -> List[str]:
        """从一个消息链（list-like）里提取所有 Image 的可发送数据"""
        out: List[str] = []
        if not chain:
            return out
        for seg in chain:
            if isinstance(seg, Image):
                norm = await self._normalize_image_component(seg)
                if not norm:
                    # 兜底再尝试 to_dict
                    try:
                        d = await seg.to_dict()
                        u = (d.get("data") or {}).get("url") or (d.get("data") or {}).get("file")
                        if u:
                            out.append(u)
                            continue
                    except Exception:
                        pass
                else:
                    out.append(norm)
        return out

    async def _extract_images_from_reply(self, event: AstrMessageEvent, reply_seg: Reply) -> List[str]:
        """
        从 Reply 段中提取被引用消息的图片：
        1) 优先读取 reply_seg.chain
        2) 其次兼容 reply_seg.message / reply_seg.messages
        3) 若仍无，在 aiocqhttp 平台下使用 get_msg 回拉原消息
        """
        images: List[str] = []

        for attr in ("chain", "message", "messages"):
            quoted_chain = getattr(reply_seg, attr, None)
            if quoted_chain:
                imgs = await self._extract_images_from_any_chain(quoted_chain)
                images.extend(imgs)
                if imgs:
                    break  

        if not images:
            quoted_id = getattr(reply_seg, "id", None)
            pn = event.get_platform_name()
            if quoted_id and pn == 'aiocqhttp':
                bot = getattr(event, "bot", None)
                api = getattr(bot, "api", None) if bot else None
                if api:
                    try:
                        try:
                            mid = int(quoted_id)
                        except Exception:
                            mid = quoted_id
                        raw = await api.get_msg(message_id=mid)
                        for s in raw.get("message", []) or []:
                            if isinstance(s, dict) and s.get("type") == "image":
                                u = (s.get("data") or {}).get("url") or (s.get("data") or {}).get("file")
                                if u:
                                    images.append(u)
                    except Exception as e:
                        logger.warning(f'[gscore_adapter] 拉取引用原消息失败: id={quoted_id}, err={e}')
        return images


    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if self.is_connect is False:
            await self.connect()

        if not hasattr(self, 'ws'):
            logger.error(
                '[链接错误] Core服务器连接失败...请确认是否根据文档安装【早柚核心】！'
            )

        assert self.ws is not None
        try:
            await self.ws.ping()
        except ConnectionClosed:
            await self.connect()

        user_name = event.get_sender_name()
        logger.debug(event.unified_msg_origin)

        message_chain = event.get_messages()  # 用户消息链
        logger.info(message_chain)

        pn = event.get_platform_name()
        sender = {
            'nickname': user_name,
        }

        self_id = event.get_self_id()
        user_id = str(event.get_sender_id())
        if pn == 'qq_official':
            avatar = f'https://q.qlogo.cn/qqapp/{self_id}/{user_id}/100'
        elif pn == 'aiocqhttp':
            avatar = f'https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640'
        else:
            avatar = ''
        sender['avatar'] = avatar

        # ----------------------- 构造待发送的消息 引用图片收集 -----------------------
        gs_messages: List[GsMessage] = []
        image_seen: Set[str] = set()

        async def _append_image_data(data_str: Optional[str]):
            if not data_str:
                return
            if data_str not in image_seen:
                image_seen.add(data_str)
                gs_messages.append(GsMessage(type='image', data=data_str))

        # 当前消息中的图片
        for seg in message_chain:
            if isinstance(seg, Image):
                norm = await self._normalize_image_component(seg)
                await _append_image_data(norm)

        # 处理 Reply
        for seg in message_chain:
            if isinstance(seg, Reply):
                gs_messages.append(GsMessage(type='reply', data=seg.id))
                try:
                    quoted_imgs = await self._extract_images_from_reply(event, seg)
                    for u in quoted_imgs:
                        await _append_image_data(u)
                except Exception as e:
                    logger.warning(f'[gscore_adapter] 解析引用图片失败: {e}')

        # 其它文本/文件/at
        for seg in message_chain:
            if isinstance(seg, Plain):
                gs_messages.append(GsMessage(type='text', data=seg.text))
            elif isinstance(seg, At):
                gs_messages.append(GsMessage(type='at', data=str(seg.qq)))
            elif isinstance(seg, File):
                if seg.file and seg.name:
                    if seg.file_:
                        file_val = await file_to_base64(Path(seg.file_))
                    else:
                        file_val = seg.url
                    file_name = seg.name
                    gs_messages.append(GsMessage(type='file', data=f'{file_name}|{file_val}'))

        # 打印收集到的图片数量与示例
        if image_seen:
            example = next(iter(image_seen))
            logger.info(f'[gscore_adapter] 已收集图片数量: {len(image_seen)} | 示例: {example[:120]}...')
        else:
            logger.info('[gscore_adapter] 未收集到任何图片（当前消息与引用均为空）')

        user_type = (
            'group'
            if event.get_message_type() == MessageType.GROUP_MESSAGE
            else 'direct'
        )
        pm = 1 if event.is_admin() else 6

        platform_id = event.get_platform_id()
        msg = MessageReceive(
            bot_id='onebot' if pn == 'aiocqhttp' else pn,
            bot_self_id=platform_id,
            user_type=user_type,
            group_id=event.get_group_id(),
            user_id=user_id,
            sender=sender,
            content=gs_messages,
            msg_id=event.get_session_id(),
            user_pm=pm,
        )
        logger.info(f'【发送】[gsuid-core]: {msg.bot_id}')
        await self._input(msg)

    async def _input(self, msg: MessageReceive):
        await self.msg_list.put(msg)

    async def send_msg(self):
        while True:
            msg: MessageReceive = await self.msg_list.get()
            msg_send = msgjson.encode(msg)
            await self.ws.send(msg_send)

    async def start(self):
        asyncio.create_task(self.recv_msg())
        asyncio.create_task(self.send_msg())
        # _, self.pending = await asyncio.wait(
        #     [recv_task, send_task],
        #     return_when=asyncio.FIRST_COMPLETED,
        # )

    async def recv_msg(self):
        try:
            await asyncio.sleep(5)
            async for message in self.ws:
                try:
                    msg = msgjson.decode(message, type=MessageSend)
                    logger.info(
                        f'【接收】[gsuid-core]: '
                        f'{msg.bot_id} - {msg.target_type} - {msg.target_id}'
                    )
                    # 解析消息
                    if msg.bot_id == 'AstrBot':
                        if msg.content:
                            _data = msg.content[0]
                            if _data.type and _data.type.startswith('log'):
                                _type = _data.type.split('_')[-1].lower()
                                getattr(logger, _type)(_data.data)
                        continue

                    bid = msg.bot_id
                    session_id = msg.target_id

                    if session_id is None:
                        logger.warning(f'[GsCore] 消息{msg}没有session_id')
                        continue

                    if msg.target_id and msg.content:
                        session = MessageSesion(
                            msg.bot_self_id,
                            (
                                MessageType.GROUP_MESSAGE
                                if msg.target_type == 'group'
                                else MessageType.FRIEND_MESSAGE
                            ),
                            session_id,
                        )
                        await self.bot_send_msg(msg.content, session, bid)
                except Exception as e:
                    logger.exception(e)
        except RuntimeError:
            pass
        except ConnectionClosedError:
            for task in self.pending:
                task.cancel()
            logger.warning(f'与[gsuid-core]断开连接! Bot_ID: {self.BOT_ID}')
            self.is_alive = False
            for _ in range(30):
                await asyncio.sleep(5)
                try:
                    await self.async_connect()
                    await self.start()
                    break
                except:  # noqa
                    logger.debug('自动连接core服务器失败...五秒后重新连接...')

    async def _to_msg(
        self, msg: List[GsMessage], bot_id: str
    ) -> List[BaseMessageComponent]:
        message = []
        for _c in msg:
            if _c.data:
                if _c.type == 'text':
                    message.append(Plain(_c.data))
                elif _c.type == 'image':
                    if isinstance(_c.data, str) and _c.data.startswith('link://'):
                        message.append(Image.fromURL(_c.data[7:]))
                    else:
                        data_str = _c.data
                        if isinstance(data_str, str) and data_str.startswith('base64://'):
                            data_str = data_str[9:]
                        message.append(
                            Image.fromBase64(data_str),  # type: ignore
                        )
                elif _c.type == 'node':
                    # 特殊处理 qq 平台
                    if bot_id == 'onebot':
                        node_message: List[Node] = []
                        for _node in _c.data:
                            node_message.append(
                                Node(
                                    await self._to_msg(
                                        [GsMessage(**_node)],
                                        bot_id,
                                    )
                                )
                            )

                        # 将一条消息转为多条消息，优化观感
                        message.append(
                            Nodes(
                                node_message,
                            )
                        )
                    else:
                        for _node in _c.data:
                            message.extend(
                                await self._to_msg(
                                    [GsMessage(**_node)],
                                    bot_id,
                                )
                            )
                elif _c.type == 'file':
                    file_name, file_content = _c.data.split('|')
                    path = Path(__file__).resolve().parent / file_name
                    store_file(path, file_content)
                    message.append(File(file_name, str(path)))
                elif _c.type == 'at':
                    message.append(At(qq=_c.data))
        return message

    async def bot_send_msg(
        self,
        gsmsgs: List[GsMessage],
        session: MessageSesion,
        bot_id: str,
    ):
        messages = MessageChain()
        message = await self._to_msg(gsmsgs, bot_id)

        messages.chain.extend(message)
        logger.info(f'【即将发送】[gsuid-core]: {messages}')
        await self.context.send_message(session, messages)


def store_file(path: Path, file: str):
    file_content = base64.b64decode(file)
    with open(path, 'wb') as f:
        f.write(file_content)


def del_file(path: Path):
    if path.exists():
        os.remove(path)


async def file_to_base64(file_path: Path):
    # 读取文件内容
    async with aiofiles.open(str(file_path), 'rb') as file:
        file_content = await file.read()

    # 将文件内容转换为base64编码
    base64_encoded = b64encode(file_content)

    # 将base64编码的字节转换为字符串
    base64_string = base64_encoded.decode('utf-8')

    return base64_string
