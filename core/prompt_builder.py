from typing import List, Tuple

from .constant import BUILTIN_PROMPTS, PRESET_LABELS
from .models import GroupConfig


def resolve_group_prompts(gc: GroupConfig) -> List[Tuple[str, str]]:
    prompts: List[Tuple[str, str]] = []
    for key in gc.presets:
        if key == "custom":
            prompts.append(("custom", gc.custom_prompt or BUILTIN_PROMPTS["default"]))
        elif key in BUILTIN_PROMPTS:
            prompts.append((key, BUILTIN_PROMPTS[key]))
    return prompts if prompts else [("default", BUILTIN_PROMPTS["default"])]


def build_combined_prompt(
    prompts: List[Tuple[str, str]],
    skip_silent: bool = True,
    group_rules: str = "",
    action_mode: str = "suggest",
) -> str:
    lines: List[str] = []
    lines.append("你正在审查一批群聊消息，请严格以\"群管理/内容审核\"视角输出。")
    lines.append("输入中的每条消息都带有 [#N] 编号，编号是唯一定位依据。")
    lines.append("消息内容可能是从图文消息中提取出的纯文本；未提供的图片信息一律视为未知，不得脑补。")
    lines.append("请只根据已给出的聊天内容下结论，避免过度解读。")
    lines.append("")
    lines.append("重要：聊天记录中标记为 [管理员] 的成员是该群的群管理员，请特别注意：")
    lines.append("  1. 对 [管理员] 成员，无论发言存在什么问题，都不要列入【处置建议】。")
    lines.append("  2. 对 [管理员] 成员的异常发言，请在【定位清单】中正常列出。")
    lines.append("  3. 对 [管理员] 成员的每次异常发言，请使用【管理员提醒】章节输出一条提醒，")
    lines.append("     格式：提醒 #N 提醒内容")
    lines.append("  4. 提醒内容要求：友善、自然、克制，像同事之间的善意提醒。")
    lines.append("     示例：提醒 #2 发言时请注意措辞，保持友善氛围有助于群管理")
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
    lines.append("4. 不得输出任何联系方式、账号、二维码、链接、身份证号、手机号、地址、银行卡等敏感信息；如果原文出现，只能概括为\"联系方式\"或\"隐私信息\"。")
    lines.append("5. 不得提供规避审核、逃避风控、违法操作、攻击他人或扩大传播的建议。")
    lines.append("6. 不要直接评判用户人格，只描述可观察到的发言风险和群管理影响。")
    lines.append("7. 不得在任何位置出现引号内容、括号补充、原词示例、具体题材词、具体癖好词、具体暴力方式、具体辱骂词、具体群体标签。")
    lines.append("8. 遇到敏感话题时，一律改写为抽象类别，如\"话题尺度风险\"\"表达冲突风险\"\"内容边界风险\"\"群秩序风险\"\"需人工复核的高风险表达\"。")
    lines.append("")
    lines.append("判定原则：")
    lines.append("1. 普通闲聊、玩笑、轻微吐槽，如果未明显影响群秩序，不要上升为违规。")
    lines.append("2. 只有当某条消息确实值得管理员关注或处理时，才将其列入定位清单。")
    lines.append("3. 对明显异常或高风险内容，不要直接下最终定性，统一使用\"复核 #N 原因\"的格式逐条列出。")
    lines.append("4. 同一条消息如涉及多项问题，请合并成一条原因，保持简洁。")
    lines.append("5. 如果只是整体气氛一般、轻微偏题、活跃度异常，但没有明确问题，可在正文说明，不要伪造违规编号。")
    lines.append("")
    lines.append("请综合以下分析维度：")
    for key, text in prompts:
        label = PRESET_LABELS.get(key, key)
        lines.append(f"【{label}】")
        lines.append(text)
        lines.append("")
    if skip_silent:
        lines.append(
            "静默规则：如果本轮聊天整体正常，没有任何值得管理员关注的问题，"
            "也没有需要点名定位的消息，请在【总体结论】输出“正常”，"
            "【定位清单】和【处置建议】均输出“无”。"
        )
    lines.append("输出要求：")
    lines.append("1. 必须使用中文。")
    lines.append("2. 除非触发静默规则，否则严格按下面模板输出，不要省略标题，不要改标题名称。")
    lines.append("3. 每个部分尽量简洁，优先写管理员真正需要看的信息。")
    lines.append("4. 不得在摘要、风险与依据、处理建议、定位清单中粘贴原始违规句子；只允许做安全概括。")
    lines.append("5. 定位清单中的原因必须是短句标签，长度尽量控制在 8-20 个字，不要展开复述聊天原文。")
    lines.append("6. 【风险与依据】只能写抽象风险类别，不得写具体讨论内容，不得写消息原词，不得写\"某类题材词/某句原话/某种细节\"。")
    lines.append("7. 【处理建议】只能写管理动作，如提醒、观察、收敛话题、人工复核，不得重提具体风险内容。")
    lines.append("8. 【定位清单】只能写抽象标签，如\"话题尺度风险\"\"表达冲突风险\"\"群秩序风险\"\"需人工复核\"。")
    lines.append("")
    lines.append("【总体结论】")
    lines.append("填写\"正常 / 需关注 / 建议复核\"三选一。不要使用\"存在违规\"等直接定性表述。")
    lines.append("【摘要】")
    lines.append("用 2-4 句概括当前话题、氛围和最值得关注的点。")
    lines.append("【风险与依据】")
    lines.append("按要点列出抽象风险判断；没有明确风险时写\"未发现明确风险\"。")
    lines.append("【处理建议】")
    lines.append("给出简短建议；无需处理时写\"建议继续观察\"。")
    lines.append("【定位清单】")
    lines.append("若总体结论为\"需关注\"，每行一个：关注 #N 具体原因")
    lines.append("若总体结论为\"建议复核\"，每行一个：复核 #N 具体原因")
    lines.append("只有总体结论为\"正常\"时，才允许写\"无\"")
    lines.append("")
    lines.append("【管理员提醒】")
    lines.append("针对标记为 [管理员] 的成员每次异常发言，每行一条：提醒 #N 友善提醒内容。")
    lines.append("没有需要提醒的管理员时写\"无\"。")
    lines.append("")
    if action_mode in ("confirm", "auto"):
        lines.append("【处置建议】")
        lines.append("你拥有群管理操作权限，请根据分析结果建议具体的处置操作。")
        lines.append("")
        lines.append("【处置建议】使用规则（极其重要）：")
        lines.append("  1. 操作类型严格限定为 禁言、移除、拉黑、清昵、撤回 五个词，不得使用提醒、警告、at、全体、群公告等任何其他词。")
        lines.append("  2. 每行一条，格式为：{操作类型} #N [秒数] 简短原因 | 群内通知")
        lines.append("  3. 只列出需要对\"具体成员\"执行 QQ 平台操作（禁言/移除/拉黑/清昵/撤回）的项目。")
        lines.append("  4. 如果某个问题只需全员文字提醒、不需要对任何人执行禁言/移除/拉黑/清昵/撤回，请写入【处理建议】而非【处置建议】。")
        lines.append("  5. 没有需要实质性处置的成员时，【处置建议】只写一个\"无\"。")
        lines.append("")
        lines.append("竖线后的”群内通知“是 Bot 在群内执行处置时同步发送的消息，要求：")
        lines.append("  1. 有人味、自然、不僵硬，像管理员在说话。")
        lines.append("  2. 不能复述原始违规内容，不能出现脏话、色情、广告原词。")
        lines.append("  3. 简短有力，一般 10-30 字。")
        lines.append("  4. 语气与违规程度匹配：轻微用轻松提醒，中等用严肃警告，严重用正式宣告。")
        lines.append("  5. 通知中不要使用 @某人 语法，Bot 会自动补充。")
        lines.append("")
        lines.append("各操作格式说明：")
        lines.append("  禁言：禁言 #N 秒数 原因 | 通知，秒数根据违规程度：轻微=300-600，中等=600-1800，严重=1800-7200。")
        lines.append("  移除：移除 #N 原因 | 通知。用于广告、严重违规等需移出群聊的场景。")
        lines.append("  拉黑：拉黑 #N 原因 | 通知。用于极严重违规或屡教不改，移除并拉黑禁止再次加群。")
        lines.append("  清昵：清昵 #N 原因 | 通知。用于群昵称含广告、违规词等需清空的场景。")
        lines.append("  撤回：撤回 #N 原因 | 通知。用于单条消息内容违规但无需禁言等处置的场景。")
        lines.append("")
        lines.append("重要：撤回与其他动作的关系：")
        lines.append("  1. 撤回是一个独立动作，不会随禁言/移除/拉黑自动附带。")
        lines.append("  2. 如果某条消息需要撤回且该成员还需要禁言/移除/拉黑，请同时输出 撤回 #N 和 禁言 #N（或其他动作）两行。")
        lines.append("  3. 如果只需撤回消息而不需要其他处置，只输出 撤回 #N 一行即可。")
        lines.append("")
        lines.append("处置建议示例：")
        lines.append("禁言 #3 600 刷屏影响阅读体验 | 别刷屏啦，休息一下再聊～")
        lines.append("移除 #7 广告导流需清理 | 本群禁止广告推广，请遵守群规")
        lines.append("拉黑 #12 多次严重违规需移除并拉黑 | 多次违规，已移出并拉黑")
        lines.append("清昵 #5 群昵称含广告信息 | 群昵称含违规信息，已清空请重新设置")
        lines.append("撤回 #8 消息内容不当需撤回 | 该消息违规，已撤回")
        lines.append("撤回 #2 消息违规需撤回 | 该消息已撤回")
        lines.append("禁言 #2 600 违规需警示 | 请注意发言规范")
        lines.append("无")
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
