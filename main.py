import asyncio
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import *
from astrbot.api.star import StarTools
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event.filter import (
    EventMessageType,
    after_message_sent,
    command,
    event_message_type,
    permission_type,
    PermissionType,
)
from astrbot.api.message_components import Plain

REPORT_PREFIX = "\U0001f4ca 聊天记录分析报告"

_BUILTIN_PROMPTS: dict[str, str] = {
    "default": (
        "你是群聊审核助手，不是普通摘要助手。你的目标是帮助管理员快速判断这批聊天记录"
        "是否需要介入，以及需要关注哪些具体消息。\n"
        "重点完成以下任务：\n"
        "1. 识别本轮主要讨论主题，但只保留与管理、秩序、争议、异常行为有关的内容。\n"
        "2. 判断整体氛围是正常、轻微紧张、明显冲突还是已经失控，并说明依据。\n"
        "3. 提炼真正值得管理员知道的焦点，不要把普通闲聊包装成风险。\n"
        "4. 判断是否存在争吵、挑衅、刷屏、引战、广告、骚扰、违规内容等问题。\n"
        "5. 给出简洁可执行的处理建议，例如观察、提醒、撤回、禁言、人工复核。\n"
        "要求：结论要克制、基于聊天原文，不要脑补图片内容，不要夸大语气词。"
    ),
    "topic": (
        "你负责话题维度审核，只输出与管理价值有关的话题结论。\n"
        "请重点识别：\n"
        "1. 当前主要话题是什么，哪些话题占比最高。\n"
        "2. 话题是否偏离群定位，是否出现无关灌水、广告导流、敏感议题或引战点。\n"
        "3. 是否存在话题快速转向争议、对立、带节奏的迹象。\n"
        "4. 哪些话题值得管理员继续观察，哪些只是正常闲聊无需处理。\n"
        "要求：只保留关键话题，不做长篇概述。"
    ),
    "sentiment": (
        "你负责群聊舆情和情绪稳定性判断。\n"
        "请重点识别：\n"
        "1. 整体情绪是稳定、中性、轻微负面、明显对立还是持续升级。\n"
        "2. 是否存在抱怨、阴阳怪气、挑衅、辱骂、围攻、恐慌传播等负面信号。\n"
        "3. 负面情绪是个别发言还是已影响多人互动。\n"
        "4. 是否需要管理员介入安抚、提醒规则或及时止损。\n"
        "要求：避免把普通吐槽当成严重风险，只有在情绪已影响群秩序时再提高等级。"
    ),
    "activity": (
        "你负责社群互动质量分析，但仍以管理员视角输出。\n"
        "请重点识别：\n"
        "1. 本轮互动是正常交流、少数人连续刷屏，还是多人有效互动。\n"
        "2. 是否存在明显灌水、复读、无意义刷表情、连续打断他人等影响秩序的行为。\n"
        "3. 哪些成员持续主导讨论，是否形成单人刷屏或小圈子霸占话题。\n"
        "4. 对活跃度只给简短结论，重点指出是否影响阅读体验或管理成本。\n"
        "要求：不要做运营复盘式长建议，优先指出异常互动模式。"
    ),
    "risk": (
        "你负责严格的内容安全与违规审查，请优先识别可执行的风险点。\n"
        "请重点检测：\n"
        "1. 广告导流、无关推广、刷屏、引流联系方式、可疑拉群或交易行为。\n"
        "2. 违法违规、政治敏感、暴力威胁、极端言论、诈骗、赌博、色情等内容。\n"
        "3. 人身攻击、侮辱、歧视、挑衅、网暴、骚扰等破坏群秩序的发言。\n"
        "4. 隐私泄露，如手机号、身份证、住址、实名信息、账号密码等敏感信息。\n"
        "5. 其他明显违反群规或平台规范、需要管理员立即处理的内容。\n"
        "要求：只有在聊天原文能直接支持时才判定违规；证据不足时写需关注，不要硬判。"
    ),
}

_PRESET_LABELS: dict[str, str] = {
    "default": "综合审核",
    "topic": "主题分析",
    "sentiment": "舆情监控",
    "activity": "活跃度分析",
    "risk": "风险检测",
}

_TRIGGER_COOLDOWN_SECONDS = 60
_SKIP_SILENT_MARKER = "[无需报告]"

_CLEAN_MESSAGE_RE = re.compile(r"\[[^\]]*\](?:\s*)")

_POSITION_LINE_RE = re.compile(r"(关注|复核|违规)\s*#(\d+)\s*(.+)", re.IGNORECASE)


def _extract_overall_conclusion(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip(" -*\t") for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if line == "【总体结论】":
            for next_line in lines[idx + 1:]:
                if not next_line:
                    continue
                if next_line.startswith("【"):
                    break
                if "建议复核" in next_line or "需复核" in next_line:
                    return "建议复核"
                if "存在违规" in next_line:
                    return "建议复核"
                if "需关注" in next_line:
                    return "需关注"
                if "正常" in next_line:
                    return "正常"
    for line in lines:
        if "建议复核" in line or "需复核" in line:
            return "建议复核"
        if "存在违规" in line:
            return "建议复核"
        if "需关注" in line:
            return "需关注"
        if "正常" in line:
            return "正常"
    return ""


def _extract_position_items(text: str, max_idx: int) -> List[Tuple[str, int, str]]:
    if not text:
        return []
    items: List[Tuple[str, int, str]] = []
    seen: set[Tuple[str, int]] = set()
    for line in text.splitlines():
        m = _POSITION_LINE_RE.search(line)
        if m:
            level = m.group(1).strip()
            idx = int(m.group(2))
            key = (level, idx)
            if 1 <= idx <= max_idx and key not in seen:
                items.append((level, idx, m.group(3).strip()))
                seen.add(key)
    return items


def _sanitize_analysis_output(text: str) -> str:
    if not text:
        return text

    sanitized = text
    replacements = [
        ("存在违规", "建议复核"),
        ("发现违规", "建议复核"),
        ("疑似违规", "建议复核"),
        ("违规 #", "复核 #"),
        ("违规项", "复核项"),
        ("违规内容", "风险内容"),
        ("违规言论", "风险言论"),
        ("违规行为", "风险行为"),
    ]
    for src, dst in replacements:
        sanitized = sanitized.replace(src, dst)

    return sanitized


def _resolve_group_prompts(gc: "GroupConfig") -> List[Tuple[str, str]]:
    prompts: List[Tuple[str, str]] = []
    for key in gc.presets:
        if key == "custom":
            prompts.append(("custom", gc.custom_prompt or _BUILTIN_PROMPTS["default"]))
        elif key in _BUILTIN_PROMPTS:
            prompts.append((key, _BUILTIN_PROMPTS[key]))
    return prompts if prompts else [("default", _BUILTIN_PROMPTS["default"])]


def _build_combined_prompt(prompts: List[Tuple[str, str]], skip_silent: bool = True, group_rules: str = "") -> str:
    lines: List[str] = []
    lines.append("你正在审查一批群聊消息，请严格以“群管理/内容审核”视角输出。")
    lines.append("输入中的每条消息都带有 [#N] 编号，编号是唯一定位依据。")
    lines.append("消息内容可能是从图文消息中提取出的纯文本；未提供的图片信息一律视为未知，不得脑补。")
    lines.append("请只根据已给出的聊天内容下结论，避免过度解读。")
    lines.append("")
    if group_rules:
        lines.append("【群规参考】")
        lines.append("以下为该群的群规，请将其作为判断消息是否违规的重要依据：")
        for rule_line in group_rules.strip().splitlines():
            stripped = rule_line.strip()
            if stripped:
                lines.append(f"  {stripped}")
        lines.append("在分析时，如果某条消息违反了以上群规中的任一条款，"
                     "应将其视为需要关注或复核的内容。")
        lines.append("")
    lines.append("安全输出规则：")
    lines.append("1. 你的输出必须合规、克制、中性，服务于管理员审核，不得生成煽动、辱骂、色情、诈骗、暴力或违法指导内容。")
    lines.append("2. 不要复述原始违规文案，不要逐字引用脏话、露骨色情描述、诈骗话术、政治极端口号、暴力细节。")
    lines.append("3. 如需说明问题，只能使用抽象概括和风险标签，不得写入具体敏感表述。")
    lines.append("4. 不得输出任何联系方式、账号、二维码、链接、身份证号、手机号、地址、银行卡等敏感信息；如果原文出现，只能概括为“联系方式”或“隐私信息”。")
    lines.append("5. 不得提供规避审核、逃避风控、违法操作、攻击他人或扩大传播的建议。")
    lines.append("6. 不要直接评判用户人格，只描述可观察到的发言风险和群管理影响。")
    lines.append("7. 不得在任何位置出现引号内容、括号补充、原词示例、具体题材词、具体癖好词、具体暴力方式、具体辱骂词、具体群体标签。")
    lines.append("8. 遇到敏感话题时，一律改写为抽象类别，如“话题尺度风险”“表达冲突风险”“内容边界风险”“群秩序风险”“需人工复核的高风险表达”。")
    lines.append("")
    lines.append("判定原则：")
    lines.append("1. 普通闲聊、玩笑、轻微吐槽，如果未明显影响群秩序，不要上升为违规。")
    lines.append("2. 只有当某条消息确实值得管理员关注或处理时，才将其列入定位清单。")
    lines.append("3. 对明显异常或高风险内容，不要直接下最终定性，统一使用“复核 #N 原因”的格式逐条列出。")
    lines.append("4. 同一条消息如涉及多项问题，请合并成一条原因，保持简洁。")
    lines.append("5. 如果只是整体气氛一般、轻微偏题、活跃度异常，但没有明确问题，可在正文说明，不要伪造违规编号。")
    lines.append("")
    lines.append("请综合以下分析维度：")
    for key, text in prompts:
        label = _PRESET_LABELS.get(key, key)
        lines.append(f"【{label}】")
        lines.append(text)
        lines.append("")
    if skip_silent:
        lines.append(
            f"静默规则：如果本轮聊天整体正常，没有任何值得管理员关注的问题，"
            f"也没有需要点名定位的消息，请仅回复「{_SKIP_SILENT_MARKER}」，不要输出任何其他内容。"
        )
    lines.append("输出要求：")
    lines.append("1. 必须使用中文。")
    lines.append("2. 除非触发静默规则，否则严格按下面模板输出，不要省略标题，不要改标题名称。")
    lines.append("3. 每个部分尽量简洁，优先写管理员真正需要看的信息。")
    lines.append("4. 不得在摘要、风险与依据、处理建议、定位清单中粘贴原始违规句子；只允许做安全概括。")
    lines.append("5. 定位清单中的原因必须是短句标签，长度尽量控制在 8-20 个字，不要展开复述聊天原文。")
    lines.append("6. 【风险与依据】只能写抽象风险类别，不得写具体讨论内容，不得写消息原词，不得写“某类题材词/某句原话/某种细节”。")
    lines.append("7. 【处理建议】只能写管理动作，如提醒、观察、收敛话题、人工复核，不得重提具体风险内容。")
    lines.append("8. 【定位清单】只能写抽象标签，如“话题尺度风险”“表达冲突风险”“群秩序风险”“需人工复核”。")
    lines.append("")
    lines.append("【总体结论】")
    lines.append("填写“正常 / 需关注 / 建议复核”三选一。不要使用“存在违规”等直接定性表述。")
    lines.append("【摘要】")
    lines.append("用 2-4 句概括当前话题、氛围和最值得关注的点。")
    lines.append("【风险与依据】")
    lines.append("按要点列出抽象风险判断；没有明确风险时写“未发现明确风险”。")
    lines.append("【处理建议】")
    lines.append("给出简短建议；无需处理时写“建议继续观察”。")
    lines.append("【定位清单】")
    lines.append("若总体结论为“需关注”，每行一个：关注 #N 具体原因")
    lines.append("若总体结论为“建议复核”，每行一个：复核 #N 具体原因")
    lines.append("只有总体结论为“正常”时，才允许写“无”")
    lines.append("")
    lines.append("安全示例：")
    lines.append("【总体结论】")
    lines.append("建议复核")
    lines.append("【风险与依据】")
    lines.append("1. 存在话题尺度风险。")
    lines.append("2. 存在表达冲突风险。")
    lines.append("【处理建议】")
    lines.append("建议提醒成员收敛话题，并结合上下文人工复核。")
    lines.append("【定位清单】")
    lines.append("关注 #2 话题尺度需控制")
    lines.append("复核 #7 表达可能影响群秩序")
    return "\n".join(lines)


@dataclass
class GroupConfig:
    group_id: str
    presets: List[str] = field(default_factory=lambda: ["default"])
    custom_prompt: str = ""
    group_rules: str = ""
    trigger_keywords: List[str] = field(default_factory=list)
    target_session: str = ""


def _split_ids(raw_ids: str) -> List[str]:
    return [
        gid.strip()
        for gid in raw_ids.replace("，", ",").split(",")
        if gid.strip()
    ]


def _parse_group_configs(config: dict) -> Dict[str, GroupConfig]:
    raw = config.get("chat_analysis_group_configs")
    if not isinstance(raw, list) or not raw:
        return {}
    result: Dict[str, GroupConfig] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        ids_raw = str(item.get("group_ids", "")).strip()
        if not ids_raw:
            ids_raw = str(item.get("group_id", "")).strip()
        group_ids = _split_ids(ids_raw)
        if not group_ids:
            continue
        preset_val = item.get("presets", ["default"])
        if isinstance(preset_val, list):
            preset_candidates = [str(p).strip() for p in preset_val if str(p).strip()]
        else:
            preset_candidates = [
                p.strip()
                for p in str(preset_val).replace("，", ",").split(",")
                if p.strip()
            ]
        valid_presets = [
            p for p in preset_candidates if p in _BUILTIN_PROMPTS or p == "custom"
        ]
        if not valid_presets:
            valid_presets = ["default"]
        keyword_raw = str(item.get("trigger_keywords", "")).strip()
        keywords = [
            kw.strip()
            for kw in keyword_raw.replace("，", ",").split(",")
            if kw.strip()
        ]
        shared_config = dict(
            presets=valid_presets,
            custom_prompt=str(item.get("custom_prompt", "")).strip(),
            group_rules=str(item.get("group_rules", "")).strip(),
            trigger_keywords=keywords,
            target_session=str(item.get("target_session", "")).strip(),
        )
        for gid in group_ids:
            result[gid] = GroupConfig(group_id=gid, **shared_config)
    return result


@register("astrbot_plugin_chat_analyzer", "makotowu", "", "", "")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        self.interval_minutes = max(
            1, int(self.cfg.get("chat_analysis_interval_minutes", 30))
        )
        self.max_messages = max(
            0, int(self.cfg.get("chat_analysis_max_messages", 0))
        )
        self.skip_silent = bool(self.cfg.get("chat_analysis_skip_silent", True))
        self.target_session = str(
            self.cfg.get("chat_analysis_target_session", "") or ""
        ).strip()

        self._group_configs = _parse_group_configs(config)
        self._active = (
            bool(self.cfg.get("chat_analysis_enabled", False))
            and bool(self._group_configs)
        )

        if not self._active:
            if not self._group_configs:
                logger.warning(
                    "未配置任何策略组，插件不会运行。"
                    "请在 WebUI 的「多群独立分析策略组」中添加策略组并关联群号。"
                )
        else:
            logger.info(
                f"聊天记录分析已启动，间隔 {self.interval_minutes} 分钟，"
                f"最大消息数 {str(self.max_messages) if self.max_messages > 0 else '不限制'}，"
                f"监控群数 {len(self._group_configs)}"
            )

        self._data_path = os.path.join(
            StarTools.get_data_dir(plugin_name="astrbot_plugin_chat_analyzer"),
            "buffers.json",
        )

        self._buffers: Dict[str, Deque[_ChatRecord]] = {}
        self._lock = asyncio.Lock()
        self._trigger_cooldowns: Dict[str, float] = {}
        self._trigger_tasks: Set[asyncio.Task] = set()
        self._bot_id: str = ""

        self._msg_count: Dict[str, int] = {}
        self._load_buffers()

        self._task: Optional[asyncio.Task] = None
        if self._active:
            self._task = asyncio.create_task(self._analysis_loop())

    def _buffer_key(self, group_id: str) -> str:
        return group_id if group_id else "_private"

    def _ensure_buffer(self, key: str) -> None:
        if key not in self._buffers:
            cap = self.max_messages * 3 if self.max_messages > 0 else 10000
            self._buffers[key] = deque(maxlen=cap)

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        return event.get_sender_id() or ""

    def _get_self_id(self, event: AstrMessageEvent) -> str:
        sid = event.get_self_id() or ""
        if sid and not self._bot_id:
            self._bot_id = sid
        return sid

    def _load_buffers(self) -> None:
        if not os.path.exists(self._data_path):
            return
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            loaded = 0
            for key, items in data.items():
                if not isinstance(items, list):
                    continue
                records = [_ChatRecord.from_dict(r) for r in items if isinstance(r, dict)]
                if records:
                    self._ensure_buffer(key)
                    self._buffers[key].extend(records)
                    loaded += len(records)
            if loaded:
                logger.info(f"从磁盘恢复了 {loaded} 条待分析消息")
        except Exception as e:
            logger.error(f"加载持久化缓冲失败: {e}")

    def _save_buffers(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
            data: Dict[str, List[Dict[str, Any]]] = {}
            for key, buf in self._buffers.items():
                if buf:
                    data[key] = [r.to_dict() for r in buf]
            with open(self._data_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓冲到磁盘失败: {e}")

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self._active:
            return
        group_id = event.get_group_id() or ""
        if group_id not in self._group_configs:
            return
        sender = event.get_sender_name() or "unknown"
        text = _CLEAN_MESSAGE_RE.sub("", (event.message_str or "").strip()).strip()
        if not text:
            return
        if text.startswith(REPORT_PREFIX):
            return
        record = _ChatRecord(
            session_id=event.unified_msg_origin,
            sender=sender,
            sender_id=self._get_sender_id(event),
            content=text,
            timestamp=time.time(),
            is_group=bool(group_id),
            group_id=group_id,
        )

        buf_key = self._buffer_key(group_id)
        async with self._lock:
            self._ensure_buffer(buf_key)
            self._buffers[buf_key].append(record)

        self._msg_count[buf_key] = self._msg_count.get(buf_key, 0) + 1
        if self._msg_count[buf_key] % 10 == 0:
            self._save_buffers()

        await self._check_trigger(group_id, text)

    @after_message_sent()
    async def on_bot_reply(self, event: AstrMessageEvent):
        if not self._active:
            return
        group_id = event.get_group_id() or ""
        if group_id not in self._group_configs:
            return
        result = event.get_result()
        if not result:
            return
        chain = getattr(result, "chain", None) or []
        text_parts = []
        for seg in chain:
            seg_text = getattr(seg, "text", None)
            if seg_text:
                text_parts.append(str(seg_text))
        text = "".join(text_parts).strip()
        if not text:
            return
        if text.startswith(REPORT_PREFIX):
            return
        record = _ChatRecord(
            session_id=event.unified_msg_origin,
            sender="AstrBot",
            sender_id=self._get_self_id(event),
            content=text,
            timestamp=time.time(),
            is_group=bool(group_id),
            group_id=group_id,
        )

        buf_key = self._buffer_key(group_id)
        async with self._lock:
            self._ensure_buffer(buf_key)
            self._buffers[buf_key].append(record)

        self._msg_count[buf_key] = self._msg_count.get(buf_key, 0) + 1
        if self._msg_count[buf_key] % 10 == 0:
            self._save_buffers()

    @permission_type(PermissionType.ADMIN)
    @command("analyze", alias={"审核", "analyze_now", "分析"})
    async def cmd_analyze(self, event: AstrMessageEvent, gid: str = ""):
        if not self._active:
            yield event.plain_result("插件未启用或未配置策略组，请先在 WebUI 中配置。")
            return
        group_id = (gid or "").strip()
        if not group_id:
            group_id = event.get_group_id() or ""
        if not group_id:
            yield event.plain_result("请指定群号，例如：/审核 123456")
            return
        if group_id not in self._group_configs:
            yield event.plain_result(
                f"群 {group_id} 未配置分析策略，请在 WebUI 的「多群独立分析策略组」中添加。"
            )
            return
        buf_key = self._buffer_key(group_id)
        async with self._lock:
            pending = len(self._buffers.get(buf_key, deque()))
        if not pending:
            yield event.plain_result(f"群 {group_id} 当前没有待分析的聊天记录。")
            return

        gc = self._group_configs[group_id]
        admin_name = event.get_sender_name() or "管理员"
        admin_id = event.get_sender_id() or ""
        target = gc.target_session or self.target_session

        if target:
            notify = (
                f"\U0001f4e2 管理员 {admin_name}({admin_id}) "
                f"发起了对群 {group_id} 的手动审核，正在分析中..."
            )
            try:
                await self.context.send_message(
                    target,
                    MessageChain(chain=[Plain(notify)]),
                )
            except Exception as e:
                logger.error(f"发送审核通知失败: {e}")

        result_target_text = "通知群" if target else "日志"
        yield event.plain_result(f"正在分析群 {group_id}，结果将发送到{result_target_text}...")
        await self._run_analysis_for_group(group_id, gc, force_report=True)

    async def terminate(self):
        self._save_buffers()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for t in list(self._trigger_tasks):
            if not t.done():
                t.cancel()

    async def _analysis_loop(self):
        while True:
            try:
                await asyncio.sleep(self.interval_minutes * 60)
                for gid, gc in self._group_configs.items():
                    await self._run_analysis_for_group(gid, gc)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"聊天记录分析循环异常: {e}")

    async def _run_analysis_for_group(self, group_id: str, gc: GroupConfig, *, force_report: bool = False):
        buf_key = self._buffer_key(group_id)
        records: List[_ChatRecord] = []
        async with self._lock:
            buf = self._buffers.get(buf_key)
            if not buf:
                return
            count = len(buf) if self.max_messages <= 0 else min(len(buf), self.max_messages)
            for _ in range(count):
                records.append(buf.popleft())
        if not records:
            return

        prompts = _resolve_group_prompts(gc)
        system_prompt = _build_combined_prompt(prompts, self.skip_silent and not force_report, gc.group_rules)
        chat_text = "\n".join(r.format(i + 1) for i, r in enumerate(records))

        logger.info(
            f"开始分析群 {group_id} 的 {len(records)} 条记录，"
            f"策略数 {len(prompts)}（合并为单次 AI 调用）"
        )

        raw_analysis_result = await self._call_ai_analysis(chat_text, system_prompt)
        if not raw_analysis_result:
            async with self._lock:
                buf = self._buffers.get(buf_key)
                if buf is not None:
                    buf.extendleft(reversed(records))
            logger.error(f"群 {group_id} AI 分析返回空结果。")
            return

        analysis_result = _sanitize_analysis_output(raw_analysis_result)

        if self.skip_silent and not force_report and analysis_result.strip() == _SKIP_SILENT_MARKER:
            logger.info(f"群 {group_id} 本轮分析无异常，跳过报告推送。")
            return

        overall_conclusion = _extract_overall_conclusion(analysis_result)
        position_items = _extract_position_items(analysis_result, len(records))
        flagged_pairs: Optional[List[Tuple[str, str, "_ChatRecord"]]] = None

        if overall_conclusion != "正常":
            if position_items:
                pairs: List[Tuple[str, str, "_ChatRecord"]] = []
                for level, idx, reason in position_items:
                    if level == "违规":
                        level = "复核"
                    pairs.append((level, reason, records[idx - 1]))
                flagged_pairs = pairs
                logger.info(
                    f"群 {group_id} 提取到 {len(position_items)} 条定位消息:"
                    f" {[f'{level}#{idx}({reason})' for level, idx, reason in position_items]}"
                )
            else:
                fallback_reason = (
                    f"AI 总体结论为“{overall_conclusion or '需人工复核'}”，"
                    "但未返回明确编号，请逐条人工复核"
                )
                flagged_pairs = [("关注", fallback_reason, record) for record in records]
                logger.warning(
                    f"群 {group_id} 总体结论为 {overall_conclusion or '未知'}，"
                    "但未提取到定位清单，已回退为整批消息逐条转发"
                )

        header = self._build_header(records, prompts)
        full_report = header + analysis_result

        target = gc.target_session or self.target_session
        await self._send_result(target, full_report, analysis_result, flagged_pairs)
        self._save_buffers()

    async def _check_trigger(self, group_id: str, text: str):
        if not group_id:
            return
        gc = self._group_configs.get(group_id)
        if not gc or not gc.trigger_keywords:
            return
        if not any(kw in text for kw in gc.trigger_keywords):
            return

        now = time.time()
        last = self._trigger_cooldowns.get(group_id, 0)
        if now - last < _TRIGGER_COOLDOWN_SECONDS:
            return
        self._trigger_cooldowns[group_id] = now

        task = asyncio.create_task(self._trigger_analysis(group_id, gc))
        self._trigger_tasks.add(task)
        task.add_done_callback(self._trigger_tasks.discard)
        logger.info(f"群 {group_id} 触发关键词即时分析")

    async def _trigger_analysis(self, group_id: str, gc: GroupConfig):
        try:
            await self._run_analysis_for_group(group_id, gc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"群 {group_id} 即时分析异常: {e}")

    def _build_header(
        self,
        records: List["_ChatRecord"],
        prompts: List[Tuple[str, str]],
    ) -> str:
        labels = [_PRESET_LABELS.get(k, k) for k, _ in prompts]
        return (
            f"{REPORT_PREFIX}\n"
            f"\u23f0 时间范围: {_format_time_range(records)}\n"
            f"\U0001f4dd 消息数量: {len(records)}\n"
            f"\U0001f465 会话: {_extract_session_info(records)}\n"
            f"\U0001f4cb 分析策略: {'  '.join(labels)}\n"
            f"{'\u2500' * 30}\n"
        )

    async def _call_ai_analysis(
        self, chat_text: str, system_prompt: str
    ) -> Optional[str]:
        try:
            umo = self.target_session if self.target_session else None
            provider = self.context.get_using_provider(umo=umo)
            if provider is None:
                logger.error("无法获取 LLM 提供商，请检查配置。")
                return None

            resp = await provider.text_chat(
                prompt=f"聊天记录:\n{chat_text}",
                system_prompt=system_prompt,
            )
            if resp and resp.completion_text:
                return resp.completion_text.strip()
            return None
        except Exception as e:
            logger.error(f"AI 分析调用失败: {e}")
            return None

    async def _send_result(
        self,
        target: str,
        report: str,
        analysis_text: str = "",
        flagged_pairs: Optional[List[Tuple[str, str, "_ChatRecord"]]] = None,
    ):
        if not target:
            logger.info("未配置报告发送目标，分析结果无法发送。")
            return
        try:
            await self.context.send_message(
                target,
                MessageChain(chain=[Plain(report)]),
            )
            logger.info(f"分析报告已发送至 {target}")
            if flagged_pairs:
                await self._send_violation_forward(target, analysis_text, flagged_pairs)
        except Exception as e:
            logger.error(f"发送分析报告失败: {e}")

    async def _send_violation_forward(
        self,
        target: str,
        analysis_text: str,
        pairs: List[Tuple[str, str, "_ChatRecord"]],
    ):
        try:
            group_id = target.split(":")[-1] if ":" in target else target
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                client = platform.get_client()
                if not hasattr(client, "call_action"):
                    continue

                nodes = []
                if analysis_text:
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": self._bot_id or "",
                            "nickname": "AI 分析审核",
                            "id": "",
                            "content": [
                                {"type": "text", "data": {"text": analysis_text}}
                            ],
                        },
                    })

                pair_idx = 0
                for level, reason, record in pairs:
                    pair_idx += 1
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": self._bot_id or "",
                            "nickname": f"{level}原因 #{pair_idx}",
                            "id": "",
                            "content": [
                                {"type": "text", "data": {"text": reason}}
                            ],
                        },
                    })
                    uid = record.sender_id or record.sender
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": uid,
                            "nickname": record.sender,
                            "id": "",
                            "content": [
                                {"type": "text", "data": {"text": record.content}}
                            ],
                        },
                    })

                if not nodes:
                    return

                forward_msg = {"group_id": group_id, "messages": nodes}
                await client.api.call_action("send_forward_msg", **forward_msg)
                logger.info(
                    f"定位消息转发已发送至 {target}，共 {len(pairs)} 组"
                )
                return
        except Exception as e:
            logger.error(f"发送定位消息转发失败: {e}")


class _ChatRecord:
    def __init__(
        self,
        session_id: str,
        sender: str,
        content: str,
        timestamp: float,
        is_group: bool = False,
        group_id: str = "",
        sender_id: str = "",
    ):
        self.session_id = session_id
        self.sender = sender
        self.sender_id = sender_id
        self.content = content
        self.timestamp = timestamp
        self.is_group = is_group
        self.group_id = group_id

    def format(self, index: int = 0) -> str:
        time_str = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        location = (
            f"[群:{self.group_id}]" if self.is_group and self.group_id else "[私聊]"
        )
        prefix = f"[#{index}] " if index > 0 else ""
        return f"{prefix}[{time_str}]{location} {self.sender}: {self.content}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "sender": self.sender,
            "sender_id": self.sender_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "is_group": self.is_group,
            "group_id": self.group_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_ChatRecord":
        return cls(
            session_id=d.get("session_id", ""),
            sender=d.get("sender", ""),
            sender_id=d.get("sender_id", ""),
            content=d.get("content", ""),
            timestamp=d.get("timestamp", 0.0),
            is_group=d.get("is_group", False),
            group_id=d.get("group_id", ""),
        )


def _format_time_range(records: List[_ChatRecord]) -> str:
    if not records:
        return "N/A"
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(records[0].timestamp))
    end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(records[-1].timestamp))
    return f"{start} ~ {end}"


def _extract_session_info(records: List[_ChatRecord]) -> str:
    groups: set[str] = set()
    has_private = False
    for r in records:
        if r.is_group and r.group_id:
            groups.add(r.group_id)
        else:
            has_private = True
    parts: list[str] = []
    if groups:
        parts.append(f"群聊: {', '.join(sorted(groups))}")
    if has_private:
        parts.append("私聊")
    return "; ".join(parts) if parts else "未知"
