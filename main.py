import aiohttp
import asyncio
import time
import re
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from lxml import etree

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult,MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

from .data_handler import DataHandler
from .pic_handler import RssImageHandler
from .rss import RSSItem
from typing import List, Optional


@register(
    "astrbot_plugin_rss",
    "Soulter",
    "RSS订阅插件",
    "1.1.3",
    "https://github.com/Soulter/astrbot_plugin_rss",
)
class RssPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        self.logger = logging.getLogger("astrbot")
        self.context = context
        self.config = config
        self.data_handler = DataHandler()
        self.pic_handler = RssImageHandler()

        # 提取scheme文件中的配置
        self.title_max_length = config.get("title_max_length")
        self.description_max_length = config.get("description_max_length")
        self.max_items_per_poll = config.get("max_items_per_poll")
        self.t2i = config.get("t2i")
        self.is_hide_url = config.get("is_hide_url")
        self.is_read_pic= config.get("pic_config").get("is_read_pic")
        self.is_adjust_pic= config.get("pic_config").get("is_adjust_pic")
        self.max_pic_item = config.get("pic_config").get("max_pic_item")
        self.is_compose = config.get("compose")
        self.rsshub_base_url = self._normalize_rsshub_base_url(config.get("rsshub_base_url") or "")
        self.rsshub_query_param = (config.get("rsshub_query_param") or "").strip()
        self.message_timezone = (config.get("message_timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
        self._target_tz = self._load_target_timezone(self.message_timezone)
        self.translation_timezone = self.message_timezone if isinstance(self._target_tz, ZoneInfo) else "UTC"
        self.translate_enabled = self._cfg_bool("translate_enabled", False)
        self.translate_target_language = self._cfg_str("translate_target_language", "zh-Hans")
        self.translate_provider_id = self._cfg_str("translate_provider_id", "")

        self.pic_handler = RssImageHandler(self.is_adjust_pic)
        self._future_task_plugin_id = "astrbot_plugin_rss_mod"
        self._cron_refresh_lock = asyncio.Lock()
        self._cron_sync_task: asyncio.Task | None = None

    async def initialize(self):
        """插件加载后在 cron manager 就绪后同步 FutureTask。"""
        self._cron_sync_task = asyncio.create_task(self._wait_and_refresh_future_tasks())

    async def terminate(self):
        """插件卸载时删除插件创建的 FutureTask，避免热重载后重复触发。"""
        if self._cron_sync_task and not self._cron_sync_task.done():
            self._cron_sync_task.cancel()
            try:
                await self._cron_sync_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning(f"rss: terminate cron sync task failed: {exc}")
        await self._cleanup_future_tasks()

    async def _wait_and_refresh_future_tasks(self):
        cron_mgr = getattr(self.context, "cron_manager", None)
        if cron_mgr is None:
            self.logger.warning("rss: cron_manager is unavailable, skip initial FutureTask sync")
            return

        for _ in range(100):
            if getattr(cron_mgr, "_started", False) and hasattr(cron_mgr, "ctx"):
                await self._refresh_future_tasks()
                return
            await asyncio.sleep(0.1)

        self.logger.warning("rss: cron_manager not ready in time, skip initial FutureTask sync")

    async def parse_channel_info(self, url):
        headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        try:
            async with aiohttp.ClientSession(trust_env=True,
                                        connector=connector,
                                        timeout=timeout,
                                        headers=headers
                                        ) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        self.logger.error(f"rss: 无法正常打开站点 {url}")
                        return None
                    text = await resp.read()
                    return text
        except asyncio.TimeoutError:
            self.logger.error(f"rss: 请求站点 {url} 超时")
            return None
        except aiohttp.ClientError as e:
            self.logger.error(f"rss: 请求站点 {url} 网络错误: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"rss: 请求站点 {url} 发生未知错误: {str(e)}")
            return None

    async def cron_task_callback(self, url: str, user: str, **_kwargs):
        """定时任务回调"""

        if url not in self.data_handler.data:
            return
        if user not in self.data_handler.data[url]["subscribers"]:
            return

        self.logger.info(f"RSS 定时任务触发: {url} - {user}")
        last_update = self.data_handler.data[url]["subscribers"][user]["last_update"]
        latest_link = self.data_handler.data[url]["subscribers"][user]["latest_link"]
        max_items_per_poll = self.max_items_per_poll
        # 拉取 RSS
        rss_items = await self.poll_rss(
            url,
            num=max_items_per_poll,
            after_timestamp=last_update,
            after_link=latest_link,
        )
        max_ts = last_update

        # 分解MessageSesion
        platform_name,message_type,session_id = user.split(":")

        # 分平台处理消息
        if platform_name == "aiocqhttp" and self.is_compose:
            nodes = []
            for item in rss_items:
                comps = await self._get_chain_components(item)
                node = Comp.Node(
                    uin=0,
                    name="Astrbot",
                    content=comps
                )
                nodes.append(node)
                self.data_handler.data[url]["subscribers"][user]["last_update"] = int(
                    time.time()
                )
                max_ts = max(max_ts, item.pubDate_timestamp)

            # 合并消息发送
            if len(nodes) > 0:
                msc = MessageChain(
                    chain=nodes,
                    use_t2i_=self.t2i
                )
                await self.context.send_message(user, msc)
                for item in rss_items:
                    await self._send_translation_followup(user, item)
        else:
            # 每个消息单独发送
            for item in rss_items:
                comps = await self._get_chain_components(item)
                msc = MessageChain(
                    chain=comps,
                    use_t2i_=self.t2i
                )
                await self.context.send_message(user, msc)
                await self._send_translation_followup(user, item)
                self.data_handler.data[url]["subscribers"][user]["last_update"] = int(
                    time.time()
                )
                max_ts = max(max_ts, item.pubDate_timestamp)

        # 更新最后更新时间
        if rss_items:
            self.data_handler.data[url]["subscribers"][user]["last_update"] = max_ts
            self.data_handler.data[url]["subscribers"][user]["latest_link"] = rss_items[
                0
            ].link
            self.data_handler.save_data()
            self.logger.info(f"RSS 定时任务 {url} 推送成功 - {user}")
        else:
            self.logger.info(f"RSS 定时任务 {url} 无消息更新 - {user}")


    async def poll_rss(
        self,
        url: str,
        num: int = -1,
        after_timestamp: int = 0,
        after_link: str = "",
    ) -> List[RSSItem]:
        """从站点拉取RSS信息"""
        text = await self.parse_channel_info(url)
        if text is None:
            self.logger.error(f"rss: 无法解析站点 {url} 的RSS信息")
            return []
        root = etree.fromstring(text)
        items = root.xpath("//item")

        cnt = 0
        rss_items = []

        for item in items:
            try:
                chan_title = (
                    self.data_handler.data[url]["info"]["title"]
                    if url in self.data_handler.data
                    else "未知频道"
                )

                title_nodes = item.xpath("title")
                title = (title_nodes[0].text or "").strip() if title_nodes else ""
                if not title:
                    title = "(无标题)"
                if len(title) > self.title_max_length:
                    title = title[: self.title_max_length] + "..."

                link_nodes = item.xpath("link")
                link = (link_nodes[0].text or "").strip() if link_nodes else ""
                if not link:
                    self.logger.warning(f"rss: 条目缺少 link，已跳过: {url}")
                    continue
                if not re.match(r"^https?://", link):
                    link = self.data_handler.get_root_url(url) + link

                description_nodes = item.xpath("description")
                description_html = (
                    description_nodes[0].text if description_nodes else ""
                ) or ""

                pic_url_list = self.data_handler.strip_html_pic(description_html)
                description = self.data_handler.strip_html(description_html).strip()
                if not description:
                    description = "(无描述)"

                if len(description) > self.description_max_length:
                    description = (
                        description[: self.description_max_length] + "..."
                    )

                pub_date_nodes = item.xpath("pubDate")
                pub_date = (pub_date_nodes[0].text or "").strip() if pub_date_nodes else ""
                use_link_fallback = False
                pub_date_timestamp = 0

                if pub_date:
                    try:
                        pub_date_dt = datetime.strptime(
                            pub_date.replace("GMT", "+0000"),
                            "%a, %d %b %Y %H:%M:%S %z",
                        )
                        pub_date_timestamp = int(pub_date_dt.timestamp())
                    except Exception:
                        self.logger.warning(
                            f"rss: 条目 pubDate 解析失败，改用 link 去重: {url}"
                        )
                        use_link_fallback = True
                else:
                    use_link_fallback = True

                if not use_link_fallback:
                    if pub_date_timestamp > after_timestamp:
                        rss_items.append(
                            RSSItem(
                                chan_title,
                                title,
                                link,
                                description,
                                pub_date,
                                pub_date_timestamp,
                                pic_url_list,
                            )
                        )
                        cnt += 1
                        if num != -1 and cnt >= num:
                            break
                    else:
                        break
                else:
                    if link != after_link:
                        rss_items.append(
                            RSSItem(chan_title, title, link, description, "", 0, pic_url_list)
                        )
                        cnt += 1
                        if num != -1 and cnt >= num:
                            break
                    else:
                        break

            except Exception as e:
                self.logger.error(f"rss: 解析Rss条目 {url} 失败: {str(e)}")
                continue

        return rss_items

    def parse_rss_url(self, url: str) -> str:
        """解析RSS URL，确保以http或https开头"""
        if not re.match(r"^https?://", url):
            if not url.startswith("/"):
                url = "/" + url
            url = "https://" + url
        return url

    def _normalize_rsshub_base_url(self, base_url: str) -> str:
        """Normalize RSSHub base URL and trim trailing slash."""
        base_url = (base_url or "").strip()
        if not base_url:
            return ""
        return base_url.rstrip("/")

    def _get_rsshub_endpoint(self, idx: int) -> str:
        """Get RSSHub endpoint: indexed endpoint first, config fallback."""
        if 0 <= idx < len(self.data_handler.data["rsshub_endpoints"]):
            return self.data_handler.data["rsshub_endpoints"][idx]
        return self.rsshub_base_url

    def _append_rsshub_query_param(self, url: str) -> str:
        """为 RSSHub 路由拼接附加查询参数（如 ACCESS_KEY）。"""
        query = self.rsshub_query_param
        if not query:
            return url
        query = query.lstrip("?&")
        if not query:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{query}"

    def _load_target_timezone(self, timezone_name: str):
        """加载目标时区，非法时区自动回退到 UTC。"""
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.logger.warning(
                f"rss: 无效时区配置 {timezone_name}，已自动回退到 UTC"
            )
            return timezone.utc

    def _format_item_time(self, item: RSSItem) -> str:
        """将 RSS 条目时间转换为配置时区后格式化。"""
        if item.pubDate:
            try:
                dt = datetime.strptime(
                    item.pubDate.replace("GMT", "+0000"),
                    "%a, %d %b %Y %H:%M:%S %z",
                )
                local_dt = dt.astimezone(self._target_tz)
                return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                pass

        if item.pubDate_timestamp > 0:
            dt = datetime.fromtimestamp(item.pubDate_timestamp, tz=timezone.utc)
            local_dt = dt.astimezone(self._target_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")

        return "未知"

    async def _cleanup_future_tasks(self):
        cron_mgr = getattr(self.context, "cron_manager", None)
        if cron_mgr is None:
            return

        try:
            jobs = await cron_mgr.list_jobs("basic")
        except Exception as exc:
            self.logger.warning(f"rss: failed to list FutureTask jobs: {exc}")
            return

        for job in jobs:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if payload.get("plugin") != self._future_task_plugin_id:
                continue
            try:
                await cron_mgr.delete_job(job.job_id)
            except Exception as exc:
                self.logger.warning(f"rss: failed to delete FutureTask job {job.job_id}: {exc}")

    async def _refresh_future_tasks(self):
        async with self._cron_refresh_lock:
            cron_mgr = getattr(self.context, "cron_manager", None)
            if cron_mgr is None:
                self.logger.warning("rss: cron_manager is unavailable, skip FutureTask sync")
                return

            self.logger.info("rss: refreshing FutureTask jobs")
            await self._cleanup_future_tasks()

            data_changed = False
            for url, info in self.data_handler.data.items():
                if url == "rsshub_endpoints" or url == "settings":
                    continue

                chan_title = ((info.get("info") or {}).get("title") or url).strip()
                job_name = f"RSS feed push: {chan_title}"[:120]
                subscribers = info.get("subscribers") or {}

                for user, sub_info in subscribers.items():
                    if not isinstance(sub_info, dict):
                        continue

                    if "future_task_id" in sub_info:
                        sub_info.pop("future_task_id", None)
                        data_changed = True

                    cron_expr = str(sub_info.get("cron_expr") or "").strip()
                    if not cron_expr:
                        self.logger.warning(f"rss: missing cron_expr, skipped: {url} - {user}")
                        continue

                    session_id = user.split(":", 2)[2] if ":" in user else user
                    payload = {
                        "plugin": self._future_task_plugin_id,
                        "url": url,
                        "user": user,
                        "session": user,
                        "session_id": session_id,
                    }

                    try:
                        job = await cron_mgr.add_basic_job(
                            name=job_name,
                            cron_expression=cron_expr,
                            handler=self.cron_task_callback,
                            description=f"RSS push: {url} -> {user}",
                            payload=payload,
                            enabled=True,
                            persistent=False,
                        )
                        sub_info["future_task_id"] = job.job_id
                        data_changed = True
                    except Exception as exc:
                        self.logger.warning(f"rss: failed to create FutureTask job {url} - {user}: {exc}")

            if data_changed:
                self.data_handler.save_data()

    async def _add_url(self, url: str, cron_expr: str, message: AstrMessageEvent):
        """内部方法：添加URL订阅的共用逻辑"""
        user = message.unified_msg_origin
        latest_item = await self.poll_rss(url)
        now_ts = int(time.time())

        last_update = now_ts
        latest_link = ""
        if latest_item:
            first_item = latest_item[0]
            last_update = first_item.pubDate_timestamp or now_ts
            latest_link = first_item.link or ""

        if url in self.data_handler.data:
            self.data_handler.data[url]["subscribers"][user] = {
                "cron_expr": cron_expr,
                "last_update": last_update,
                "latest_link": latest_link,
            }
        else:
            try:
                text = await self.parse_channel_info(url)
                if text is None:
                    return message.plain_result("无法访问该 RSS 链接，请检查链接是否可用")
                title, desc = self.data_handler.parse_channel_text_info(text)
            except Exception as e:
                return message.plain_result(f"解析频道信息失败: {str(e)}")

            self.data_handler.data[url] = {
                "subscribers": {
                    user: {
                        "cron_expr": cron_expr,
                        "last_update": last_update,
                        "latest_link": latest_link,
                    }
                },
                "info": {
                    "title": title,
                    "description": desc,
                },
            }

        self.data_handler.save_data()
        return self.data_handler.data[url]["info"]

    async def _get_chain_components(self, item: RSSItem):
        """组装消息链"""
        comps = []
        time_text = self._format_item_time(item)

        header_lines = [
            f"频道 {item.chan_title} 最新 Feed",
            f"标题: {item.title}",
            f"时间: {time_text}",
        ]
        if not self.is_hide_url:
            header_lines.append(f"链接: {item.link}")

        comps.append(Comp.Plain("\n".join(header_lines) + "\n"))
        comps.append(Comp.Plain(item.description + "\n"))

        if self.is_read_pic and item.pic_urls:
            # 如果max_pic_item为-1则不限制图片数量
            temp_max_pic_item = len(item.pic_urls) if self.max_pic_item == -1 else self.max_pic_item
            for pic_url in item.pic_urls[:temp_max_pic_item]:
                base64str = await self.pic_handler.modify_corner_pixel_to_base64(pic_url)
                if base64str is None:
                    comps.append(Comp.Plain("图片链接读取失败\n"))
                    continue
                else:
                    comps.append(Comp.Image.fromBase64(base64str))
        return comps

    async def _send_translation_followup(self, umo: str, item: RSSItem) -> None:
        try:
            translated_text = await self._translate_item_text(umo=umo, item=item)
            if not translated_text:
                return
            message_chain = MessageChain(chain=[Comp.Plain(translated_text + "\n")], use_t2i_=self.t2i)
            await self.context.send_message(umo, message_chain)
        except Exception as exc:
            self.logger.warning(f"rss: 发送翻译消息失败: {exc}")

    def _build_translation_source_text(self, item: RSSItem) -> str:
        parts = [str(item.title or "").strip(), str(item.description or "").strip()]
        return "\n".join([p for p in parts if p]).strip()

    def _build_language_detection_text(self, item: RSSItem) -> str:
        return "\n".join([item.title or "", item.description or ""]).strip()

    async def _translate_item_text(self, umo: str, item: RSSItem) -> Optional[str]:
        if not self.translate_enabled:
            return None

        target_language = self.translate_target_language
        if not target_language:
            return None

        source_text = self._build_translation_source_text(item)
        if not source_text:
            return None
        detection_text = self._build_language_detection_text(item) or source_text

        provider_id = self.translate_provider_id
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception as exc:
                self.logger.warning(f"rss: 获取翻译 provider 失败: {exc}")
                return None

        if not provider_id:
            self.logger.warning("rss: 未找到可用翻译 provider，跳过翻译")
            return None

        detected_language = await self._detect_language(provider_id, detection_text)
        if detected_language and self._language_matches_target(detected_language, target_language):
            self.logger.info(
                f"rss: 检测到语言 {detected_language} 与目标语言 {target_language} 匹配，跳过翻译",
            )
            return None

        translated_text = await self._request_translation(
            provider_id=provider_id,
            target_language=target_language,
            text=source_text,
            system_prompt="你是翻译助手。只输出翻译结果，不要解释。",
        )

        if not translated_text or translated_text == source_text:
            translated_text = await self._request_translation(
                provider_id=provider_id,
                target_language=target_language,
                text=source_text,
                system_prompt="Translate the given text and output translation only.",
            )

        if not translated_text:
            return None

        return translated_text

    async def _request_translation(
        self,
        provider_id: str,
        target_language: str,
        text: str,
        system_prompt: str,
    ) -> Optional[str]:
        prompt = (
            f"请将以下 RSS 文本翻译为 {target_language}。\n"
            f"若文本中包含具体时间、日期、时区信息，请换算并统一为 {self.translation_timezone} 时区后再输出。\n"
            "保持原文结构，仅输出翻译结果，不要解释：\n\n"
            f"{text}"
        )
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=prompt,
            )
            translated_text = (llm_resp.completion_text or "").strip()
            return translated_text or None
        except Exception as exc:
            self.logger.warning(f"rss: 翻译请求失败: {exc}")
            return None

    async def _detect_language(
        self,
        provider_id: str,
        text: str,
    ) -> Optional[str]:
        detected_by_google = await self._detect_language_by_google(text)
        if detected_by_google:
            self.logger.info(
                f"rss: language detection API=GoogleFree detected={detected_by_google}",
            )
            return detected_by_google

        self.logger.info("rss: language detection GoogleFree unavailable, fallback to LLM")
        detected_by_llm = await self._detect_language_by_llm(provider_id, text)
        if detected_by_llm:
            self.logger.info(
                f"rss: language detection API=LLM provider={provider_id} detected={detected_by_llm}",
            )
        return detected_by_llm

    async def _detect_language_by_google(self, text: str) -> Optional[str]:
        sample_text = text.strip()
        if not sample_text:
            return None

        if len(sample_text) > 2000:
            sample_text = sample_text[:2000]

        api_url = "https://translate.googleapis.com/translate_a/single"
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": "astrbot-plugin-rss/1.1.2"}
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": "en",
            "dt": "t",
            "q": sample_text,
        }

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(api_url, params=params) as response:
                    if response.status >= 400:
                        self.logger.warning(
                            f"rss: Google language detect HTTP {response.status}",
                        )
                        return None
                    data = await response.json(content_type=None)
        except Exception as exc:
            self.logger.warning(f"rss: Google language detect failed: {exc}")
            return None

        detected_language = None
        if isinstance(data, list) and len(data) >= 3 and isinstance(data[2], str):
            detected_language = data[2].strip()

        if not detected_language:
            self.logger.warning("rss: Google language detect returned unexpected payload")
            return None

        code_match = re.search(
            r"\b([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)\b",
            detected_language,
        )
        if not code_match:
            self.logger.warning("rss: Google language detect returned invalid language code")
            return None
        return code_match.group(1)

    async def _detect_language_by_llm(
        self,
        provider_id: str,
        text: str,
    ) -> Optional[str]:
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=(
                    "你是语言识别助手。只返回语言代码，如 en、zh-Hans、ja。"
                ),
                prompt=text,
            )
            response_text = (llm_resp.completion_text or "").strip()
        except Exception as exc:
            self.logger.warning(f"rss: fallback LLM language detection failed: {exc}")
            return None

        code_match = re.search(
            r"\b([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)\b",
            response_text,
        )
        if not code_match:
            self.logger.warning("rss: fallback LLM language detection returned invalid language code")
            return None
        return code_match.group(1)

    def _language_matches_target(self, detected: str, target: str) -> bool:
        detected_norm = detected.strip().lower().replace("_", "-")
        target_norm = target.strip().lower().replace("_", "-")
        if not detected_norm or not target_norm:
            return False
        if detected_norm == target_norm:
            return True
        if detected_norm.startswith(target_norm) or target_norm.startswith(detected_norm):
            return True
        if detected_norm.startswith("zh") and target_norm.startswith("zh"):
            return True
        return False

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _is_url_or_ip(self,text: str) -> bool:
        """
        判断一个字符串是否为网址（http/https 开头）或 IP 地址。
        """
        url_pattern = r"^(?:http|https)://.+$"
        ip_pattern = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
        return bool(re.match(url_pattern, text) or re.match(ip_pattern, text))

    @filter.command_group("rss", alias={"RSS"})
    def rss(self):
        """RSS订阅插件

        可以订阅和管理多个RSS源，支持cron表达式设置更新频率

        cron 表达式格式：
        * * * * *，分别表示分钟 小时 日 月 星期，* 表示任意值，支持范围和逗号分隔。例：
        1. 0 0 * * * 表示每天 0 点触发。
        2. 0/5 * * * * 表示每 5 分钟触发。
        3. 0 9-18 * * * 表示每天 9 点到 18 点触发。
        4. 0 0 1,15 * * 表示每月 1 号和 15 号 0 点触发。
        星期的取值范围是 0-6，0 表示星期天。
        """
        pass

    @rss.group("rsshub")
    def rsshub(self, event: AstrMessageEvent):
        """RSSHub相关操作

        可以添加、查看、删除RSSHub的端点
        """
        pass

    @rsshub.command("add")
    async def rsshub_add(self, event: AstrMessageEvent, url: str):
        """添加一个RSSHub端点

        Args:
            url: RSSHub服务器地址，例如：https://rsshub.app
        """
        if url.endswith("/"):
            url = url[:-1]
        # 检查是否为url或ip
        if not self._is_url_or_ip(url):
            yield event.plain_result("请输入正确的URL")
            return
        # 检查该网址是否已存在
        elif url in self.data_handler.data["rsshub_endpoints"]:
            yield event.plain_result("该RSSHub端点已存在")
            return
        else:
            self.data_handler.data["rsshub_endpoints"].append(url)
            self.data_handler.save_data()
            yield event.plain_result("添加成功")

    @rsshub.command("list")
    async def rsshub_list(self, event: AstrMessageEvent):
        """列出所有已添加的RSSHub端点"""
        lines = []
        if self.rsshub_base_url:
            lines.append(f"配置中的默认 endpoint: {self.rsshub_base_url}")
        lines.extend(
            [f"{i}: {x}" for i, x in enumerate(self.data_handler.data["rsshub_endpoints"])]
        )
        if not lines:
            yield event.plain_result(
                "当前没有可用的 rsshub endpoint。请先在插件配置中填写 rsshub_base_url，或使用 /rss rsshub add 添加。"
            )
            return
        yield event.plain_result("当前Bot可用的 rsshub endpoint：\n" + "\n".join(lines))

    @rsshub.command("remove")
    async def rsshub_remove(self, event: AstrMessageEvent, idx: int):
        """删除一个RSSHub端点

        Args:
            idx: 要删除的端点索引，可通过list命令查看
        """
        if idx < 0 or idx >= len(self.data_handler.data["rsshub_endpoints"]):
            yield event.plain_result("索引越界")
            return
        else:
            self.data_handler.data["rsshub_endpoints"].pop(idx)
            self.data_handler.save_data()
            yield event.plain_result("删除成功")

    @rss.command("add")
    async def add_command(
        self,
        event: AstrMessageEvent,
        idx: int,
        route: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """通过RSSHub路由添加订阅

        Args:
            idx: RSSHub端点索引（若配置了 rsshub_base_url，可直接使用 0）
            route: RSSHub路由，需以/开头
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        endpoint = self._get_rsshub_endpoint(idx)
        if not endpoint:
            yield event.plain_result(
                "请先在插件配置中填写 rsshub_base_url，或使用 /rss rsshub add 添加 endpoint"
            )
            return
        if not self._is_url_or_ip(endpoint):
            yield event.plain_result("配置的 rsshub_base_url 不是有效 URL，请检查插件配置")
            return
        if not route.startswith("/"):
            yield event.plain_result("路由必须以 / 开头")
            return

        url = endpoint + route
        url = self._append_rsshub_query_param(url)
        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"

        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret["title"]
            chan_desc = ret["description"]

        # 刷新定时任务
        await self._refresh_future_tasks()

        yield event.plain_result(
            f"添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}"
        )

    @rss.command("add-url")
    async def add_url_command(
        self,
        event: AstrMessageEvent,
        url: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """直接通过Feed URL添加订阅

        Args:
            url: RSS Feed的完整URL
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"
        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret["title"]
            chan_desc = ret["description"]

        # 刷新定时任务
        await self._refresh_future_tasks()

        yield event.plain_result(
            f"添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}"
        )

    @rss.command("list")
    async def list_command(self, event: AstrMessageEvent):
        """列出当前所有订阅的RSS频道"""
        user = event.unified_msg_origin
        ret = "当前订阅的频道：\n"
        subs_urls = self.data_handler.get_subs_channel_url(user)
        cnt = 0
        for url in subs_urls:
            info = self.data_handler.data[url]["info"]
            ret += f"{cnt}. {info['title']} - {info['description']}\n"
            cnt += 1
        yield event.plain_result(ret)

    @rss.command("remove")
    async def remove_command(self, event: AstrMessageEvent, idx: int):
        """删除一个RSS订阅

        Args:
            idx: 要删除的订阅索引，可通过/rss list查看
        """
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        url = subs_urls[idx]
        self.data_handler.data[url]["subscribers"].pop(event.unified_msg_origin)

        self.data_handler.save_data()

        # 刷新定时任务
        await self._refresh_future_tasks()
        yield event.plain_result("删除成功")

    @rss.command("get")
    async def get_command(self, event: AstrMessageEvent, idx: int):
        """获取指定订阅的最新内容

        Args:
            idx: 要查看的订阅索引，可通过/rss list查看
        """
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        url = subs_urls[idx]
        rss_items = await self.poll_rss(url)
        if not rss_items:
            yield event.plain_result("没有新的订阅内容")
            return
        item = rss_items[0]
        # 分解MessageSesion
        platform_name,message_type,session_id = event.unified_msg_origin.split(":")
        # 构造返回消息链
        comps = await self._get_chain_components(item)
        # 区分平台
        if(platform_name == "aiocqhttp" and self.is_compose):
            node = Comp.Node(
                    uin=0,
                    name="Astrbot",
                    content=comps
                )
            yield event.chain_result([node]).use_t2i(self.t2i)
        else:
            yield event.chain_result(comps).use_t2i(self.t2i)

        translated_text = await self._translate_item_text(event.unified_msg_origin, item)
        if translated_text:
            yield event.chain_result([Comp.Plain(translated_text + "\n")]).use_t2i(self.t2i)
