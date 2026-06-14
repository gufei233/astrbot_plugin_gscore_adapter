"""AstrBot ↔ gsuid_core 适配器插件入口.

职责(平台侧):
- 插件生命周期: 加载即建立与 core 的 WS 连接, 卸载/重载时优雅断开;
- 监听 AstrBot 全部消息事件, 转换为 MessageReceive 上报 core;
- 监听平台元事件(进群/退群/戳一戳), 单独成包上报(见 meta_event.py);
- GSCORE_ONLY_PREFIXES 命中时拦截 AstrBot 后续 LLM 流程.

协议侧(连接/下发/回执/控制包)见 client.py 与 send_utils.py.
"""

import asyncio
from base64 import b64encode
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import override

import aiofiles
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import At, File, Image, Plain, Reply
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.event_message_type import EventMessageType

from .client import GsClient
from .meta_event import build_meta_receive
from .models import Message as GsMessage
from .models import MessageReceive

PLUGIN_NAME = "astrbot_plugin_gscore_adapter"


def _cfg_str(config: AstrBotConfig, key: str, default: str) -> str:
    """从配置读取字符串项; AstrBotConfig 为弱类型 dict, 统一收窄为 str."""
    val = config.get(key)
    return str(val) if val is not None else default


def _cfg_int(config: AstrBotConfig, key: str, default: int) -> int:
    """从配置读取整数项; 非法值回退默认值."""
    val = config.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _cfg_str_list(config: AstrBotConfig, key: str) -> list[str]:
    """从配置读取字符串列表项, 过滤空串与非字符串元素."""
    val = config.get(key)
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, str) and item]


@register(
    PLUGIN_NAME,
    "KimigaiiWuyi",
    "用于链接SayuCore（早柚核心）的适配器！适用于多种游戏功能, 原神、星铁、绝区零、鸣朝、雀魂等游戏的最佳工具箱！",
    "0.5.0",
)
class GsCoreAdapter(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config: AstrBotConfig = config
        self.GSCORE_ONLY_PREFIXES: list[str] = _cfg_str_list(
            config, "GSCORE_ONLY_PREFIXES"
        )

        self.temp_dir: Path = StarTools.get_data_dir(PLUGIN_NAME) / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.client: GsClient = GsClient(
            context,
            bot_id=_cfg_str(config, "BOT_ID", "AstrBot"),
            host=_cfg_str(config, "IP", "localhost"),
            port=_cfg_str(config, "PORT", "8765"),
            ws_token=_cfg_str(config, "WS_TOKEN", ""),
            max_retry=_cfg_int(config, "MAX_RETRY_TIMES", 30),
            temp_dir=self.temp_dir,
        )

    @override
    async def initialize(self) -> None:
        self._clean_temp_dir()
        await self.client.start()

    @override
    async def terminate(self) -> None:
        await self.client.stop()

    def _clean_temp_dir(self) -> None:
        """清理上次运行遗留的临时文件(file/video 段发送时落盘)."""
        try:
            for f in self.temp_dir.iterdir():
                if f.is_file():
                    f.unlink()
        except OSError as e:
            logger.warning(f"[GsCore] 清理临时目录失败: {e}")

    def _is_gscore_only_message(self, event: AstrMessageEvent) -> bool:
        if not self.GSCORE_ONLY_PREFIXES:
            return False

        raw_text = event.message_str.lstrip()
        if not raw_text:
            return False

        return any(raw_text.startswith(prefix) for prefix in self.GSCORE_ONLY_PREFIXES)

    async def _convert_image(self, image_msg: Image) -> GsMessage | None:
        img_path = getattr(image_msg, "path", None) or getattr(image_msg, "url", None)
        if not img_path:
            logger.warning(f"[GsCore] 图片消息缺少路径: {image_msg}")
            return None

        if isinstance(img_path, str) and img_path.startswith("http"):
            return GsMessage(type="image", data=img_path)

        file_path = Path(str(img_path))
        if not file_path.exists():
            file_path = Path(__file__).parent / str(img_path)
        if not file_path.exists():
            logger.warning(f"[GsCore] 图片文件不存在: {img_path}")
            return None

        async with aiofiles.open(file_path, "rb") as f:
            img_data = await f.read()

        base64_data = b64encode(img_data).decode("utf-8")
        return GsMessage(type="image", data=f"base64://{base64_data}")

    async def _build_single_content(
        self, msg: object, *, from_reply: bool = False
    ) -> list[GsMessage]:
        """把单个 AstrBot 消息段转换为 core 消息段."""
        if isinstance(msg, Image):
            image_data = await self._convert_image(msg)
            return [image_data] if image_data else []
        if isinstance(msg, File):
            if msg.file_:
                file_val = await file_to_base64(Path(msg.file_))
            else:
                file_val = msg.url or ""
            return [GsMessage(type="file", data=f"{msg.name or 'file'}|{file_val}")]
        if isinstance(msg, Plain):
            return [GsMessage(type="text", data=msg.text)]
        if isinstance(msg, At):
            return [GsMessage(type="at", data=str(msg.qq))]

        # 引用消息内经常会带 Json/Face 等 core 不消费的消息段；这些不应阻止
        # 当前消息里的命令文本继续上报。
        if not from_reply:
            logger.warning(f"[GsCore] 不支持的消息类型: {type(msg)}")
        return []

    async def _build_content(self, event: AstrMessageEvent) -> list[GsMessage]:
        """把 AstrBot 消息链转换为上报 core 的 GsMessage 列表.

        AstrBot/OneBot 的引用消息通常排在消息链最前面，例如：
        [Reply(...引用图片...), Plain("ww评分校长")]

        gsuid_core 的命令匹配更依赖当前消息文本。若按原始顺序把 reply/引用图片
        放在最前面，部分 core 插件会先看到 reply/image 段而错过后面的命令文本。
        因此这里优先上报“当前消息”的文本/at/图片等内容，再把 reply 段和引用
        消息里的图片作为上下文附加到末尾。这样 quoted-image + command 可以正常
        触发，同时仍保留被引用图片给需要取图的插件使用。
        """
        current_message: list[GsMessage] = []
        quoted_context: list[GsMessage] = []

        for msg in event.get_messages():
            if isinstance(msg, Reply):
                quoted_context.append(GsMessage(type="reply", data=msg.id))
                # 引用消息内的图片一并上报，供 core 内插件取图。
                for reply_msg in getattr(msg, "chain", None) or []:
                    # 只把 core 常用媒体上下文带过去；忽略 Json/Face 等无关引用段。
                    if isinstance(reply_msg, Image):
                        quoted_context.extend(
                            await self._build_single_content(reply_msg, from_reply=True)
                        )
                continue

            current_message.extend(await self._build_single_content(msg))

        return current_message + quoted_context

    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        # 幂等: 连接循环若已退出(超过最大重试次数)则重新拉起
        await self.client.start()

        pn = event.get_platform_name()
        # bot_id在gscore内部数据库具有唯一标识符，修改将会造成breaking change
        bot_id = "onebot" if pn == "aiocqhttp" else pn
        # bot_self_id 使用平台实例 id, 下发时据此路由回对应平台
        platform_id = event.get_platform_id() or event.get_self_id()
        pm = 1 if event.is_admin() else 6

        # 元事件(进群/退群/戳一戳)优先: 命中则单独成包上报, 不进普通消息流程
        meta_msg = build_meta_receive(event, bot_id, platform_id, pm)
        if meta_msg is not None:
            logger.info(f"【发送】[gsuid-core][Meta]: {meta_msg.content[0].type}")
            await self.client.report(meta_msg)
            return

        content = await self._build_content(event)
        if not content:
            return

        self_id = event.get_self_id()
        user_id = str(event.get_sender_id())
        if pn == "qq_official":
            avatar = f"https://q.qlogo.cn/qqapp/{self_id}/{user_id}/100"
        elif pn == "aiocqhttp":
            avatar = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        else:
            avatar = ""

        msg = MessageReceive(
            bot_id=bot_id,
            bot_self_id=platform_id,
            user_type=(
                "group"
                if event.get_message_type() == MessageType.GROUP_MESSAGE
                else "direct"
            ),
            group_id=event.get_group_id(),
            user_id=user_id,
            sender={"nickname": event.get_sender_name(), "avatar": avatar},
            content=content,
            # 非 onebot 平台下发时以 msg_id 回读会话 id(core 会原样带回)
            msg_id=event.get_session_id(),
            user_pm=pm,
        )
        logger.info(f"【发送】[gsuid-core]: {msg.bot_id}")
        await self.client.report(msg)

        if self._is_gscore_only_message(event):
            # 按 AstrBot 文档显式阻断事件传播, 不参与后续 LLM 等流程
            event.stop_event()
            logger.info(
                "[GsCore] 当前消息命中GSCORE_ONLY_PREFIXES，已调用 stop_event() 拦截后续 AstrBot LLM 流程"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("连接core", alias={"链接core"})
    async def connect_core(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """手动重连 gsuid_core."""
        await self.client.start()
        for _ in range(6):
            if self.client.is_connected:
                break
            await asyncio.sleep(0.5)
        if self.client.is_connected:
            yield event.plain_result("链接成功！")
        else:
            yield event.plain_result("正在尝试连接core, 请稍后通过日志确认连接状态...")


async def file_to_base64(file_path: Path) -> str:
    async with aiofiles.open(str(file_path), "rb") as file:
        file_content = await file.read()
    return b64encode(file_content).decode("utf-8")
