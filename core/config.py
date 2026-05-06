from typing import Dict, List

from astrbot.api import logger

from .constant import BUILTIN_PROMPTS
from .models import GroupConfig


def _split_ids(raw_ids: str) -> List[str]:
    return [
        gid.strip()
        for gid in raw_ids.replace("\uff0c", ",").split(",")
        if gid.strip()
    ]


def parse_group_configs(config: dict) -> Dict[str, GroupConfig]:
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
                for p in str(preset_val).replace("\uff0c", ",").split(",")
                if p.strip()
            ]
        valid_presets = [
            p for p in preset_candidates if p in BUILTIN_PROMPTS or p == "custom"
        ]
        if not valid_presets:
            valid_presets = ["default"]
        keyword_raw = str(item.get("trigger_keywords", "")).strip()
        keywords = [
            kw.strip()
            for kw in keyword_raw.replace("\uff0c", ",").split(",")
            if kw.strip()
        ]
        action_mode = str(item.get("action_mode", "suggest")).strip()
        if action_mode not in ("suggest", "confirm", "auto"):
            action_mode = "suggest"
        admin_raw = str(item.get("admin_ids", "")).strip()
        strategy_admins = _split_ids(admin_raw)
        shared_config = dict(
            presets=valid_presets,
            custom_prompt=str(item.get("custom_prompt", "")).strip(),
            action_mode=action_mode,
            group_rules=str(item.get("group_rules", "")).strip(),
            trigger_keywords=keywords,
            target_session=str(item.get("target_session", "")).strip(),
            admin_ids=strategy_admins,
        )
        for gid in group_ids:
            if gid in result:
                logger.warning(
                    f"群 {gid} 已在其他策略组中配置，跳过重复添加。"
                    "每个群只能加入一个策略组。"
                )
                continue
            result[gid] = GroupConfig(group_id=gid, **shared_config)
    return result
