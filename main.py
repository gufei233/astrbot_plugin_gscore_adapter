import asyncio
import base64
import os
from base64 import b64encode
from pathlib import Path
from typing import List, Dict

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
# 🔧 新增：用于记录已创建的实例，防止重复
_instances: Dict[str, 'GsCoreAdapter'] = {}


@register(
    "astrbot_plugin_gscore_adapter",
    "KimigaiiWuyi",
    "用于链接SayuCore（早柚核心）的适配器！适用于多种游戏功能, 原神、星铁、绝区零、鸣朝、雀魂等游戏的最佳工具箱！",
    "0.5.6",
)
class GsCoreAdapter(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.is_connect = False
        self.BOT_ID = self.config.BOT_ID
        self.IP = self.config.IP
        self.PORT = self.config.PORT
        # 缓存 bot_self_id 映射 {bot_id: bot_self_id}
        self.bot_self_id_cache: Dict[str, str] = {}
        
        # 检查并注册单例
        instance_key = f"{self.IP}:{self.PORT}:{self.BOT_ID}"
        if instance_key in _instances:
            logger.warning(f'[gsuid-core] 检测到重复实例: {instance_key}，使用已有实例')
            # 复用已有实例的连接
            existing = _instances[instance_key]
            self.ws = existing.ws
            self.is_connect = existing.is_connect
            self.msg_list = existing.msg_list
            self.pending = existing.pending
            self.bot_self_id_cache = existing.bot_self_id_cache
        else:
            _instances[instance_key] = self
            logger.info(f'[gsuid-core] 注册新实例: {instance_key}')

    async def async_connect(self):
        self.is_alive = True
        self.ws_url = f'ws://{self.IP}:{self.PORT}/ws/{self.BOT_ID}'
        logger.info(f'Bot_ID: {self.BOT_ID}连接至[gsuid-core]: {self.ws_url}...')
        self.ws = await websockets.client.connect(
            self.ws_url, max_size=2**26, open_timeout=60, ping_timeout=60
        )
        logger.info(f'与[gsuid-core]成功连接! Bot_ID: {self.BOT_ID}')
        self.is_connect = True
        self.msg_list = asyncio.queues.Queue()
        self.pending = []

    async def connect(self):
        global gsconnecting
        
        # 先检查是否已连接
        instance_key = f"{self.IP}:{self.PORT}:{self.BOT_ID}"
        if instance_key in _instances:
            existing = _instances[instance_key]
            if existing.is_connect and existing is not self:
                logger.info(f'[gsuid-core] 复用已有连接: {instance_key}')
                self.ws = existing.ws
                self.is_connect = True
                self.msg_list = existing.msg_list
                self.pending = existing.pending
                return
        
        if not gsconnecting and not self.is_connect:
            gsconnecting = True
            try:
                await self.async_connect()
                logger.info('[gsuid-core]: 发起一次连接')
                await self.start()
            except ConnectionRefusedError:
                logger.error(
                    '[链接错误] Core服务器连接失败...请确认是否根据文档安装【早柚核心】！'
                )
            except Exception as e:
                logger.error(f'[gsuid-core] 连接异常: {e}')
            finally:
                gsconnecting = False

    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if self.is_connect is False:
            await self.connect()

        if not hasattr(self, 'ws') or self.ws is None:
            logger.error(
                '[链接错误] Core服务器连接失败...请确认是否根据文档安装【早柚核心】！'
            )
            return

        try:
            await self.ws.ping()
        except ConnectionClosed:
            await self.connect()

        user_name = event.get_sender_name()

        logger.debug(event.unified_msg_origin)

        message_chain = event.get_messages()
        logger.info(message_chain)

        pn = event.get_platform_name()
        sender = {
            'nickname': user_name,
        }

        # 修复 bot_self_id 为空的问题
        self_id = event.get_self_id()
        if not self_id or self_id == "None":
            try:
                platforms = self.context.platform_manager.get_insts()
                for platform in platforms:
                    platform_name = platform.__class__.__name__.lower()
                    if pn.lower() in platform_name:
                        if hasattr(platform, 'bot_id'):
                            self_id = str(platform.bot_id)
                        elif hasattr(platform, 'self_id'):
                            self_id = str(platform.self_id)
                        break
            except Exception as e:
                logger.warning(f"无法获取 bot_self_id: {e}")
            
            if not self_id or self_id == "None":
                self_id = self.BOT_ID
        
        user_id = str(event.get_sender_id())
        if pn == 'qq_official':
            avatar = f'https://q.qlogo.cn/qqapp/{self_id}/{user_id}/100'
        elif pn == 'aiocqhttp':
            avatar = f'https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640'
        else:
            avatar = ''
        sender['avatar'] = avatar

        message: List[GsMessage] = []
        for msg in message_chain:
            if isinstance(msg, Image):
                img_path = msg.path
                if not img_path:
                    img_path = msg.url
                if img_path:
                    if img_path.startswith('http'):
                        message.append(
                            GsMessage(
                                type='image',
                                data=img_path,
                            )
                        )
                    else:
                        if not os.path.exists(img_path):
                            img_path = Path(__file__).parent / img_path
                        if os.path.exists(img_path):
                            async with aiofiles.open(img_path, 'rb') as f:
                                img_data = await f.read()
                            base64_data = b64encode(img_data).decode('utf-8')
                            message.append(
                                GsMessage(
                                    type='image',
                                    data=f'base64://{base64_data}',
                                )
                            )
                        else:
                            logger.warning(f"图片文件不存在: {img_path}")
            elif isinstance(msg, File):
                if msg.file and msg.name:
                    if msg.file_:
                        file_val = await file_to_base64(Path(msg.file_))
                    else:
                        file_val = msg.url
                    file_name = msg.name
                    message.append(
                        GsMessage(
                            type='file',
                            data=f'{file_name}|{file_val}',
                        )
                    )
            elif isinstance(msg, Plain):
                message.append(
                    GsMessage(
                        type='text',
                        data=msg.text,
                    )
                )
            elif isinstance(msg, At):
                message.append(
                    GsMessage(
                        type='at',
                        data=str(msg.qq),
                    )
                )
            elif isinstance(msg, Reply):
                message.append(
                    GsMessage(
                        type='reply',
                        data=msg.id,
                    )
                )
                if hasattr(msg, 'chain') and msg.chain:
                    for reply_component in msg.chain:
                        if isinstance(reply_component, Image):
                            img_path = reply_component.path
                            if not img_path:
                                img_path = reply_component.url
                            if img_path:
                                if img_path.startswith('http'):
                                    message.append(
                                        GsMessage(
                                            type='image',
                                            data=img_path,
                                        )
                                    )
                                else:
                                    if not os.path.exists(img_path):
                                        img_path = Path(__file__).parent / img_path
                                    if os.path.exists(img_path):
                                        async with aiofiles.open(img_path, 'rb') as f:
                                            img_data = await f.read()
                                        base64_data = b64encode(img_data).decode('utf-8')
                                        message.append(
                                            GsMessage(
                                                type='image',
                                                data=f'base64://{base64_data}',
                                            )
                                        )
                                    else:
                                        logger.warning(f"引用消息中的图片文件不存在: {img_path}")
            else:
                logger.warning(f'不支持的消息类型: {type(msg)}')

        user_type = (
            'group'
            if event.get_message_type() == MessageType.GROUP_MESSAGE
            else 'direct'
        )
        pm = 1 if event.is_admin() else 6

        platform_id = event.get_platform_id()
        if not platform_id or platform_id == "None":
            platform_id = self_id
        
        bot_id = 'onebot' if pn == 'aiocqhttp' else pn
        self.bot_self_id_cache[bot_id] = platform_id
        logger.debug(f'缓存 bot_self_id: {bot_id} -> {platform_id}')
            
        msg = MessageReceive(
            bot_id=bot_id,
            bot_self_id=platform_id,
            user_type=user_type,
            group_id=event.get_group_id(),
            user_id=user_id,
            sender=sender,
            content=message,
            msg_id=event.get_session_id(),
            user_pm=pm,
        )
        logger.info(f'【发送】[gsuid-core]: {msg.bot_id} - bot_self_id: {msg.bot_self_id}')
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

    async def recv_msg(self):
        try:
            await asyncio.sleep(5)
            logger.info('[gsuid-core] 开始监听 WebSocket 消息...')
            async for message in self.ws:
                try:
                    msg = msgjson.decode(message, type=MessageSend)
                    logger.info(
                        f'【接收】[gsuid-core]: '
                        f'{msg.bot_id} - {msg.target_type} - {msg.target_id}'
                    )
                    
                    if msg.bot_id == 'AstrBot':
                        if msg.content:
                            _data = msg.content[0]
                            if _data.type and _data.type.startswith('log'):
                                _type = _data.type.split('_')[-1].lower()
                                getattr(logger, _type)(_data.data)
                        continue

                    bid = msg.bot_id

                    if not msg.target_id:
                        logger.warning(f'[GsCore] 消息没有target_id: {msg}')
                        continue

                    if not msg.content:
                        logger.warning(f'[GsCore] 消息没有content: {msg}')
                        continue

                    bot_self_id = msg.bot_self_id
                    
                    if not bot_self_id or bot_self_id == "None" or bot_self_id == self.BOT_ID:
                        bot_self_id = self.bot_self_id_cache.get(bid)
                        
                        if not bot_self_id:
                            logger.warning(f'[GsCore] 无法找到 {bid} 的 bot_self_id 缓存，尝试从平台获取')
                            try:
                                platforms = self.context.platform_manager.get_insts()
                                for platform in platforms:
                                    if hasattr(platform, 'bot_id'):
                                        bot_self_id = str(platform.bot_id)
                                        break
                                    elif hasattr(platform, 'self_id'):
                                        bot_self_id = str(platform.self_id)
                                        break
                            except Exception as e:
                                logger.error(f'获取 bot_self_id 失败: {e}')
                                bot_self_id = self.BOT_ID
                    
                    session = MessageSesion(
                        bot_self_id,
                        (
                            MessageType.GROUP_MESSAGE
                            if msg.target_type == 'group'
                            else MessageType.FRIEND_MESSAGE
                        ),
                        msg.target_id,
                    )
                    logger.info(f'【准备发送】session: {bot_self_id} - {msg.target_type} - {msg.target_id}')
                    await self.bot_send_msg(msg.content, session, bid)
                except Exception as e:
                    logger.exception(f'[gsuid-core] 处理消息异常: {e}')
        except RuntimeError as e:
            logger.error(f'[gsuid-core] RuntimeError: {e}')
        except ConnectionClosedError as e:
            logger.error(f'[gsuid-core] ConnectionClosedError: {e}')
            for task in self.pending:
                task.cancel()
            logger.warning(f'与[gsuid-core]断开连接! Bot_ID: {self.BOT_ID}')
            self.is_alive = False
            self.is_connect = False
            for _ in range(30):
                await asyncio.sleep(5)
                try:
                    await self.async_connect()
                    await self.start()
                    break
                except:  # noqa
                    logger.debug('自动连接core服务器失败...五秒后重新连接...')
        except Exception as e:
            logger.exception(f'[gsuid-core] recv_msg 未知错误: {e}')

    async def _to_msg(
        self, msg: List[GsMessage], bot_id: str
    ) -> List[BaseMessageComponent]:
        message = []
        for _c in msg:
            if _c.data:
                if _c.type == 'text':
                    message.append(Plain(_c.data))
                elif _c.type == 'image':
                    if _c.data.startswith('link://'):
                        message.append(Image.fromURL(_c.data[7:]))
                    elif _c.data.startswith('http://') or _c.data.startswith('https://'):
                        message.append(Image.fromURL(_c.data))
                    else:
                        if _c.data.startswith('base64://'):
                            _c.data = _c.data[9:]
                        message.append(
                            Image.fromBase64(_c.data),  # type: ignore
                        )
                elif _c.type == 'node':
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
                    file_name, file_content = _c.data.split('|', 1)
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
    async with aiofiles.open(str(file_path), 'rb') as file:
        file_content = await file.read()
    base64_encoded = b64encode(file_content)
    base64_string = base64_encoded.decode('utf-8')
    return base64_string
