from typing import Any, Literal

from msgspec import Struct


class Message(Struct):
    type: str | None = None
    data: Any | None = None


class MessageReceive(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: str | None = None
    user_id: str | None = None
    sender: dict[str, Any] = {}
    user_pm: int = 3
    content: list[Message] = []


class MessageContent(Struct):
    raw: MessageReceive | None = None
    raw_text: str = ""
    command: str | None = None
    text: str | None = None
    image: str | None = None
    at: str | None = None
    image_list: list[Any] = []
    at_list: list[Any] = []


class MessageSend(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: str | None = None
    target_id: str | None = None
    content: list[Message] | None = None
    # 回执关联令牌；core 仅在请求 recall_message_id 时下发，非空才需回执
    echo: str | None = None
