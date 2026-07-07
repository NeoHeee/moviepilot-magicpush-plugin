from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class MagicPushMsg(_PluginBase):
    """
    将 MoviePilot V2 的通知消息转发到 MagicPush。
    """

    plugin_name = "MagicPush消息通知"
    plugin_desc = "将MoviePilot通知转发到MagicPush，并支持通知类型筛选、海报和跳转链接。"
    plugin_icon = "magicpush.png"
    plugin_version = "1.1.0"
    plugin_author = "Neo"
    author_url = "https://github.com/magiccode1412/magicpush"
    plugin_config_prefix = "magicpushmsg_"
    plugin_order = 28
    auth_level = 1

    _enabled = False
    _onlyonce = False
    _server = ""
    _token = ""
    _content_type = "markdown"
    _msgtypes = []
    _append_image = True
    _title_prefix = ""
    _forward_channel_messages = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._server = (config.get("server") or "").strip()
            self._token = (config.get("token") or "").strip()
            self._content_type = config.get("content_type") or "markdown"
            self._msgtypes = config.get("msgtypes") or []
            self._append_image = bool(config.get("append_image", True))
            self._title_prefix = (config.get("title_prefix") or "").strip()
            self._forward_channel_messages = bool(
                config.get("forward_channel_messages", False)
            )

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "server": self._server,
                "token": self._token,
                "content_type": self._content_type,
                "msgtypes": self._msgtypes,
                "append_image": self._append_image,
                "title_prefix": self._title_prefix,
                "forward_channel_messages": self._forward_channel_messages,
            })
            ok, message = self._send(
                title="MagicPush 测试通知",
                text="MoviePilot V2 与 MagicPush 的通知连接正常。",
                image=None,
                link=None,
            )
            if ok:
                logger.info("MagicPush测试通知发送成功")
            else:
                logger.warning(f"MagicPush测试通知发送失败：{message}")

    def get_state(self) -> bool:
        return self._enabled and bool(self._server) and bool(self._token)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        msg_type_options = [
            {"title": item.value, "value": item.name}
            for item in NotificationType
        ]
        content_type_options = [
            {"title": "Markdown（推荐）", "value": "markdown"},
            {"title": "纯文本", "value": "text"},
            {"title": "HTML", "value": "html"},
        ]

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "发送测试通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "append_image",
                                            "label": "正文附加海报",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "forward_channel_messages",
                                            "label": "转发客户端定向消息（命令回复）",
                                            "hint": "开启后，微信或Telegram命令的普通文本回复也会同步到MagicPush",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "server",
                                            "label": "MagicPush地址",
                                            "placeholder": "http://192.168.1.10:3000",
                                            "hint": "填写MagicPush根地址，不需要填写/api/push/",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "token",
                                            "label": "接口Token",
                                            "placeholder": "MagicPush接口Token",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "content_type",
                                            "label": "消息格式",
                                            "items": content_type_options,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "title_prefix",
                                            "label": "标题前缀（可选）",
                                            "placeholder": "[MoviePilot]",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "model": "msgtypes",
                                            "label": "接收的通知类型",
                                            "hint": "留空表示接收全部通知类型",
                                            "persistent-hint": True,
                                            "items": msg_type_options,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "建议在MagicPush中先创建接口并绑定推送渠道，再将该接口Token填写到这里。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "server": "",
            "token": "",
            "content_type": "markdown",
            "msgtypes": [],
            "append_image": True,
            "title_prefix": "",
            "forward_channel_messages": False,
        }

    def get_page(self) -> List[dict]:
        pass

    def _push_url(self) -> str:
        server = self._server.rstrip("/")
        # 同时兼容用户误填完整推送地址的情况
        if "/api/push/" in server:
            return server
        return f"{server}/api/push/{quote(self._token, safe='')}"

    def _format_content(
        self,
        text: Optional[str],
        title: Optional[str],
        image: Optional[str],
    ) -> str:
        content = (text or "").strip() or (title or "").strip() or "MoviePilot通知"

        if not self._append_image or not image:
            return content

        if self._content_type == "html":
            return f'{content}<br><br><img src="{image}" alt="MoviePilot海报">'
        if self._content_type == "text":
            return f"{content}\n\n海报：{image}"
        return f"{content}\n\n![MoviePilot海报]({image})"

    def _send(
        self,
        title: Optional[str],
        text: Optional[str],
        image: Optional[str],
        link: Optional[str],
    ) -> Tuple[bool, str]:
        if not self._server or not self._token:
            return False, "MagicPush地址或接口Token未配置"

        final_title = (title or "MoviePilot通知").strip()
        if self._title_prefix:
            final_title = f"{self._title_prefix} {final_title}".strip()

        payload = {
            "title": final_title,
            "content": self._format_content(text, final_title, image),
            "type": self._content_type,
        }
        if link:
            payload["url"] = link

        try:
            response = RequestUtils(
                content_type="application/json"
            ).post_res(self._push_url(), json=payload)

            if response is None:
                return False, "未获取到MagicPush响应"

            if not 200 <= response.status_code < 300:
                return False, f"HTTP {response.status_code}：{response.text}"

            try:
                result = response.json()
            except Exception:
                result = None

            if isinstance(result, dict) and result.get("success") is False:
                return False, result.get("message") or str(result)

            success_count = result.get("successCount") if isinstance(result, dict) else None
            failed_count = result.get("failedCount") if isinstance(result, dict) else None
            if success_count is not None:
                logger.info(
                    f"MagicPush消息发送完成：成功 {success_count}，失败 {failed_count or 0}"
                )
            else:
                logger.info("MagicPush消息发送成功")
            return True, "发送成功"

        except Exception as exc:
            logger.error(f"MagicPush消息发送异常：{exc}")
            return False, str(exc)

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        if not self.get_state() or not event or not event.event_data:
            return

        msg_body = event.event_data

        # 带 channel 的消息通常是微信、Telegram 等客户端的定向回复。
        # 默认跳过以避免重复；开启“转发客户端定向消息”后同步到 MagicPush。
        channel = msg_body.get("channel")
        if channel and not self._forward_channel_messages:
            return

        msg_type: NotificationType = msg_body.get("type")
        if (
            msg_type
            and self._msgtypes
            and getattr(msg_type, "name", None) not in self._msgtypes
        ):
            logger.info(
                f"MagicPush未启用通知类型：{getattr(msg_type, 'value', msg_type)}"
            )
            return

        title = msg_body.get("title")
        text = msg_body.get("text")
        if channel and not title:
            title = "MoviePilot命令回复"
        image = msg_body.get("image")
        link = msg_body.get("link")

        if not title and not text:
            logger.warning("MagicPush通知标题和正文不能同时为空")
            return

        ok, message = self._send(title, text, image, link)
        if not ok:
            logger.warning(f"MagicPush消息发送失败：{message}")
        return ok, message

    def stop_service(self):
        pass
