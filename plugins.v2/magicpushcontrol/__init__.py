import hashlib
import html
import json
import queue
import secrets
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from app.command import Command
from app.core.event import Event, eventmanager
from app.helper.thread import ThreadHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Notification
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class MagicPushControl(_PluginBase):
    """通过 MagicPush 入站 Webhook 提供通知转发和独立命令控制。"""

    plugin_name = "MagicPush控制中心"
    plugin_desc = "不依赖系统通知客户端，将MoviePilot通知和命令结果推送到MagicPush，并提供手机控制页。"
    plugin_icon = "magicpushcontrol.png"
    plugin_version = "1.0.0"
    plugin_author = "Neo"
    author_url = "https://github.com/magiccode1412/magicpush"
    plugin_label = "消息通知,命令管理,MagicPush"
    plugin_config_prefix = "magicpushcontrol_"
    plugin_order = 28
    auth_level = 1

    _COMMAND_SOURCE = "MagicPushControl"
    _COMMAND_USER_PREFIX = "magicpush-control"
    _DEFAULT_ALLOWED_COMMANDS = [
        "/version",
        "/sites",
        "/subscribes",
        "/downloading",
        "/cookiecloud",
        "/mediaserver_sync",
        "/transfer",
        "/clear_cache",
    ]
    _DEFAULT_DANGEROUS_COMMANDS = ["/restart", "/clear_cache"]

    def __init__(self):
        """初始化插件运行资源。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._inbound_url = ""
        self._content_type = "markdown"
        self._title_prefix = "[MoviePilot]"
        self._forward_notifications = True
        self._forward_command_results = True
        self._notify_command_start = True
        self._append_image = True
        self._append_link = True
        self._msgtypes: List[str] = []
        self._control_token = ""
        self._allowed_commands: List[str] = []
        self._dangerous_commands: List[str] = []
        self._command_overrides: Dict[str, dict] = {}
        self._console_title = "MoviePilot 控制中心"

        self._message_queue: queue.Queue = queue.Queue(maxsize=200)
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._exec_lock = threading.RLock()
        self._last_exec_by_ip: Dict[str, float] = {}
        self._dedup_lock = threading.RLock()
        self._recent_messages: Dict[str, float] = {}

    def init_plugin(self, config: dict = None) -> None:
        """读取配置、生成控制令牌并启动推送队列。"""
        self.stop_service()

        self._message_queue = queue.Queue(maxsize=200)
        self._stop_event = threading.Event()
        self._recent_messages = {}
        self._last_exec_by_ip = {}

        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        run_test = bool(config.get("onlyonce"))
        self._onlyonce = run_test
        self._inbound_url = str(config.get("inbound_url") or "").strip()
        self._content_type = str(config.get("content_type") or "markdown").strip()
        if self._content_type not in {"text", "markdown", "html"}:
            self._content_type = "markdown"
        self._title_prefix = str(config.get("title_prefix") or "").strip()
        self._forward_notifications = bool(config.get("forward_notifications", True))
        self._forward_command_results = bool(config.get("forward_command_results", True))
        self._notify_command_start = bool(config.get("notify_command_start", True))
        self._append_image = bool(config.get("append_image", True))
        self._append_link = bool(config.get("append_link", True))
        self._msgtypes = list(config.get("msgtypes") or [])
        self._console_title = str(
            config.get("console_title") or "MoviePilot 控制中心"
        ).strip()

        self._control_token = str(config.get("control_token") or "").strip()
        token_generated = False
        if config and not self._control_token:
            self._control_token = secrets.token_urlsafe(24)
            config["control_token"] = self._control_token
            token_generated = True

        configured_commands = config.get("allowed_commands")
        if configured_commands:
            self._allowed_commands = list(configured_commands)
        else:
            self._allowed_commands = list(self._DEFAULT_ALLOWED_COMMANDS)

        self._dangerous_commands = self._parse_command_list(
            config.get("dangerous_commands")
        )
        if not self._dangerous_commands:
            self._dangerous_commands = list(self._DEFAULT_DANGEROUS_COMMANDS)

        self._command_overrides = self._parse_json_object(
            config.get("command_overrides")
        )

        if config and (token_generated or run_test):
            config["onlyonce"] = False
            self._onlyonce = False
            self.update_config(config)

        if self.get_state():
            self._worker = threading.Thread(
                target=self._queue_worker,
                name="magicpush-control-worker",
                daemon=True,
            )
            self._worker.start()

        if run_test:
            success, message = self._send_payload(self._build_payload(
                title="MagicPush 控制中心测试",
                content="MoviePilot 与 MagicPush 入站 Webhook 连接正常。",
                event_name="plugin.test",
            ))
            if success:
                logger.info("MagicPush控制中心测试通知发送成功")
            else:
                logger.warning(f"MagicPush控制中心测试通知发送失败：{message}")

    def get_state(self) -> bool:
        """返回插件是否处于可用状态。"""
        return bool(self._enabled and self._inbound_url)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不向系统通知客户端注册斜杠命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册手机控制页及命令调用接口。"""
        return [
            {
                "path": "/console",
                "endpoint": self.console_page,
                "methods": ["GET"],
                "summary": "MagicPush控制中心",
                "description": "使用插件控制令牌打开手机控制页。",
                "allow_anonymous": True,
            },
            {
                "path": "/commands",
                "endpoint": self.commands_api,
                "methods": ["GET"],
                "summary": "查询可用命令",
                "allow_anonymous": True,
            },
            {
                "path": "/execute",
                "endpoint": self.execute_api,
                "methods": ["POST"],
                "summary": "执行MoviePilot命令",
                "allow_anonymous": True,
            },
            {
                "path": "/test",
                "endpoint": self.test_api,
                "methods": ["POST"],
                "summary": "测试MagicPush入站Webhook",
                "allow_anonymous": True,
            },
            {
                "path": "/status",
                "endpoint": self.status_api,
                "methods": ["GET"],
                "summary": "查询插件状态",
                "allow_anonymous": True,
            },
        ]

    def get_module(self) -> Dict[str, Any]:
        """声明复杂交互消息的摘要转换能力。"""
        return {
            "post_medias_message": self.post_medias_message,
            "post_torrents_message": self.post_torrents_message,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单及默认配置。"""
        msg_type_options = [
            {"title": item.value, "value": item.name}
            for item in NotificationType
        ]
        command_options = self._command_select_options()
        default_overrides = json.dumps(
            {
                "/version": {
                    "title": "查看版本",
                    "category": "系统管理",
                    "show": True,
                },
                "/restart": {
                    "title": "重启MoviePilot",
                    "category": "系统管理",
                    "show": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enabled",
                                        "label": "启用插件",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "onlyonce",
                                        "label": "发送测试通知",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "forward_notifications",
                                        "label": "转发系统通知",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "forward_command_results",
                                        "label": "转发命令结果",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "inbound_url",
                                        "label": "MagicPush入站Webhook完整地址",
                                        "placeholder": "http://192.168.1.10:3000/api/inbound/接口Token",
                                        "hint": "必须在MagicPush接口管理中开启入站接收，并填写完整/api/inbound/Token地址",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "content_type",
                                        "label": "消息格式",
                                        "items": [
                                            {"title": "Markdown（推荐）", "value": "markdown"},
                                            {"title": "纯文本", "value": "text"},
                                            {"title": "HTML", "value": "html"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "title_prefix",
                                        "label": "标题前缀",
                                        "placeholder": "[MoviePilot]",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "console_title",
                                        "label": "控制页标题",
                                        "placeholder": "MoviePilot 控制中心",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "append_image",
                                        "label": "正文附加海报",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "append_link",
                                        "label": "正文附加跳转链接",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "notify_command_start",
                                        "label": "推送命令已提交",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "msgtypes",
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "label": "转发的通知类型",
                                        "hint": "留空表示接收全部MoviePilot通知类型",
                                        "persistent-hint": True,
                                        "items": msg_type_options,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "控制页不依赖微信、Telegram等系统通知客户端。控制令牌留空保存时会自动生成。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "control_token",
                                        "label": "控制页/API安全令牌",
                                        "type": "password",
                                        "hint": "建议至少24位；不要对外公开",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "dangerous_commands",
                                        "label": "需要二次确认的命令",
                                        "placeholder": "/restart,/clear_cache",
                                        "hint": "多个命令使用英文逗号分隔",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "allowed_commands",
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "label": "控制页允许执行的命令",
                                        "hint": "留空时使用插件内置的安全默认命令；危险命令仍需二次确认",
                                        "persistent-hint": True,
                                        "items": command_options,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "command_overrides",
                                        "label": "命令显示设置（JSON）",
                                        "rows": 9,
                                        "hint": "可修改title、category、show，不会改变命令实际功能",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "建议仅在局域网或HTTPS反向代理下使用控制页，不要把带控制令牌的地址公开到互联网。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "inbound_url": "",
            "content_type": "markdown",
            "title_prefix": "[MoviePilot]",
            "console_title": "MoviePilot 控制中心",
            "forward_notifications": True,
            "forward_command_results": True,
            "notify_command_start": True,
            "append_image": True,
            "append_link": True,
            "msgtypes": [],
            "control_token": "",
            "allowed_commands": list(self._DEFAULT_ALLOWED_COMMANDS),
            "dangerous_commands": ",".join(self._DEFAULT_DANGEROUS_COMMANDS),
            "command_overrides": default_overrides,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页和控制页入口。"""
        if not self._control_token:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "text": "请先保存插件配置以生成控制令牌。",
                },
            }]

        console_path = (
            f"/api/v1/plugin/{self.__class__.__name__}/console"
            f"?token={quote(self._control_token, safe='')}"
        )
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if self.get_state() else "warning",
                    "variant": "tonal",
                    "text": (
                        "MagicPush控制中心已启用。"
                        if self.get_state()
                        else "插件尚未启用或未填写MagicPush入站Webhook。"
                    ),
                },
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "手机控制页",
                    },
                    {
                        "component": "VCardText",
                        "text": console_path,
                    },
                    {
                        "component": "VCardActions",
                        "content": [{
                            "component": "VBtn",
                            "props": {
                                "href": console_path,
                                "target": "_blank",
                                "variant": "tonal",
                                "prepend-icon": "mdi-open-in-new",
                            },
                            "text": "打开控制页",
                        }],
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "class": "mt-3",
                    "text": "MagicPush入站字段建议配置：标题 $.title，正文 $.content，类型 $.type。",
                },
            },
        ]

    def stop_service(self) -> None:
        """停止推送队列线程。"""
        try:
            self._stop_event.set()
            if self._worker and self._worker.is_alive():
                try:
                    self._message_queue.put_nowait(None)
                except queue.Full:
                    pass
                self._worker.join(timeout=2)
        finally:
            self._worker = None

    @eventmanager.register(EventType.NoticeMessage)
    def handle_notice(self, event: Event) -> None:
        """接收MoviePilot通知事件并转发到MagicPush。"""
        if not self.get_state() or not event or not event.event_data:
            return

        data = event.event_data
        source = str(data.get("source") or "")
        userid = str(data.get("userid") or "")
        is_command_result = (
            source == self._COMMAND_SOURCE
            or userid.startswith(self._COMMAND_USER_PREFIX)
        )

        if is_command_result:
            if not self._forward_command_results:
                return
        elif not self._forward_notifications:
            return

        msg_type = data.get("type")
        if not is_command_result and not self._notification_type_enabled(msg_type):
            return

        title = str(data.get("title") or "").strip()
        content = str(data.get("text") or "").strip()
        image = str(data.get("image") or "").strip()
        link = str(data.get("link") or "").strip()

        if not title and not content:
            return

        if self._is_duplicate(title, content, source, userid):
            logger.debug("MagicPush控制中心跳过短时间重复消息")
            return

        payload = self._build_payload(
            title=title or ("MoviePilot命令结果" if is_command_result else "MoviePilot通知"),
            content=content or title,
            image=image,
            link=link,
            event_name="command.result" if is_command_result else "notice.message",
            notification_type=self._notification_type_name(msg_type),
        )
        self._enqueue_payload(payload)

    def post_medias_message(
        self,
        message: Notification,
        medias: List[Any],
    ) -> None:
        """把媒体选择列表转换为MagicPush文字摘要。"""
        if not self.get_state() or not self._is_control_message(message):
            return None

        lines = []
        for index, media in enumerate((medias or [])[:20], start=1):
            name = (
                getattr(media, "title_year", None)
                or getattr(media, "title", None)
                or getattr(media, "name", None)
                or getattr(media, "original_title", None)
                or f"媒体 {index}"
            )
            media_type = getattr(getattr(media, "type", None), "value", None)
            suffix = f"（{media_type}）" if media_type else ""
            lines.append(f"{index}. {name}{suffix}")

        if len(medias or []) > 20:
            lines.append(f"……另有 {len(medias) - 20} 项未显示")

        self._enqueue_payload(self._build_payload(
            title=message.title or "媒体选择结果",
            content="\n".join(lines) or "没有可显示的媒体结果。",
            link=message.link,
            event_name="command.media_list",
        ))
        return None

    def post_torrents_message(
        self,
        message: Notification,
        torrents: List[Any],
    ) -> None:
        """把种子选择列表转换为MagicPush文字摘要。"""
        if not self.get_state() or not self._is_control_message(message):
            return None

        lines = []
        for index, context in enumerate((torrents or [])[:20], start=1):
            torrent = getattr(context, "torrent_info", None) or context
            name = (
                getattr(torrent, "title", None)
                or getattr(torrent, "name", None)
                or f"资源 {index}"
            )
            site = getattr(torrent, "site_name", None) or getattr(torrent, "site", None)
            seeders = getattr(torrent, "seeders", None)
            details = []
            if site:
                details.append(str(site))
            if seeders is not None:
                details.append(f"做种 {seeders}")
            suffix = f"｜{'，'.join(details)}" if details else ""
            lines.append(f"{index}. {name}{suffix}")

        if len(torrents or []) > 20:
            lines.append(f"……另有 {len(torrents) - 20} 项未显示")

        self._enqueue_payload(self._build_payload(
            title=message.title or "资源选择结果",
            content="\n".join(lines) or "没有可显示的资源结果。",
            link=message.link,
            event_name="command.torrent_list",
        ))
        return None

    def console_page(self, request: Request, token: str = "") -> HTMLResponse:
        """返回适合手机浏览器使用的命令控制页面。"""
        self._require_token(token or request.headers.get("X-MagicPush-Control-Token"))
        if not self.get_state():
            raise HTTPException(status_code=503, detail="插件未启用或未配置入站Webhook")

        commands = self._available_commands()
        grouped: Dict[str, List[dict]] = {}
        for item in commands:
            grouped.setdefault(item["category"], []).append(item)

        group_html = []
        for category, items in grouped.items():
            buttons = []
            for item in items:
                command = html.escape(item["command"], quote=True)
                title = html.escape(item["title"])
                description = html.escape(item["description"])
                dangerous = "true" if item["dangerous"] else "false"
                buttons.append(
                    f'<button class="cmd" data-command="{command}" '
                    f'data-dangerous="{dangerous}">'
                    f'<strong>{title}</strong><span>{description}</span></button>'
                )
            group_html.append(
                f'<section><h2>{html.escape(category)}</h2>'
                f'<div class="grid">{"".join(buttons)}</div></section>'
            )

        token_json = json.dumps(token, ensure_ascii=False)
        title = html.escape(self._console_title or "MoviePilot 控制中心")
        empty_html = (
            '<div class="empty">当前没有可用命令，请在插件配置中选择允许的命令。</div>'
            if not group_html else ""
        )

        page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:#11131a; color:#f5f7ff; }}
main {{ width:min(920px,100%); margin:auto; padding:24px 16px 56px; }}
header {{ padding:14px 4px 22px; }}
h1 {{ margin:0 0 8px; font-size:28px; }}
header p {{ margin:0; color:#aeb5ca; }}
section {{ margin:22px 0; }}
h2 {{ font-size:16px; color:#bfc7dc; margin:0 0 10px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
.cmd {{ border:1px solid #343b50; background:#1c2130; color:#fff; border-radius:14px;
padding:15px; min-height:82px; text-align:left; cursor:pointer; }}
.cmd:active {{ transform:scale(.98); }}
.cmd strong {{ display:block; font-size:16px; margin-bottom:7px; }}
.cmd span {{ display:block; color:#9da7bf; font-size:12px; line-height:1.4; }}
.cmd[data-dangerous="true"] {{ border-color:#74414a; background:#2b1d24; }}
.manual {{ margin-top:28px; padding:16px; border:1px solid #343b50; border-radius:16px; background:#181c28; }}
.row {{ display:flex; gap:9px; flex-wrap:wrap; }}
input {{ flex:1; min-width:180px; background:#0f1219; border:1px solid #3a4258; color:#fff;
border-radius:10px; padding:12px; font-size:15px; }}
#run {{ border:0; border-radius:10px; padding:12px 18px; background:#6875f5; color:white; font-weight:700; }}
#status {{ min-height:24px; margin-top:13px; color:#b9c2d7; }}
.empty {{ padding:20px; border:1px dashed #424b63; border-radius:14px; color:#aeb5ca; }}
footer {{ margin-top:30px; color:#747e96; font-size:12px; }}
</style>
</head>
<body>
<main>
<header><h1>{title}</h1><p>执行结果将通过 MagicPush 推送</p></header>
{"".join(group_html)}
{empty_html}
<div class="manual">
  <h2>带参数执行</h2>
  <div class="row">
    <input id="command" placeholder="/命令，例如 /version">
    <input id="args" placeholder="参数，可留空">
    <button id="run">执行</button>
  </div>
  <div id="status"></div>
</div>
<footer>请仅在可信网络中使用此页面。危险命令会要求二次确认。</footer>
</main>
<script>
const TOKEN = {token_json};
const statusEl = document.getElementById("status");

async function executeCommand(command, args = "", dangerous = false) {{
  if (!command) return;
  let confirmed = false;
  if (dangerous) {{
    confirmed = confirm(`确认执行高风险命令 ${{command}}？`);
    if (!confirmed) return;
  }}
  statusEl.textContent = `正在提交 ${{command}} ...`;
  try {{
    const response = await fetch("./execute", {{
      method: "POST",
      headers: {{
        "Content-Type": "application/json",
        "X-MagicPush-Control-Token": TOKEN
      }},
      body: JSON.stringify({{ command, args, confirm: confirmed }})
    }});
    const data = await response.json();
    if (!response.ok || !data.success) {{
      throw new Error(data.detail || data.message || "执行失败");
    }}
    statusEl.textContent = data.message || "命令已提交，请查看MagicPush通知。";
  }} catch (error) {{
    statusEl.textContent = `错误：${{error.message}}`;
  }}
}}

document.querySelectorAll(".cmd").forEach(button => {{
  button.addEventListener("click", () => executeCommand(
    button.dataset.command,
    "",
    button.dataset.dangerous === "true"
  ));
}});

document.getElementById("run").addEventListener("click", () => {{
  const command = document.getElementById("command").value.trim();
  const args = document.getElementById("args").value.trim();
  const item = {json.dumps({item["command"]: item["dangerous"] for item in commands}, ensure_ascii=False)};
  executeCommand(command, args, Boolean(item[command]));
}});
</script>
</body>
</html>"""

        return HTMLResponse(
            content=page,
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; "
                    "img-src data:; base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )

    def commands_api(
        self,
        request: Request,
        token: str = "",
    ) -> dict:
        """返回控制页允许执行的命令清单。"""
        self._require_token(
            token or request.headers.get("X-MagicPush-Control-Token")
        )
        return {
            "success": True,
            "data": self._available_commands(),
        }

    async def execute_api(self, request: Request) -> dict:
        """校验请求并异步执行MoviePilot命令。"""
        try:
            body = await request.json()
        except Exception:
            body = {}

        supplied_token = (
            request.headers.get("X-MagicPush-Control-Token")
            or request.query_params.get("token")
            or body.get("token")
        )
        self._require_token(supplied_token)

        if not self.get_state():
            raise HTTPException(status_code=503, detail="插件未启用或未配置入站Webhook")

        raw_command = str(body.get("command") or "").strip()
        command = raw_command.split()[0] if raw_command else ""
        args = str(body.get("args") or "").strip()
        confirmed = bool(body.get("confirm"))

        if not command.startswith("/"):
            raise HTTPException(status_code=400, detail="命令必须以 / 开头")
        if len(args) > 500:
            raise HTTPException(status_code=400, detail="命令参数过长")

        allowed = {item["command"] for item in self._available_commands()}
        if command not in allowed:
            raise HTTPException(status_code=403, detail="该命令未在插件配置中启用")

        commands = Command().get_commands()
        if command not in commands:
            raise HTTPException(status_code=404, detail="MoviePilot中不存在该命令")

        if command in self._dangerous_commands and not confirmed:
            raise HTTPException(status_code=409, detail="该命令需要二次确认")

        client_ip = request.client.host if request.client else "unknown"
        self._check_rate_limit(client_ip)

        request_id = secrets.token_hex(6)
        ThreadHelper().submit(
            self._run_command,
            command,
            args,
            request_id,
        )

        return {
            "success": True,
            "message": f"命令 {command} 已提交，请查看MagicPush通知。",
            "request_id": request_id,
        }

    async def test_api(self, request: Request) -> dict:
        """通过API发送一条MagicPush入站测试消息。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        supplied_token = (
            request.headers.get("X-MagicPush-Control-Token")
            or request.query_params.get("token")
            or body.get("token")
        )
        self._require_token(supplied_token)

        success, message = self._send_payload(self._build_payload(
            title="MagicPush 控制中心测试",
            content="MoviePilot 与 MagicPush 入站 Webhook 连接正常。",
            event_name="plugin.test",
        ))
        return {
            "success": success,
            "message": message,
        }

    def status_api(
        self,
        request: Request,
        token: str = "",
    ) -> dict:
        """返回插件公开控制状态。"""
        self._require_token(
            token or request.headers.get("X-MagicPush-Control-Token")
        )
        return {
            "success": True,
            "data": {
                "enabled": self.get_state(),
                "inbound_configured": bool(self._inbound_url),
                "command_count": len(self._available_commands()),
                "version": self.plugin_version,
            },
        }

    def _run_command(self, command: str, args: str, request_id: str) -> None:
        """在后台线程执行命令并通过MagicPush发送提交状态。"""
        if self._notify_command_start:
            content = f"命令：`{command}`"
            if args:
                content += f"\n\n参数：`{args}`"
            content += f"\n\n请求编号：`{request_id}`"
            self._enqueue_payload(self._build_payload(
                title="MoviePilot命令已提交",
                content=content,
                event_name="command.submitted",
            ))

        try:
            Command().execute(
                cmd=command,
                data_str=args,
                channel=None,
                source=self._COMMAND_SOURCE,
                userid=f"{self._COMMAND_USER_PREFIX}:{request_id}",
            )
        except Exception as exc:
            logger.error(f"MagicPush控制中心执行命令失败：{exc}")
            self._enqueue_payload(self._build_payload(
                title="MoviePilot命令执行异常",
                content=f"命令：`{command}`\n\n错误：{exc}",
                event_name="command.error",
            ))

    def _available_commands(self) -> List[dict]:
        """根据允许清单和显示设置构建控制页命令。"""
        try:
            all_commands = Command().get_commands() or {}
        except Exception as exc:
            logger.warning(f"MagicPush控制中心读取命令失败：{exc}")
            return []

        allowed = set(self._allowed_commands)
        result = []
        for command, info in all_commands.items():
            if command not in allowed:
                continue
            if not info.get("show", True):
                continue

            override = self._command_overrides.get(command) or {}
            if override.get("show") is False:
                continue

            title = str(
                override.get("title")
                or info.get("description")
                or command
            )
            description = str(info.get("description") or title)
            category = str(
                override.get("category")
                or info.get("category")
                or "其它"
            )

            result.append({
                "command": command,
                "title": title,
                "description": description,
                "category": category,
                "dangerous": command in self._dangerous_commands,
            })

        result.sort(key=lambda item: (
            item["category"],
            item["title"],
            item["command"],
        ))
        return result

    def _command_select_options(self) -> List[dict]:
        """生成配置页面中的命令选择项。"""
        try:
            commands = Command().get_commands() or {}
        except Exception:
            commands = {}
        options = []
        for command, info in commands.items():
            if not info.get("show", True):
                continue
            description = info.get("description") or command
            category = info.get("category") or "其它"
            options.append({
                "title": f"{command}｜{description}（{category}）",
                "value": command,
            })
        options.sort(key=lambda item: item["title"])
        return options

    def _build_payload(
        self,
        title: str,
        content: str,
        image: str = "",
        link: str = "",
        event_name: str = "notice.message",
        notification_type: str = "",
    ) -> dict:
        """构建MagicPush入站Webhook标准JSON。"""
        final_title = str(title or "MoviePilot通知").strip()
        if self._title_prefix:
            final_title = f"{self._title_prefix} {final_title}".strip()

        formatted_content = str(content or "").strip() or final_title
        if image and self._append_image:
            if self._content_type == "html":
                formatted_content += (
                    f'<br><br><img src="{html.escape(image, quote=True)}" '
                    f'alt="MoviePilot海报">'
                )
            elif self._content_type == "markdown":
                formatted_content += f"\n\n![MoviePilot海报]({image})"
            else:
                formatted_content += f"\n\n海报：{image}"

        if link and self._append_link:
            if self._content_type == "html":
                formatted_content += (
                    f'<br><br><a href="{html.escape(link, quote=True)}">'
                    f"打开详情</a>"
                )
            elif self._content_type == "markdown":
                formatted_content += f"\n\n[打开详情]({link})"
            else:
                formatted_content += f"\n\n详情：{link}"

        return {
            "title": final_title,
            "content": formatted_content,
            "type": self._content_type,
            "source": "MoviePilot",
            "event": event_name,
            "notification_type": notification_type,
            "image": image or None,
            "url": link or None,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    def _enqueue_payload(self, payload: dict) -> None:
        """将消息加入异步推送队列。"""
        if not self.get_state():
            return
        try:
            self._message_queue.put_nowait(payload)
        except queue.Full:
            try:
                self._message_queue.get_nowait()
                self._message_queue.task_done()
                self._message_queue.put_nowait(payload)
                logger.warning("MagicPush推送队列已满，已丢弃最早的一条消息")
            except Exception:
                logger.warning("MagicPush推送队列已满，本条消息已丢弃")

    def _queue_worker(self) -> None:
        """后台消费MagicPush推送队列。"""
        while not self._stop_event.is_set():
            try:
                payload = self._message_queue.get(timeout=1)
            except queue.Empty:
                continue

            if payload is None:
                self._message_queue.task_done()
                break

            try:
                self._send_payload(payload)
            finally:
                self._message_queue.task_done()

    def _send_payload(self, payload: dict) -> Tuple[bool, str]:
        """向MagicPush入站Webhook发送JSON。"""
        if not self._inbound_url:
            return False, "未配置MagicPush入站Webhook地址"

        try:
            response = RequestUtils(
                content_type="application/json"
            ).post_res(self._inbound_url, json=payload)

            if response is None:
                return False, "未获取到MagicPush响应"

            if not 200 <= response.status_code < 300:
                detail = response.text or response.reason
                logger.warning(
                    f"MagicPush入站推送失败：HTTP {response.status_code} {detail}"
                )
                return False, f"HTTP {response.status_code}：{detail}"

            try:
                result = response.json()
            except Exception:
                result = {}

            if isinstance(result, dict) and result.get("success") is False:
                message = result.get("message") or str(result)
                logger.warning(f"MagicPush入站推送失败：{message}")
                return False, message

            logger.info(f"MagicPush入站推送成功：{payload.get('title')}")
            return True, "推送成功"
        except Exception as exc:
            logger.error(f"MagicPush入站推送异常：{exc}")
            return False, str(exc)

    def _require_token(self, supplied_token: Optional[str]) -> None:
        """校验控制页和API安全令牌。"""
        supplied = str(supplied_token or "")
        expected = str(self._control_token or "")
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="控制令牌无效")

    def _check_rate_limit(self, client_ip: str) -> None:
        """限制同一来源短时间重复提交命令。"""
        now = time.monotonic()
        with self._exec_lock:
            previous = self._last_exec_by_ip.get(client_ip, 0)
            if now - previous < 1.5:
                raise HTTPException(status_code=429, detail="操作过于频繁，请稍后重试")
            self._last_exec_by_ip[client_ip] = now

    def _notification_type_enabled(self, msg_type: Any) -> bool:
        """判断通知类型是否在用户选择范围内。"""
        if not self._msgtypes or not msg_type:
            return True
        name = getattr(msg_type, "name", None)
        if name:
            return name in self._msgtypes
        return str(msg_type) in self._msgtypes

    @staticmethod
    def _notification_type_name(msg_type: Any) -> str:
        """返回适合写入Webhook的通知类型名称。"""
        if not msg_type:
            return ""
        return str(
            getattr(msg_type, "value", None)
            or getattr(msg_type, "name", None)
            or msg_type
        )

    def _is_control_message(self, message: Notification) -> bool:
        """判断复杂消息是否由本插件控制页触发。"""
        source = str(getattr(message, "source", None) or "")
        userid = str(getattr(message, "userid", None) or "")
        return (
            source == self._COMMAND_SOURCE
            or userid.startswith(self._COMMAND_USER_PREFIX)
        )

    def _is_duplicate(
        self,
        title: str,
        content: str,
        source: str,
        userid: str,
    ) -> bool:
        """在三秒窗口内抑制完全相同的重复通知。"""
        digest = hashlib.sha256(
            f"{title}\0{content}\0{source}\0{userid}".encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        with self._dedup_lock:
            expired = [
                key for key, created in self._recent_messages.items()
                if now - created > 3
            ]
            for key in expired:
                self._recent_messages.pop(key, None)
            if digest in self._recent_messages:
                return True
            self._recent_messages[digest] = now
        return False

    @staticmethod
    def _parse_command_list(value: Any) -> List[str]:
        """把列表或逗号分隔字符串转换为命令列表。"""
        if isinstance(value, list):
            values = value
        else:
            values = str(value or "").replace("，", ",").split(",")
        result = []
        for item in values:
            command = str(item or "").strip()
            if command and command not in result:
                result.append(command)
        return result

    @staticmethod
    def _parse_json_object(value: Any) -> Dict[str, dict]:
        """安全解析命令显示设置JSON。"""
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(str(value or "{}"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            logger.warning(f"MagicPush控制中心命令显示设置JSON无效：{exc}")
            return {}
