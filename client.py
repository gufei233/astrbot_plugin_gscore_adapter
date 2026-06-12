"""与 [gsuid-core] 的 WebSocket 连接客户端.

职责(协议侧, 不含任何平台事件监听):
- 维护唯一 WS 连接: 建连/断线自动重连/优雅关闭;
- 上报队列串行化发送(断连期间暂存, 重连后继续发出);
- 下发分发: log 回显包 / excute_delete_message 控制包 / 普通消息;
- recall_message_id 回执: 凡 MessageSend.echo 非空, 发送后必回执
  (即便未拿到平台消息 id 也回 id=None, 让 core 立即结算该帧).
"""

import asyncio
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.context import Context
from msgspec import json as msgjson
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from .models import Message as GsMessage
from .models import MessageReceive, MessageSend
from .send_utils import aiocqhttp_send, del_msg, gs_to_components, qqofficial_send

RECONNECT_INTERVAL = 5  # 秒

# 下发时直接用 target_id 作为会话 id 的平台;
# 其余平台回读上报时塞进 msg_id 的 AstrBot 会话 id
_TARGET_ID_PLATFORMS = ("onebot", "aiocqhttp", "dingtalk", "lark", "wechatpadpro")


class GsClient:
    """维护与 [gsuid-core] 的 WS 连接: 上报队列 + 下发分发 + 断线重连."""

    def __init__(
        self,
        context: Context,
        bot_id: str,
        host: str,
        port: str,
        ws_token: str,
        max_retry: int,
        temp_dir: Path,
    ) -> None:
        self.context: Context = context
        self.bot_id: str = bot_id
        self.host: str = host
        self.port: str = port
        self.ws_token: str = ws_token
        self.max_retry: int = max_retry
        self.temp_dir: Path = temp_dir

        self._ws: ClientConnection | None = None
        self._queue: asyncio.Queue[MessageReceive] = asyncio.Queue()
        self._supervisor: asyncio.Task[None] | None = None
        self._running: bool = False

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and not self._supervisor.done()

    async def start(self) -> None:
        """启动(或拉起已退出的)连接主循环; 幂等."""
        if self.is_running:
            return
        self._running = True
        self._supervisor = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停止连接主循环并关闭连接(插件卸载/重载时调用)."""
        self._running = False
        if self._supervisor is not None:
            _ = self._supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def report(self, msg: MessageReceive) -> None:
        """上报消息入队; 断连期间暂存, 重连后由发送循环补发."""
        if not self.is_running:
            logger.warning("[GsCore] 尚未连接, 消息丢弃")
            return
        await self._queue.put(msg)

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        retry = 0
        while self._running:
            try:
                ws = await self._connect()
            except Exception as e:
                retry += 1
                if self.max_retry != -1 and retry > self.max_retry:
                    logger.error(
                        f"[GsCore] 已达到最大重试次数 ({self.max_retry}), 停止重试连接"
                    )
                    break
                logger.warning(
                    f"[链接错误] Core服务器连接失败: {e} ...请确认是否根据文档安装【早柚核心】! "
                    + f"{RECONNECT_INTERVAL}秒后重试 (第 {retry} 次)"
                )
                await asyncio.sleep(RECONNECT_INTERVAL)
                continue
            retry = 0
            await self._serve(ws)
            if not self._running:
                break
            logger.warning(f"与[gsuid-core]断开连接! Bot_ID: {self.bot_id}")
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _connect(self) -> ClientConnection:
        ws_url = f"ws://{self.host}:{self.port}/ws/{self.bot_id}"
        logger.info(f"Bot_ID: {self.bot_id}连接至[gsuid-core]: {ws_url}...")
        if self.ws_token:
            ws_url += f"?token={self.ws_token}"
        ws = await ws_connect(ws_url, max_size=2**26, open_timeout=3, ping_timeout=10)
        self._ws = ws
        logger.info(f"与[gsuid-core]成功连接! Bot_ID: {self.bot_id}")
        return ws

    async def _serve(self, ws: ClientConnection) -> None:
        """收包主循环; 返回即连接断开. 单包异常不影响整体循环."""
        send_task = asyncio.create_task(self._send_loop(ws))
        try:
            async for raw in ws:
                try:
                    await self._handle_packet(raw)
                except Exception as e:
                    logger.exception(e)
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.exception(e)
        finally:
            _ = send_task.cancel()
            with suppress(asyncio.CancelledError):
                await send_task

    async def _send_loop(self, ws: ClientConnection) -> None:
        while True:
            msg = await self._queue.get()
            try:
                await ws.send(msgjson.encode(msg))
            except Exception:
                # 发送失败(连接中断): 放回队列待重连后补发
                self._queue.put_nowait(msg)
                return

    # ------------------------------------------------------------------
    # 下发分发
    # ------------------------------------------------------------------
    async def _handle_packet(self, raw: "str | bytes") -> None:
        msg = msgjson.decode(raw, type=MessageSend)

        # core 请求在 Bot 侧打印日志的回显包, 不当普通消息发送
        if msg.bot_id == self.bot_id:
            if msg.content:
                _data = msg.content[0]
                if _data.type and _data.type.startswith("log"):
                    _type = _data.type.split("_")[-1].lower()
                    _ = getattr(logger, _type, logger.info)(_data.data)
            return

        logger.info(
            f"【接收】[gsuid-core]: {msg.bot_id} - {msg.target_type} - {msg.target_id}"
        )

        # 撤回控制包(单段 excute_delete_message): 短路到平台撤回 API,
        # 不当普通消息发送, 避免误发空消息
        if (
            msg.content
            and len(msg.content) == 1
            and msg.content[0].type == "excute_delete_message"
        ):
            await del_msg(self.context, msg)
            return

        # 平台真实出站消息 id; 仅 aiocqhttp 直发路径会回传
        recall_id: str | list[str] | None = None
        try:
            recall_id = await self._send_to_platform(msg)
        finally:
            # 只要 core 要求回执(echo 非空)就回执, 即便没拿到 id 或发送异常:
            # 让 core 立即结算该帧, 避免空等 RECALL_WAIT_TIMEOUT
            if msg.echo:
                await self._send_recall_receipt(msg, recall_id)

    async def _send_to_platform(self, msg: MessageSend) -> str | list[str] | None:
        if not (msg.target_id and msg.content):
            return None

        if msg.bot_id in _TARGET_ID_PLATFORMS:
            session_id = msg.target_id
        else:
            session_id = msg.msg_id
        if not session_id:
            logger.warning(f"[GsCore] 消息{msg}没有session_id")
            return None

        platform = self.context.get_platform_inst(msg.bot_self_id)
        components = await gs_to_components(
            msg.content, msg.bot_id, platform, self.temp_dir
        )
        if not components:
            return None
        chain = MessageChain()
        chain.chain.extend(components)
        logger.info(f"【即将发送】[gsuid-core]: {chain}")

        is_group = msg.target_type == "group"

        # aiocqhttp 直发以捕获平台出站 message_id(供 wait_recall 回执);
        # 其余平台走 AstrBot 通用会话发送(拿不到 id, 回执 id=None)
        if platform is not None and platform.meta().name == "aiocqhttp":
            sid = session_id.split("_")[-1] if is_group else session_id
            if sid.isdigit():
                return await aiocqhttp_send(platform, chain, is_group, sid)

        # qq_official 通用发送会丢弃/污染 msg_id 退化为主动消息(无权限);
        # 直发并复用缓存的入站 msg_id 走被动回复, 详见 qqofficial_send
        if platform is not None and platform.meta().name == "qq_official":
            await qqofficial_send(
                platform,
                chain,
                is_group,
                session_id,
            )
            return None

        session = MessageSession(
            msg.bot_self_id,
            MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE,
            session_id,
        )
        _ = await self.context.send_message(session, chain)
        return None

    async def _send_recall_receipt(
        self,
        msg: MessageSend,
        recall_id: str | list[str] | None,
    ) -> None:
        """回传 recall_message_id 回执.

        复用上行 MessageReceive 通道, content 仅含单段 recall_message_id,
        data 自带 echo(原样回传供 core 关联)与 id(平台真实出站消息id).
        id 为 None 表示本帧未拿到平台消息id(core 会结算该帧但不计入返回);
        id 为 list 表示一帧被平台展开为多条消息, core 会 flatten 进扁平结果.
        """
        receipt = MessageReceive(
            bot_id=msg.bot_id,
            bot_self_id=msg.bot_self_id,
            user_id="",
            content=[
                GsMessage(
                    type="recall_message_id",
                    data={"echo": msg.echo, "id": recall_id},
                )
            ],
        )
        await self.report(receipt)
