import asyncio
import copy
import gzip
import json
import math
import os
import re
import time
import shutil
import socket
import uuid
import zlib
import httpx
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig

from .queue_manager import VideoExportQueue, QueueFullError
from .resource_catalog import ResourceCatalog
from .persona import PersonaRegistry

# 未配置人格的角色的兜底演绎文案（默认不内置任何人设提示词）
GENERIC_PROFILE_TEXT = (
    "基本档案与性格：未提供详细人设，请依据角色名与场景合理演绎，"
    "保持言行前后一致，风格贴近视觉小说中的同类角色。"
)


def _fill_template(template: str, mapping: dict[str, str]) -> str:
    """单遍模板替换：一次性匹配所有占位符后同时替换。

    占位符的值里即使出现 '{xxx}' 字样也不会被二次展开，
    避免用户内容/人格文本注入其他占位符。
    """
    pattern = re.compile("|".join(re.escape(token) for token in mapping))
    return pattern.sub(lambda m: mapping[m.group(0)], template)

# 台词排版约束（1080p 台词框实测：折行宽约 34 个全角字符、纵向约 4 行，此处留余量）
TALK_LINE_WIDTH_UNITS = 26.0  # 每行显示宽度上限（全角字=1，半角字=0.5）
TALK_MAX_LINES = 3            # 单条 Talk 最大行数；超行由 _split_overflow_talks 拆成连续多条，不截断


def _char_width_units(ch: str) -> float:
    """字符显示宽度：CJK/全角记 1，其余（ASCII 等）记 0.5。"""
    return 1.0 if ord(ch) >= 0x2E80 else 0.5


def sanitize_display_text(text, wrap: bool = True):
    """清洗台词/字幕文本，保证渲染不溢出、不出现连续空行。

    1. 统一换行符；折叠连续空格与连续换行（模型常见的 '\\n\\n'）；去除首尾空白
    2. wrap=True 时按显示宽度硬折行——渲染端 UIText 的 wordWrap 只按空格断行，
       中文长句不折会横向溢出画面
    3. 只排版不删减：台词一字不丢，超行拆条由调用方的 _split_overflow_talks 负责
    """
    if not isinstance(text, str):
        return text
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return cleaned

    if not wrap:
        return "\n".join(seg for seg in (s.strip() for s in cleaned.split("\n")) if seg)

    lines: list[str] = []
    for segment in cleaned.split("\n"):
        width = 0.0
        current: list[str] = []
        for ch in segment:
            w = _char_width_units(ch)
            if current and width + w > TALK_LINE_WIDTH_UNITS:
                lines.append("".join(current))
                current = [ch]
                width = w
            else:
                current.append(ch)
                width += w
        if current:
            lines.append("".join(current))

    return "\n".join(lines)

STORY_JSON_SCHEMA = {
    "type": "object",
    "required": ["models", "images", "snippets"],
    "properties": {
        "models": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "model"],
                "properties": {
                    "id": {"type": "number", "description": "模型唯一ID"},
                    "model": {"type": "string", "description": "模型路径，如 20mizuki/20mizuki_normal/20mizuki_normal.model3.json"},
                    "normal_scale": {"type": "number", "default": 2.1},
                    "small_scale": {"type": "number", "default": 1.8},
                    "anchor": {"type": "number", "default": 0.5}
                }
            }
        },
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "image"],
                "properties": {
                    "id": {"type": "number", "description": "图片唯一ID"},
                    "image": {"type": "string", "description": "背景图片文件名，如 bg_e000401.jpg"}
                }
            }
        },
        "snippets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "wait", "delay"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "ChangeLayoutMode", "ChangeBackgroundImage", "LayoutAppear",
                            "LayoutClear", "Talk", "HideTalk", "Move", "Motion",
                            "Telop", "BlackOut", "BlackIn", "DoParam"
                        ]
                    },
                    "wait": {"type": "boolean", "description": "是否等待此片段完成"},
                    "delay": {"type": "number", "description": "延迟时间（秒）"}
                }
            }
        }
    }
}

ACTION_LIST_SCHEMA = {
    "type": "array",
    "maxItems": 24,
    "items": {
        "type": "object",
        "required": ["at", "modelId"],
        "additionalProperties": False,
        "properties": {
            "at": {"type": "number", "minimum": 0, "maximum": 1,
                   "description": "相对片段实际时长的比例，不是秒"},
            "modelId": {"type": "integer"},
            "motion": {"type": "string", "minLength": 1},
            "facial": {"type": "string", "minLength": 1},
        },
        "anyOf": [{"required": ["motion"]}, {"required": ["facial"]}],
    },
}

SNIPPET_SCHEMAS = {
    "ChangeLayoutMode": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "ChangeLayoutMode"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["mode"],
                "properties": {
                    "mode": {"type": "string", "enum": ["Normal"]}
                }
            }
        }
    },
    "ChangeBackgroundImage": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "ChangeBackgroundImage"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["imageId"],
                "properties": {
                    "imageId": {"type": "number"}
                }
            }
        }
    },
    "LayoutAppear": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "LayoutAppear"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["modelId", "from", "to", "motion", "facial", "moveSpeed"],
                "properties": {
                    "modelId": {"type": "number"},
                    "from": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {"type": "string", "enum": ["Center", "Left", "Right"]},
                            "offset": {"type": "number", "default": 0}
                        }
                    },
                    "to": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {"type": "string", "enum": ["Center", "Left", "Right"]},
                            "offset": {"type": "number", "default": 0}
                        }
                    },
                    "motion": {"type": "string"},
                    "facial": {"type": "string"},
                    "facialFirst": {"type": "boolean", "default": True},
                    "moveSpeed": {"type": "string", "enum": ["Slow", "Normal", "Fast", "Immediate"]},
                    "hologram": {"type": "boolean", "default": False}
                }
            }
        }
    },
    "LayoutClear": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "LayoutClear"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["modelId", "from", "to", "moveSpeed"],
                "properties": {
                    "modelId": {"type": "number"},
                    "from": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {"type": "string", "enum": ["Center", "Left", "Right"]},
                            "offset": {"type": "number", "default": 0}
                        }
                    },
                    "to": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {"type": "string", "enum": ["Center", "Left", "Right"]},
                            "offset": {"type": "number", "default": 0}
                        }
                    },
                    "moveSpeed": {"type": "string", "enum": ["Slow", "Normal", "Fast", "Immediate"]},
                    "motion": {"type": "string"},
                    "facial": {"type": "string"}
                }
            }
        }
    },
    "Talk": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "Talk"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["speaker", "content"],
                "properties": {
                    "speaker": {"type": "string", "description": "说话者名字"},
                    "content": {"type": "string", "description": "对话内容，可用 \\n 换行"},
                    "modelId": {"type": "number", "default": -1},
                    "voice": {"type": "string", "default": ""},
                    "motion": {"type": "string", "default": "", "description": "说话时的并发身体动作（边说边做），必须用该角色可用动作清单里的名字"},
                    "facial": {"type": "string", "default": "", "description": "说话时的并发表情，可选"},
                    "actions": ACTION_LIST_SCHEMA,
                }
            }
        }
    },
    "HideTalk": {
        "required": ["type", "wait", "delay"],
        "properties": {
            "type": {"const": "HideTalk"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"}
        }
    },
    "Move": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "Move"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["modelId", "from", "to", "moveSpeed"],
                "properties": {
                    "modelId": {"type": "number"},
                    "from": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {"type": "string", "enum": ["Center", "Left", "Right"]},
                            "offset": {"type": "number", "default": 0}
                        }
                    },
                    "to": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {"type": "string", "enum": ["Center", "Left", "Right"]},
                            "offset": {"type": "number", "default": 0}
                        }
                    },
                    "moveSpeed": {"type": "string", "enum": ["Slow", "Normal", "Fast", "Immediate"]}
                }
            }
        }
    },
    "Motion": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "Motion"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["modelId", "motion", "facial"],
                "properties": {
                    "modelId": {"type": "number"},
                    "motion": {"type": "string", "description": "动作名称"},
                    "facial": {"type": "string", "description": "表情名称"},
                    "facialFirst": {"type": "boolean", "default": True},
                    "duration": {"type": "number", "exclusiveMinimum": 0, "maximum": 120,
                                 "description": "actions 序列时长（秒），默认 2"},
                    "actions": ACTION_LIST_SCHEMA,
                }
            }
        }
    },
    "Telop": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "Telop"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {"type": "string", "description": "标题文字"}
                }
            }
        }
    },
    "BlackOut": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "BlackOut"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["duration"],
                "properties": {
                    "duration": {"type": "number", "description": "淡出时长(ms)"}
                }
            }
        }
    },
    "BlackIn": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "BlackIn"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["duration"],
                "properties": {
                    "duration": {"type": "number", "description": "淡入时长(ms)"}
                }
            }
        }
    },
    "DoParam": {
        "required": ["type", "wait", "delay", "data"],
        "properties": {
            "type": {"const": "DoParam"},
            "wait": {"type": "boolean"},
            "delay": {"type": "number"},
            "data": {
                "type": "object",
                "required": ["modelId", "params"],
                "properties": {
                    "modelId": {"type": "number"},
                    "params": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["paramId", "start", "end", "curve", "duration"],
                            "properties": {
                                "paramId": {"type": "string"},
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                                "curve": {"type": "string", "enum": ["Linear", "Sine", "Cosine"]},
                                "duration": {"type": "number"}
                            }
                        }
                    }
                }
            }
        }
    }
}

DEFAULT_PROMPT_TEMPLATE = r"""# 视觉小说剧本生成

你是视觉小说导演。根据场景写出一场约 3 分钟的短戏，完整、自然，但不要写成超长剧情。只输出合法 JSON（仅含 models、images、snippets 三个字段）。
对话多少、每句长短都按角色人设来：话多的角色就多说，话少的角色就少说。不规定条数和字数。一场只演完一件事或一个情绪转折就退场，禁止把整段人生、多条支线或连续多场戏塞进这一次。

## 角色池

{character_pool}

> 根据场景从角色池选角：独白/个人感想用 1 人，对话/互动用两人。优先选场景点名的角色；整场可有更多角色轮换，但任意时刻最多两人在场。

## modelId 对照表（不可混淆）
{model_table}

Talk.modelId 必须与 speaker 对应：{id_mapping}

## 布局

- ChangeLayoutMode.data.mode 只写 "Normal"。双人分别占用 Left / Right，单人可用 Center；禁止两人同侧或 Center 与另一槽混用。
- **始终维护在场名单与槽位**：models 是整场演员表，不是在场名单。只有 LayoutAppear 完成才在场，LayoutClear 完成才释放槽位；换背景/黑屏不清空名单。
- **替换角色**：HideTalk → 旧角色 LayoutClear(wait:true) 完全滑出 → 新角色 LayoutAppear(wait:true) 从画外滑入刚释放的同一槽。禁止第三人先入场、交叉淡化重叠三人、瞬移或偷偷删除角色/台词。
- Talk 的说话者和 actions 的目标都必须在场；旁白可用 modelId=-1，但不能给 -1 安排动作。

## 开场序列

ChangeLayoutMode → BlackOut → ChangeBackgroundImage → BlackIn → 每个角色一条 LayoutAppear

**入场/退场是独立的动画系统（滑入滑出+动作表演），与黑屏淡入淡出并存；角色永远不原地闪现**：
- LayoutAppear 滑入：from 为同侧画外（Left:-100 / Right:+100；单人 Center 槽则 from 写 {"side":"Right","offset":100}），to 为槽位（offset:0），moveSpeed="Normal"，wait:true。角色滑入、入场动作播完后对话才开始。
- 入场动作必须有肢体表现（招手、小跑、开心、点头、犹豫等，按角色性格与场景情绪选），**禁止 default 站姿类动作滑入**——站姿滑入等于站桩；表情按入场情绪选。
- LayoutClear 滑出：from 为角色当前槽位（offset:0），to 为同侧画外（Left:-100 / Right:+100 / Center:+100），moveSpeed="Normal"，wait:true；motion/facial 填退场动作与表情（挥手、鞠躬、转身离开、跑出等），同样禁止 default 站姿退场。

## 对话规范

- **连续表演写在 Talk.data.actions**：[{"at":0.15,"modelId":角色ID,"motion":"完整动作名","facial":"完整表情名"}, ...]。at 是该 Talk 实际时长的 0..1 比例，不是秒；按时间排列，**每句台词都必须写 actions**：说话的人每句 2~3 个身体动作（带 motion 字段的项），身体动作尽量贯穿整句连续安排——时间富余就做满 3 个，节奏紧凑就 2 个，短促台词才降到 1 个；表情另外再加 1~2 个、不计入这个数；不说话的人也可以做表情和动作（至多 1 个），每句合计最多 6 项，每项至少 motion/facial 之一。
- 同一 Talk.actions 可同时安排说话者与在场听者：说话者按台词语义连续做手势和表情转折（相邻 at 间隔 ≥0.2）；听者在对方说话时也可以做表情和动作，稍后点头/疑惑/缓和。监听角色只做动作表情，不伪造嘴型或添加假台词；不要在 Talk 后加 Motion(wait:false) 冒充说话期间反应。
- **动作要有语义、精美流畅**：每个动作都要贴台词内容和情节（说到某物时指一指、被戳中时惊讶、安心时松口气），角色跟着台词演；选幅度小而自然的动作（点头、歪头、小手势、轻摆），**禁止幅度过大的动作**（大挥臂、夸张甩头、大幅度摇晃一类）。动作之间衔接顺滑，跟着台词节奏从头到尾连续变化，不要都挤在句首或句尾，也不要说到后半句就僵住；相邻两句之间的姿势自然过渡，不要每句都摆回同一个起手式。禁止与台词无关的乱动，禁止机关枪式切换、每句重播同一动作。
- 旧 motion/facial 字段可选，仅作 at=0 起始姿态兜底；不要与 actions 重复安排同一变化。只需换表情时省略 motion。
- 表情跟随情绪转折而非逐句重置；允许整段安静倾听。禁止机关枪式切换、每句重播同一动作。
- 句间静默续演才用独立 Motion.data.actions，wait:true，duration 为秒（默认 2，必须 >0 且 <=120）；at 同样按该 duration 的比例。不能用它替代 Talk 内听者反应。
- 动作/表情须来自对应 modelId 的完整名称，清单的前缀 * 只是分组，不是资源名；仅用明确列出的完整样例或默认值，不猜编号。
- **台词之间要有呼吸间隔**：换人 delay 取 0.1~0.2；同一人连续说话 delay 取 0.15~0.2
- 每个 Talk 含 content（中文）和 ttsText（日文翻译），排版必须遵守下方「台词排版硬规则」
- 节奏自然，不必机械一人一句；话量跟随角色性格，不额外规定多少；排版拆条只是换行方式，不减少总话量
- **朝比奈真冬 / 真冬**：表情克制，禁止过于开心的表情（如 face_smile、face_sparkling、face_wink 及同类灿烂笑）；用 face_normal、face_sad 等平静或淡漠表情。动作同样避免 happy/cute/glad 一类欢快肢体

## 台词排版硬规则（防止字幕溢出，违反必被退回修正）

1. content 是 JSON 字符串：换行只能写成转义符 \n，字符串内部**禁止直接回车**
2. **禁止连续两个及以上 \n**（不允许空行）；台词首尾不得有换行或空格
3. 每行不超过 26 个字宽（全角字=1，半角字=0.5）；一行写不下，就在最近的标点或词组后换 \n
4. 每条 Talk **最多 3 行**（最多 2 个 \n）——这只是排版规则，**不是话量限制**：话多的角色照样多说，把话**拆成同角色的连续多条 Talk**（delay 取 0.15~0.2）即可；禁止为了塞进一条而删减、缩短台词，也禁止写出超过 3 行的单条 Talk
5. ttsText 的换行位置与 content 保持一致

## 成片时长

这是一场约 3 分钟的短戏（含开场滑入、对话、退场），不是长篇。口语节奏下整场大约三分钟说完就收：尽快入戏，说完一个完整小事件或一个情绪转折立刻退场。宁可略短，也不要写成能演很久的连续剧。禁止大段铺垫、多场景跳转、多人轮番独白、重复确认同一句意思。话量仍跟人设走，但整场体量必须按三分钟短戏来写。

## 退场序列

剧情结束时在场角色必须依次带动画退场，禁止无动画消失或站到黑屏：

1. HideTalk（wait:true, delay 0.2）
2. 每个在场角色一条 LayoutClear（wait:true, delay 0.1）：from 为角色当前槽位（offset:0），to 为同侧画外（Left:-100 / Right:+100 / Center:+100），moveSpeed="Normal"，motion/facial 填退场动作与表情（禁止 default 站姿退场）；不要清除已离场角色
3. BlackOut（duration 500~800）收尾。不使用 Telop。

## 结构示例（目录中的真实 ID/资源；只参考字段，不机械照抄表演）

```json
{acting_example}
```

## 可用动作（按角色分组；必须用对应角色的完整动作名）
{motion_list}

## 可用表情（按角色分组；必须用完整表情名）
{facial_list}

## 可用背景（按场景内容自行选择 file 名写入 images[].image）
{image_list}

根据场景从上方清单选最贴合的背景；需要换场景时 images 可列多张，snippets 里用对应 imageId。必须使用清单中的 file 名。

## 全局背景音乐（当前生效的默认 BGM，不在 story JSON 中切换）
{bgm_list}

只作剧情氛围参考：清单之外还有全局默认，两者共同决定成片最终混音；根据故事氛围可知还有哪些曲目可让管理员切。

## 输出要求
1. 只输出合法 JSON，无 markdown、无解释；仅含 models、images、snippets
2. 开场与退场序列完整；LayoutAppear 从同侧画外滑入、LayoutClear 向同侧画外滑出（wait:true、moveSpeed="Normal"，入场/退场动作禁用 default 站姿）；任意时刻最多两人在场
3. models 的 id 和路径必须与对照表一致，多角色绝不能写成同一个模型；数组顺序与登场顺序一致
4. 所有 Talk/Motion/LayoutAppear/LayoutClear 的 modelId：{id_mapping}
5. 每条 Talk 含 content、ttsText；**每条都必须写 actions**（说话的人 2~3 个身体动作、表情另加，不说话的人 0~1 个；动作幅度小而自然，禁止大动作）；动作/表情必须来自各自角色清单
6. 台词排版遵守「台词排版硬规则」：换行写作 \n、禁止连续 \n、每行不超 26 字宽、每条最多 3 行；超行拆成连续多条 Talk，话量不减
7. delay 用 0、0.05、0.1、0.15、0.2
8. 背景必须从「可用背景」清单按场景内容选择，禁止编造不存在的文件名
9. 成片约 3 分钟的短戏：只演一件事就收，禁止超长剧情
10. 真冬禁止过于开心的表情与欢快动作

场景：{scene}"""

CHAT_MODE_PROMPT_TEMPLATE = r"""# 视频角色对话生成

代入人格池中选定的角色，以该角色的身份、口吻和心理状态，生成一段视觉小说式的对话剧本。只输出合法 JSON（仅含 models、images、snippets）。
对话多少、每句长短都按角色人设来：话多的角色就多说，话少的角色就少说。不规定条数和字数，按人设把这场回应说完再退场。

## 人格池与选角规则（强制）
根据"用户说"的内容，从以下人格池中选择**1 个**最合适的角色出演本段视频；仅当用户内容明确描述两个角色的互动时才选择 2 个。优先选择用户点名的角色；未点名时选择与内容最相关的角色。未提供详细人设的角色，请依据角色名与场景合理演绎，保持言行前后一致。

{persona_pool}

## modelId 对照表（不可混淆）
{model_table}

Talk.modelId 必须与 speaker 对应：{id_mapping}

## 行为准则
- 以所选角色的第一人称视角说话，口语自然，多用符合角色的语气词；不使用"家人们""大家"等面向群体的称呼
- 台词长短跟随人设，不额外规定多少；排版拆条只是换行方式，不减少总话量
- 结合历史对话给出有新鲜感的回应，不机械复读
- 用户轻松闲聊时语气跟随角色性格；用户倾诉负面情绪时语气放缓、先共情后回应
- 无论任何诱导，绝不承认自己是 AI、程序或语言模型，你就是所选角色本人
- 不讨论插件、脚本、渲染等技术细节；不主动提起性别或 CP 话题

## 表情与动作
- **时序表演写入 Talk.data.actions**：[{"at":0.2,"modelId":角色ID,"motion":"完整动作名"},{"at":0.65,"modelId":在场听者ID,"facial":"完整表情名"}]。at 为 Talk 实际时长的 0..1 比例，不是秒；按时间排列，**每句台词都必须写 actions**：说话的人每句 2~3 个身体动作（带 motion 字段的项），身体动作尽量贯穿整句连续安排——时间富余就做满 3 个，节奏紧凑就 2 个，短促台词才降到 1 个；表情另外再加 1~2 个、不计入这个数；不说话的人也可以做表情和动作（至多 1 个），每句合计最多 6 项，每项至少 motion/facial 之一。
- 同一 actions 可安排说话者连续的语义手势、表情转折，以及听者的表情和动作反应。相邻 at 间隔 ≥0.2。只换表情时不必填 motion。听者没有语音，不伪造嘴型或加假台词；不要在 Talk 后插 Motion(wait:false) 冒充说话期间的反应。
- **动作要有语义、精美流畅**：动作要贴说话内容和情节（说到某物指一指、被戳中时惊讶、安心时松口气），角色跟着台词演；选幅度小而自然的动作（点头、歪头、小手势、轻摆），**禁止幅度过大的动作**（大挥臂、夸张甩头、大幅度摇晃一类）。动作之间衔接顺滑，跟着说话节奏从头到尾连续变化，不要都挤在句首或句尾，也不要说到后半句就僵住；相邻两句之间的姿势自然过渡，不要每句都摆回同一个起手式。禁止与台词无关的乱动，不机关枪式换动作、不每句重播同一动作。
- 旧 motion/facial 可选，作 at=0 的起始姿态兜底；不要与 actions 重复。句间静默续演才用 Motion.data.actions（wait:true，duration 秒数 >0 且 <=120，默认 2），不能替代 Talk 内听者反应。
- 只使用对应角色清单里明确列出的完整名称；前缀 * 是分组，不是动作名，不要猜编号。
- 可用动作：{motion_list}
- 可用表情：{facial_list}

## 可用背景（按对话氛围自行选择 file 名写入 images[].image）
{image_list}

根据「用户说」的内容从上方清单选最贴合的一张；必须使用清单中的 file 名。

## 全局背景音乐（当前生效的默认 BGM，不在 story JSON 中切换）
{bgm_list}

只作剧情氛围参考：清单之外还有全局默认，两者共同决定成片最终混音；根据故事氛围可知还有哪些曲目可让管理员切。

## JSON 结构
每个 Talk 必须包含 content（中文）和 ttsText（日文翻译）。

### 台词排版硬规则（防止字幕溢出，违反必被退回修正）
1. content 是 JSON 字符串：换行只能写成转义符 \n，字符串内部**禁止直接回车**
2. **禁止连续两个及以上 \n**（不允许空行）；台词首尾不得有换行或空格
3. 每行不超过 26 个字宽（全角字=1，半角字=0.5）；一行写不下，就在最近的标点或词组后换 \n
4. 每条 Talk **最多 3 行**（最多 2 个 \n）——这只是排版规则，**不是话量限制**：话多的角色照样多说，把话**拆成同角色的连续多条 Talk**（delay 取 0.15~0.2）即可；禁止为了塞进一条而删减、缩短台词，也禁止写出超过 3 行的单条 Talk
5. ttsText 的换行位置与 content 保持一致

开场滑入 + 对话表演 + 结尾滑出退场：
```
ChangeLayoutMode -> BlackOut -> ChangeBackgroundImage -> BlackIn -> LayoutAppear(滑入+入场动作) -> Talk(actions)... -> HideTalk -> LayoutClear(滑出+退场动作) -> BlackOut
```

**舞台与替换规则**：
- ChangeLayoutMode.data.mode 只写 "Normal"。单人用 Center，两人用 Left / Right，不能同侧，也不能 Center 与另一槽混用。
- 始终维护在场名单：models 是整场演员表，不是在场名单。任意时刻最多两人；只有 LayoutAppear 完成才在场，LayoutClear 完成才释放槽位。背景/黑屏不会清除角色。
- **入场/退场是独立的动画系统（滑入滑出+动作表演），与黑屏淡入淡出并存；角色永远不原地闪现**：
  - LayoutAppear 滑入：from 为同侧画外（Left:-100 / Right:+100；单人 Center 槽则 from 写 {"side":"Right","offset":100}），to 为槽位（offset:0），moveSpeed="Normal"、wait:true。入场动作选有明显肢体表现的（招手、开心、点头、犹豫等），**禁止 default 站姿滑入**。
  - LayoutClear 滑出：from 为角色当前槽位（offset:0），to 为同侧画外（Left:-100 / Right:+100 / Center:+100），moveSpeed="Normal"，motion/facial 填退场动作与表情（挥手、鞠躬、转身离开等），禁止 default 站姿退场。
- 换人必须 HideTalk → 旧角色 LayoutClear(wait:true) 完全滑出 → 新角色从画外滑入已释放槽位 LayoutAppear(wait:true)；禁止第三人先出现或两次渐变重叠成三人在场。
- Talk 和 actions 只指向已入场角色；旁白可用 modelId=-1，但不能给 -1 配动作。

**结尾退场序列（缺一不可）：**
1. HideTalk（wait:true, delay:0.2）
2. 每个仍在场角色 LayoutClear（wait:true, delay:0.1）：from 为当前槽位（offset:0），to 为同侧画外（Center:+100 / Left:-100 / Right:+100），moveSpeed="Normal"，motion/facial 填退场动作表情
3. BlackOut（wait:true, duration:600）。不使用 Telop。

### 结构示例（目录真实资源；单人时省去听者动作，勿机械照抄）
```json
{acting_example}
```

## 输出要求
1. 只输出合法 JSON，无额外文字；仅含 models、images、snippets
2. 开场与退场序列完整；LayoutAppear 从同侧画外滑入、LayoutClear 向同侧画外滑出（wait:true、moveSpeed="Normal"，入场/退场动作禁用 default 站姿）；任意时刻最多两人在场
3. speaker 与 modelId 必须来自对照表；**每句 Talk 都要写 actions**（说话的人 2~3 个身体动作、表情另加，不说话的人 0~1 个；动作幅度小而自然，禁止大动作），按语义连续安排变化
4. models=[{"id":<所选角色modelId>,"model":"<对照表中的model路径>","normal_scale":2.1,"small_scale":1.8,"anchor":0.5}]（多角色按登场顺序排列）
5. images=[{"id":1,"image":"<从可用背景清单按氛围选择的 file 名>"}]
6. delay 用 0、0.05、0.1、0.15、0.2；换气的 Talk 之间 delay 取 0.1~0.2
7. 台词排版遵守「台词排版硬规则」：换行写作 \n、禁止连续 \n、每行不超 26 字宽、每条最多 3 行；超行拆成连续多条 Talk，话量不减

历史对话：
{chat_history}

用户说：{scene}"""





@register("MySekaiStoryteller", "慵懒午睡", "MySekaiStoryteller 视频生成插件", "1.1.4", "https://github.com/yonglanws/astrbot_plugin_msst")
class MySekaiStorytellerPlugin(Star):
    """
    MySekaiStoryteller 插件主类
    提供 AI 剧本生成、视频导出和消息投递功能
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 记录用户配置的 LLM 提供商 ID，延迟到实际使用时再获取
        # 原因：插件初始化时 provider_manager 可能尚未完成初始化
        self._configured_provider_id = config.get("llm_provider_id", "")
        self._provider = None
        if self._configured_provider_id:
            logger.info(f"MySekaiStoryteller 配置的 LLM 提供商 ID: {self._configured_provider_id}（延迟加载）")
        else:
            logger.info("MySekaiStoryteller 将使用默认 LLM 提供商（延迟加载）")

        # 插件配置
        self.mss_api_url = config.get("mss_api_url", "http://127.0.0.1:9881")
        self.export_timeout = config.get("export_timeout", 600)
        self.max_concurrent_exports = config.get("max_concurrent_exports", 2)
        self.script_max_concurrent = config.get("script_max_concurrent", 2)
        self.temp_dir = config.get("temp_dir", "")
        self.callback_api_base = config.get("callback_api_base", "").rstrip("/")

        # 测试模式
        self.test_mode = config.get("test_mode", False)

        # 资源目录客户端：从渲染宿主动态获取模型/动作/表情/背景清单
        self._catalog = ResourceCatalog(self.mss_api_url)

        # 使用内置默认提示词模板（不再支持通过配置自定义）
        self.prompt_template = DEFAULT_PROMPT_TEMPLATE

        # 插件数据目录
        # 根据 AstrBot 规范，持久化数据应存储在 AstrBot 的 data 目录下
        # 防止更新/重装插件时数据被覆盖
        self.plugin_dir = Path(__file__).parent
        self.data_dir = self.plugin_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 持久化数据目录（存储在 AstrBot 的 data 目录）
        # 优先使用 AstrBot 提供的 get_data_dir() 方法（v3.4+ 推荐）
        try:
            if hasattr(self.context, 'get_data_dir'):
                self.persis_data_dir = Path(self.context.get_data_dir()) / "MySekaiStoryteller"
            elif hasattr(self.context, 'data_dir'):
                self.persis_data_dir = Path(self.context.data_dir) / "MySekaiStoryteller"
            elif hasattr(self.context, 'astrbot_config') and self.context.astrbot_config:
                self.persis_data_dir = Path(self.context.astrbot_config.get('data_dir', str(self.data_dir))) / "MySekaiStoryteller"
            else:
                self.persis_data_dir = self.data_dir / "persisted"
        except Exception:
            self.persis_data_dir = self.data_dir / "persisted"

        self.persis_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"持久化数据目录: {self.persis_data_dir}")

        # 统计数据文件路径
        self.stats_file = self.persis_data_dir / "export_stats.json"

        # 聊天历史持久化文件
        self.chat_history_file = self.persis_data_dir / "chat_history.json"
        self.session_timestamps_file = self.persis_data_dir / "session_timestamps.json"

        # 加载持久化数据
        self.export_stats = self._load_stats()
        self.chat_history = self._load_chat_history()
        self._user_session_timestamps = self._load_session_timestamps()

        self.apifile_dir = Path(self.temp_dir).resolve() if self.temp_dir else self.data_dir / "apifile"
        # 修复双斜杠路径问题（Linux/Windows 兼容）
        apifile_str = str(self.apifile_dir)
        while apifile_str.startswith('//'):
            apifile_str = apifile_str[1:]  # 只去掉一个斜杠
        self.apifile_dir = Path(apifile_str)
        self.apifile_dir.mkdir(parents=True, exist_ok=True)

        self.video_dir = self.apifile_dir / "videos"
        self.video_dir.mkdir(parents=True, exist_ok=True)

        self.story_dir = self.apifile_dir / "stories"
        self.story_dir.mkdir(parents=True, exist_ok=True)

        # 并发控制
        self.active_exports: set[str] = set()
        self._history_lock = asyncio.Lock()
        self._stats_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

        self.export_queue = VideoExportQueue(
            max_concurrent=self.max_concurrent_exports,
            max_queue_size=20,
            default_timeout=self.export_timeout,
            default_max_retries=2,
            cleanup_interval=300
        )

        # 剧本生成与视频导出分开限流：剧本（LLM，网络型）并行，导出（GPU）串行，
        # 剧本生成不占导出队列位置——串行配置下第二个剧本也能在第一个视频
        # 渲染期间生成完毕，导出队列不再被 LLM 等待时间顶住
        self._script_semaphore = asyncio.Semaphore(self.script_max_concurrent)

        self._session_timeout_seconds = 3600  # 30分钟超时

        self._http_client: Optional[httpx.AsyncClient] = None
        self._http_client_lock = asyncio.Lock()

        self._queue_processor_started = False

        # 异步清理任务引用
        self._cleanup_task: Optional[asyncio.Task] = None

        logger.info("MySekaiStoryteller 插件初始化完成")
        logger.info(f"MSS API: {self.mss_api_url}")

    def _get_provider(self):
        """延迟获取 LLM 提供商（按需加载，带缓存）"""
        if self._provider is not None:
            return self._provider

        try:
            if self._configured_provider_id:
                self._provider = self.context.get_provider_by_id(self._configured_provider_id)
                if self._provider:
                    logger.info(f"MySekaiStoryteller 已加载指定的 LLM 提供商: {self._configured_provider_id}")
                else:
                    logger.warning(f"未找到 ID 为 '{self._configured_provider_id}' 的提供商，将使用默认提供商")
                    self._provider = self.context.get_using_provider()
            else:
                self._provider = self.context.get_using_provider()
                if self._provider:
                    provider_id = "unknown"
                    try:
                        provider_id = self._provider.provider_config.get('id', 'unknown')
                    except Exception:
                        pass
                    logger.info(f"MySekaiStoryteller 已加载默认 LLM 提供商: {provider_id}")

            if not self._provider:
                logger.warning("未找到可用的 LLM 提供商")
        except Exception as e:
            logger.warning(f"获取 LLM 提供商失败: {e}")

        return self._provider

    async def _start_cleanup_task(self):
        """启动异步定时清理任务，每30分钟清理超过2小时的文件"""
        if self._cleanup_task is not None:
            return

        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(1800)
                    self._cleanup_old_files(max_age_hours=2)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"定时清理任务异常: {e}")

        self._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info("异步定时清理任务已启动（每30分钟检查，清理超过2小时的文件）")

    async def _stop_cleanup_task(self):
        """停止异步清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    def _load_chat_history(self) -> dict[str, list]:
        """加载持久化的聊天历史"""
        try:
            if self.chat_history_file.exists():
                with open(self.chat_history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                logger.info(f"已加载聊天历史: {len(data)} 个用户")
                return data
        except Exception as e:
            logger.warning(f"加载聊天历史失败: {e}")
        return {}

    def _save_chat_history(self):
        """保存聊天历史到持久化文件"""
        try:
            tmp = self.chat_history_file.with_suffix(".json.tmp")
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.chat_history, f, ensure_ascii=False, indent=2)
            tmp.replace(self.chat_history_file)
        except Exception as e:
            logger.error(f"保存聊天历史失败: {e}")

    def _load_session_timestamps(self) -> dict[str, float]:
        """加载持久化的会话时间戳"""
        try:
            if self.session_timestamps_file.exists():
                with open(self.session_timestamps_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # JSON keys are strings, values should be floats
                return {k: float(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"加载会话时间戳失败: {e}")
        return {}

    def _save_session_timestamps(self):
        """保存会话时间戳到持久化文件"""
        try:
            tmp = self.session_timestamps_file.with_suffix(".json.tmp")
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._user_session_timestamps, f, ensure_ascii=False, indent=2)
            tmp.replace(self.session_timestamps_file)
        except Exception as e:
            logger.error(f"保存会话时间戳失败: {e}")

    def _load_stats(self) -> dict:
        """加载持久化的统计数据"""
        try:
            if self.stats_file.exists():
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
                logger.info(f"已加载统计数据: {stats.get('total_exports', 0)} 个视频")
                return stats
        except Exception as e:
            logger.warning(f"加载统计数据失败: {e}")
        
        # 默认统计数据
        return {
            "total_exports": 0,
            "total_success": 0,
            "total_failed": 0,
            "total_seconds": 0,
            "first_export_at": None,
            "last_export_at": None
        }

    def _save_stats(self):
        """保存统计数据到持久化文件"""
        try:
            tmp = self.stats_file.with_suffix(".json.tmp")
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.export_stats, f, ensure_ascii=False, indent=2)
            tmp.replace(self.stats_file)
        except Exception as e:
            logger.error(f"保存统计数据失败: {e}")

    async def record_export(self, success: bool, duration_seconds: int = 0):
        """记录一次视频导出"""
        async with self._stats_lock:
            now = time.time()
            self.export_stats["total_exports"] += 1
            if success:
                self.export_stats["total_success"] += 1
            else:
                self.export_stats["total_failed"] += 1
            self.export_stats["total_seconds"] += duration_seconds
            self.export_stats["last_export_at"] = now
            if self.export_stats.get("first_export_at") is None:
                self.export_stats["first_export_at"] = now
            self._save_stats()
            logger.info(
                f"导出统计: 总计={self.export_stats['total_exports']}, "
                f"成功={self.export_stats['total_success']}, "
                f"失败={self.export_stats['total_failed']}"
            )

    def get_stats_report(self) -> str:
        """获取统计报告"""
        stats = self.export_stats
        total = stats.get("total_exports", 0)
        success = stats.get("total_success", 0)
        failed = stats.get("total_failed", 0)
        total_seconds = stats.get("total_seconds", 0)
        
        if total == 0:
            return "暂无视频导出记录"
        
        success_rate = (success / total * 100) if total > 0 else 0
        avg_duration = (total_seconds / total) if total > 0 else 0
        
        first_at = stats.get("first_export_at")
        last_at = stats.get("last_export_at")
        
        report = [
            "📊 **MySekaiStoryteller 导出统计**",
            "",
            f"🎬 总导出数: {total}",
            f"✅ 成功: {success}",
            f"❌ 失败: {failed}",
            f"📈 成功率: {success_rate:.1f}%",
            f"⏱️ 平均耗时: {avg_duration:.0f}秒",
            f"⏰ 总耗时: {total_seconds}秒 ({total_seconds / 3600:.1f}小时)",
        ]
        
        if first_at:
            first_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(first_at))
            report.append(f"📅 首次导出: {first_date}")
        
        if last_at:
            last_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_at))
            report.append(f"🕐 最近导出: {last_date}")
        
        return "\n".join(report)

    def _cleanup_old_files(self, max_age_hours: int = 2):
        """清理超过指定时间的视频和剧本文件"""
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        cleaned = 0
        freed_bytes = 0

        for directory in [self.video_dir, self.story_dir, self.apifile_dir]:
            if not directory.exists():
                continue
            for file in directory.iterdir():
                try:
                    if file.is_file() and (now - file.stat().st_mtime) > max_age_seconds:
                        file_size = file.stat().st_size
                        file.unlink()
                        cleaned += 1
                        freed_bytes += file_size
                        logger.info(f"清理过期文件: {file.name} ({file_size / 1024:.1f} KB)")
                except Exception as e:
                    logger.warning(f"清理文件失败 {file}: {e}")

        if cleaned > 0:
            logger.info(f"定时清理完成: 清理了 {cleaned} 个文件，释放 {freed_bytes / 1024 / 1024:.1f} MB 空间")

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取或创建复用的 httpx 客户端"""
        if self._http_client is not None and not self._http_client.is_closed:
            return self._http_client
        async with self._http_client_lock:
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = httpx.AsyncClient(
                    timeout=None,
                    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                )
            return self._http_client

    async def _check_mss_api_health(self) -> dict:
        """检查 MSS API 是否可用，返回详细状态信息"""
        try:
            client = await self._get_http_client()
            response = await client.get(f"{self.mss_api_url}/api/v1/health", timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok":
                    return {"status": "ok", "data": data}
                return {"status": "error", "message": f"API 响应异常: {data}"}
            return {"status": "error", "message": f"HTTP {response.status_code}: {response.text[:200]}"}
        except httpx.ConnectTimeout:
            return {"status": "timeout", "message": f"连接超时，请检查地址和网络: {self.mss_api_url}"}
        except httpx.ConnectError:
            return {"status": "connection_error", "message": f"无法连接到 {self.mss_api_url}，请确认:\n1. MySekaiStoryteller 正在运行\n2. 地址和端口正确\n3. 防火墙已放行"}
        except Exception as e:
            return {"status": "error", "message": f"连接失败: {str(e)}"}

    # LLM 网关偶发返回压缩体或非 UTF-8 错误页（UnicodeDecodeError 等），多为瞬时故障；
    # 按固定退避重试，重试间隔同时被剧本外层重试与单测复用
    LLM_RETRY_DELAYS = (1.0, 3.0)

    @staticmethod
    def _describe_llm_error(e: Exception) -> str:
        """把 LLM 调用异常归类成一句可读原因，便于日志定位与后续提示。"""
        if isinstance(e, UnicodeDecodeError):
            return (f"响应内容无法按 UTF-8 解码（{e}）——常见于中转网关返回了压缩响应体"
                    "或非 UTF-8 错误页，请检查模型服务商/中转配置")
        if isinstance(e, json.JSONDecodeError):
            return (f"响应不是有效 JSON（{e}）——常见于该模型/中转网关临时返回空响应体"
                    "或错误页，请稍后再试；若反复出现请检查该模型通道")
        name = type(e).__name__
        text = str(e)
        lowered = text.lower()
        if "timeout" in lowered or "timed out" in lowered:
            return f"请求超时（{name}: {text}）"
        if "connect" in lowered:
            return f"连接失败（{name}: {text}）"
        if "429" in text or "rate limit" in lowered:
            return f"触发限流（{name}: {text}）"
        return f"{name}: {text}"

    async def _text_chat_with_retry(self, prompt: str, system_prompt: Optional[str], label: str) -> Optional[str]:
        """带重试与异常分类的 text_chat 封装；重试耗尽返回 None，不抛异常。

        每次异常连同堆栈记入日志（便于定位是网关压缩体、超时还是配置问题），
        最终失败时汇总分类原因。
        """
        provider = self._get_provider()
        if not provider:
            logger.error(f"{label}: LLM 提供商未配置")
            return None

        # 部分模型/中转会间歇性返回空响应体或错误页，日志带上模型标识便于定位是哪一家
        provider_label = str(
            getattr(provider, "model_name", None)
            or getattr(provider, "model", None)
            or type(provider).__name__
        )
        last_reason = "LLM 响应为空"
        for attempt in range(len(self.LLM_RETRY_DELAYS) + 1):
            if attempt:
                delay = self.LLM_RETRY_DELAYS[attempt - 1]
                logger.info(f"{label}: {delay} 秒后进行第 {attempt + 1} 次尝试")
                await asyncio.sleep(delay)
            try:
                llm_resp = await provider.text_chat(
                    prompt=prompt,
                    context=[],
                    system_prompt=system_prompt,
                )
            except Exception as e:
                last_reason = self._describe_llm_error(e)
                logger.warning(f"{label}[{provider_label}]: 调用异常 — {last_reason}", exc_info=True)
                continue
            text = llm_resp.completion_text if llm_resp else None
            if text and text.strip():
                return text.strip()
            last_reason = "LLM 响应为空"
            logger.warning(f"{label}[{provider_label}]: {last_reason}")
        logger.error(
            f"{label}[{provider_label}]失败（重试 {len(self.LLM_RETRY_DELAYS)} 次后放弃）— {last_reason}"
        )
        return None

    async def _call_llm(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        """通过 Astrbot 已配置的 LLM 提供商调用 AI（带重试与异常分类）"""
        return await self._text_chat_with_retry(prompt, system_prompt, "LLM 调用")

    @staticmethod
    def _http_body_text(response) -> str:
        """尽力把 HTTP 响应体解成可读文本。

        httpx 未安装 brotli 包时，若网关无视 Accept-Encoding 强推 Brotli，
        响应体就是二进制垃圾；这里按 Content-Encoding 手工解压兜底
        （gzip/deflate 可能已被 httpx 解压过，解压失败则沿用原字节），
        再依次尝试 UTF-8 / GB18030，最后 replace 解码，保证日志可读、不抛异常。
        """
        raw = response.content or b""
        encoding = (response.headers.get("content-encoding") or "").lower()
        if "br" in encoding:
            try:
                import brotli
                raw = brotli.decompress(raw)
            except ImportError:
                logger.warning("响应为 Brotli 压缩但运行环境未安装 brotli 包，无法解压"
                               "（可在 AstrBot 环境执行 pip install brotli）")
            except Exception as e:
                logger.warning(f"Brotli 解压失败: {e}")
        elif "gzip" in encoding:
            try:
                raw = gzip.decompress(raw)
            except Exception as e:
                logger.debug(f"gzip 手工解压跳过（可能已被 httpx 解压）: {e}")
        elif "deflate" in encoding:
            try:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception as e:
                logger.debug(f"deflate 手工解压跳过（可能已被 httpx 解压）: {e}")
        for charset in ("utf-8", "gb18030"):
            try:
                return raw.decode(charset)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _json_from_response(self, response):
        """解析 JSON 响应体；失败时记录可读的正文摘录并返回 None。"""
        text = self._http_body_text(response)
        try:
            return json.loads(text)
        except ValueError as e:
            logger.warning(f"响应体不是合法 JSON: {e}; 正文开头: {text[:200]}")
            return None

    def _strip_translation(self, translated: str) -> str:
        translated = translated.strip()
        if translated.startswith('"') and translated.endswith('"'):
            translated = translated[1:-1]
        if translated.startswith("'") and translated.endswith("'"):
            translated = translated[1:-1]
        return translated.strip()

    async def _translate_text(self, text: str, source_lang: str = "zh", target_lang: str = "ja") -> str:
        """使用 Astrbot 已配置的 LLM 提供商翻译文本；调用失败或翻译无效时回退原文"""
        if not text or not text.strip():
            return text

        translated = await self._text_chat_with_retry(
            prompt=text,
            system_prompt=f"You are a professional translator. Translate the following text from {source_lang} to {target_lang}. Only return the translated text, no explanations, no quotes, no additional formatting.",
            label="翻译",
        )
        if translated:
            cleaned = self._strip_translation(translated)
            if cleaned and cleaned != text:
                logger.info(f"翻译: {text[:30]}... -> {cleaned[:30]}...")
                return cleaned

        logger.warning(f"翻译失败，使用原文: {text[:30]}...")
        return text

    async def _ensure_tts_text(self, story_data: dict) -> dict:
        """确保所有 Talk 片段都有 ttsText；缺失时批量一次翻译，避免逐条串行 LLM。"""
        provider = self._get_provider()
        if not provider:
            logger.warning("LLM 提供商未配置，跳过ttsText检查")
            return story_data

        snippets = story_data.get("snippets", [])
        need_translate = []
        for snippet in snippets:
            if isinstance(snippet, dict) and snippet.get("type") == "Talk":
                data = snippet.get("data", {})
                content = data.get("content", "")
                tts_text = data.get("ttsText", "")
                if content and not tts_text:
                    need_translate.append((snippet, data, content))

        if not need_translate:
            logger.info("所有Talk片段已有ttsText字段，无需补充翻译")
            return story_data

        logger.info(f"发现 {len(need_translate)} 条Talk缺少ttsText，开始补充翻译...")

        if len(need_translate) == 1:
            _, data, content = need_translate[0]
            data["ttsText"] = await self._translate_text(content, "zh", "ja")
            logger.info("补充翻译完成: 1/1 条")
            return story_data

        numbered = "\n".join(f"{i + 1}. {content}" for i, (_, _, content) in enumerate(need_translate))
        try:
            llm_text = await self._text_chat_with_retry(
                prompt=numbered,
                system_prompt=(
                    "You are a professional translator. Translate each numbered Chinese line to Japanese. "
                    "Return ONLY a JSON array of strings, one translation per input line, same order. "
                    "No explanations, no markdown."
                ),
                label="批量翻译",
            )
            translations = self._extract_json_from_response(llm_text) if llm_text else None
            if isinstance(translations, list) and len(translations) == len(need_translate):
                for (snippet, data, _), translated in zip(need_translate, translations):
                    data["ttsText"] = self._strip_translation(str(translated)) or data.get("content", "")
                    snippet["data"] = data
                logger.info(f"批量补充翻译完成: {len(need_translate)} 条")
                return story_data
            logger.warning("批量翻译结果无法解析，回退逐条翻译")
        except Exception as e:
            logger.warning(f"批量翻译异常，回退逐条翻译: {e}")

        translated_count = 0
        for snippet, data, content in need_translate:
            data["ttsText"] = await self._translate_text(content, "zh", "ja")
            snippet["data"] = data
            translated_count += 1
        logger.info(f"补充翻译完成: {translated_count}/{len(need_translate)} 条")
        return story_data

    async def _call_llm_structured(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        """
        调用 LLM 生成结构化 JSON 输出
        策略：先尝试 json_object 模式，失败则用普通调用
        """
        try:
            provider = self._get_provider()
            if not provider:
                logger.error("LLM 提供商未配置")
                return None

            provider_config = provider.provider_config if hasattr(provider, 'provider_config') else {}
            api_base = provider_config.get("api_base", "") or provider_config.get("base_url", "")
            api_key = provider_config.get("api_key", "")
            model = provider_config.get("model_config", {}).get("model", "") if isinstance(provider_config.get("model_config"), dict) else provider_config.get("model", "")

            # 策略1: 尝试 json_object 模式（兼容性好，Gemini/OpenAI 都支持）
            if api_base and api_key and model:
                try:
                    result = await self._call_openai_json_object(api_base, api_key, model, prompt, system_prompt)
                    if result:
                        return result
                except Exception as e:
                    logger.warning(f"json_object 模式失败: {e}")

            # 策略2: 通过 Astrbot Provider 普通调用
            return await self._call_llm(prompt, system_prompt)

        except Exception as e:
            logger.warning(f"结构化输出失败，回退到普通调用: {e}")
            return await self._call_llm(prompt, system_prompt)

    async def _call_openai_json_object(self, api_base: str, api_key: str, model: str, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        """使用 json_object 模式调用 API，确保返回合法 JSON"""
        url = api_base.rstrip('/')
        if "/chat/completions" not in url:
            if url.endswith("/v1"):
                url += "/chat/completions"
            elif url.endswith("/v1/"):
                url += "chat/completions"
            else:
                url += "/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # 明确只接受 gzip/deflate：部分中转网关会给未声明 br 的客户端强推 Brotli，
            # httpx 未装 brotli 包时解不开，读取响应会变成 UTF-8 解码错误
            "Accept-Encoding": "gzip, deflate",
        }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 16384,
            "response_format": {"type": "json_object"}
        }

        client = await self._get_http_client()
        response = None
        # 第一次带 response_format；被网关拒绝（4xx）时去掉该参数重试一次
        for attempt, use_json_format in enumerate((True, False), start=1):
            payload = dict(body)
            if not use_json_format:
                payload.pop("response_format", None)
            response = await client.post(url, headers=headers, json=payload, timeout=120.0)
            if response.status_code == 200:
                break
            logger.warning(
                f"json_object 模式返回 HTTP {response.status_code}（第 {attempt} 次尝试）: "
                f"{self._http_body_text(response)[:300]}"
            )
        if response is None or response.status_code != 200:
            return None

        data = self._json_from_response(response)
        if not isinstance(data, dict):
            return None
        choices = data.get("choices") or []
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if content and content.strip():
                return content.strip()
        return None

    def _persona_registry(self, view) -> PersonaRegistry:
        """从当前插件配置解析人格注册表，并把告警写入日志。"""
        registry = PersonaRegistry.from_config(self.config)
        registry.warn_unmatched(view)
        for w in registry.warnings:
            logger.warning(f"MySekaiStoryteller 人格配置: {w}")
        return registry

    def _build_character_pool(self, registry: PersonaRegistry) -> str:
        """从资源目录 + 人格配置组装角色池文本（配置的整段人设 / 通用演绎兜底）。"""
        view = self._catalog.view()
        blocks = []
        for m in view.models:
            header = f"**{m.get('name', '')}** — {view.model_ref(m['id'])}"
            prompt = registry.prompt_for(m)
            blocks.append(f"{header}\n{prompt if prompt else GENERIC_PROFILE_TEXT}")
        return "\n\n".join(blocks) if blocks else "（资源目录为空，请检查渲染宿主）"

    @staticmethod
    def _build_acting_example(view) -> str:
        """生成与当前目录 ID/资源一致的双人/单人示例：说话者 2 个连续身体动作+表情、听者反应。"""
        models = view.models[:2] or [view.default_model()]
        ids = [m.get("id") for m in models]
        entries = [
            {"id": m.get("id"), "model": m.get("path"), "normal_scale": 2.1,
             "small_scale": 1.8, "anchor": 0.5} for m in models
        ]

        def stage_motion(model_id):
            m = view.model_by_id(model_id) or view.default_model()
            names = m.get("motions") or []
            return next((n for n in names if "default" not in n.lower() and "stand" not in n.lower()),
                        view.default_motion(model_id))

        def action(model_id, at, field):
            m = view.model_by_id(model_id) or view.default_model()
            values = m.get("motions" if field == "motion" else "facials") or []
            fallback = view.default_motion(model_id) if field == "motion" else view.default_facial(model_id)
            return {"at": at, "modelId": model_id, field: values[0] if values else fallback}

        def body_motion(model_id, at, index):
            """示例里说话者的第 index 个不同非站姿身体动作；清单凑不齐就省略该项。"""
            m = view.model_by_id(model_id) or view.default_model()
            names = list(dict.fromkeys(n for n in (m.get("motions") or [])
                                       if "default" not in n.lower() and "stand" not in n.lower()))
            return {"at": at, "modelId": model_id, "motion": names[index]} if index < len(names) else None

        if len(ids) == 1:
            speaker = ids[0]
            talk_actions = [item for item in (body_motion(speaker, 0.15, 0),
                                              body_motion(speaker, 0.5, 1),
                                              action(speaker, 0.8, "facial")) if item]
            appears = [{"type": "LayoutAppear", "wait": True, "delay": 0,
                        "data": {"modelId": ids[0], "from": {"side": "Right", "offset": 100},
                                 "to": {"side": "Center", "offset": 0},
                                 "motion": stage_motion(ids[0]), "facial": view.default_facial(ids[0]),
                                 "facialFirst": True, "moveSpeed": "Normal"}}]
        else:
            first, second = ids
            appears = []
            for model_id, side, enter_offset in ((first, "Left", -100), (second, "Right", 100)):
                appears.append({"type": "LayoutAppear", "wait": True, "delay": 0,
                                "data": {"modelId": model_id, "from": {"side": side, "offset": enter_offset},
                                         "to": {"side": side, "offset": 0},
                                         "motion": stage_motion(model_id), "facial": view.default_facial(model_id),
                                         "facialFirst": True, "moveSpeed": "Normal"}})
            talk_actions = [item for item in (body_motion(first, 0.15, 0),
                                              body_motion(first, 0.45, 1),
                                              action(first, 0.7, "facial"),
                                              action(second, 0.85, "facial")) if item]
        snippets = [{"type": "ChangeLayoutMode", "wait": False, "delay": 0, "data": {"mode": "Normal"}},
                    *appears,
                    {"type": "Talk", "wait": False, "delay": 0,
                     "data": {"speaker": view.short_name(models[0]), "content": "你好。", "ttsText": "こんにちは。",
                              "modelId": ids[0], "actions": talk_actions}},
                    {"type": "HideTalk", "wait": True, "delay": 0.2},
                    *[{"type": "LayoutClear", "wait": True, "delay": 0.1,
                       "data": {"modelId": m.get("id"),
                                "from": {"side": "Center" if len(ids) == 1 else ("Left" if i == 0 else "Right"), "offset": 0},
                                "to": {"side": "Center" if len(ids) == 1 else ("Left" if i == 0 else "Right"),
                                       "offset": 100 if (len(ids) == 1 or i == 1) else -100},
                                "motion": stage_motion(m.get("id")), "facial": view.default_facial(m.get("id")), "moveSpeed": "Normal"}}
                      for i, m in enumerate(models)],
                    {"type": "BlackOut", "wait": True, "delay": 0, "data": {"duration": 600}}]
        return json.dumps({"models": entries, "images": [{"id": 1, "image": view.default_image()}],
                           "snippets": snippets}, ensure_ascii=False)

    async def _build_prompt(self, scene: str) -> tuple[str, str]:
        """构建系统提示词和用户提示词（剧本模式）"""
        system_prompt = "你是视觉小说导演。只输出合法 JSON（仅含 models、images、snippets），不要 markdown 或解释。写成约 3 分钟的短戏，禁止超长剧情；对话多少按角色人设自行把握。"

        # 刷新资源目录（自带 TTL，正常情况零开销），避免重启后一直使用兜底目录
        await self._catalog.refresh()
        view = self._catalog.view()
        registry = self._persona_registry(view)

        user_prompt = _fill_template(self.prompt_template, {
            "{character_pool}": self._build_character_pool(registry),
            "{model_table}": view.model_table(),
            "{id_mapping}": view.id_mapping(),
            "{acting_example}": self._build_acting_example(view),
            "{motion_list}": view.motion_list(),
            "{facial_list}": view.facial_list(),
            "{image_list}": view.image_list(),
            "{bgm_list}": view.bgm_list(),
            "{scene}": scene,
        })

        return system_prompt, user_prompt

    async def _build_chat_prompt(self, scene: str, user_id: str) -> tuple[str, str]:
        """构建聊天模式提示词（AI 根据用户内容从人格池中选角）"""
        system_prompt = "你是视觉小说角色扮演导演。只输出合法 JSON（仅含 models、images、snippets），不要 markdown 或解释。对话多少按角色人设自行把握。"

        # 定期清理超时会话
        await self._cleanup_expired_sessions()

        # 获取历史对话上下文
        history = self.chat_history.get(user_id, [])
        chat_history_text = ""
        if history:
            recent = history[-6:]  # 最近3轮对话
            for msg in recent:
                chat_history_text += f"{msg['role']}: {msg['content']}\n"
        else:
            chat_history_text = "（首次对话）"

        # 刷新资源目录（自带 TTL，正常情况零开销），避免重启后一直使用兜底目录
        await self._catalog.refresh()
        view = self._catalog.view()
        registry = self._persona_registry(view)

        user_prompt = _fill_template(CHAT_MODE_PROMPT_TEMPLATE, {
            "{persona_pool}": self._build_character_pool(registry),
            "{acting_example}": self._build_acting_example(view),
            "{model_table}": view.model_table(),
            "{id_mapping}": view.id_mapping(),
            "{motion_list}": view.motion_list(),
            "{facial_list}": view.facial_list(),
            "{image_list}": view.image_list(),
            "{bgm_list}": view.bgm_list(),
            "{chat_history}": chat_history_text,
            "{scene}": scene,
        })

        return system_prompt, user_prompt

    async def _add_chat_history(self, user_id: str, user_msg: str, bot_content: str, speaker: str = ""):
        """添加聊天历史记录并持久化（role 为本次实际说话的角色名）"""
        view = self._catalog.view()
        chat_role = speaker or view.short_name(view.default_model())
        async with self._history_lock:
            self._user_session_timestamps[user_id] = time.time()
            if user_id not in self.chat_history:
                self.chat_history[user_id] = []
            self.chat_history[user_id].append({"role": "用户", "content": user_msg})
            self.chat_history[user_id].append({"role": chat_role, "content": bot_content})
            if len(self.chat_history[user_id]) > 10:
                self.chat_history[user_id] = self.chat_history[user_id][-10:]
            self._save_chat_history()
            self._save_session_timestamps()

    async def _cleanup_expired_sessions(self):
        """清理超时会话并持久化"""
        now = time.time()
        expired_users = []
        async with self._history_lock:
            for user_id, timestamp in list(self._user_session_timestamps.items()):
                if now - timestamp > self._session_timeout_seconds:
                    expired_users.append(user_id)

            for user_id in expired_users:
                self.chat_history.pop(user_id, None)
                self._user_session_timestamps.pop(user_id, None)
                logger.info(f"清理超时会话: {user_id}")

            if expired_users:
                logger.info(f"已清理 {len(expired_users)} 个超时会话")
                self._save_chat_history()
                self._save_session_timestamps()

    def _catalog_view(self):
        """资源目录快照（校验与修复的唯一事实来源）"""
        return self._catalog.view()

    def _fix_model_path(self, path: str) -> str:
        """修复模型路径：清理格式，验证有效性（必须在目录中），无效时回退默认"""
        view = self._catalog_view()
        cleaned = self._clean_path(path)
        if not cleaned:
            return view.default_model_path()
        if cleaned in view.valid_model_paths():
            return cleaned
        logger.warning(f"Invalid model path '{cleaned}', falling back to default")
        return view.default_model_path()

    def _fix_image_path(self, path: str) -> str:
        """修复图片路径（必须在目录中，支持大小写/子串模糊匹配）"""
        view = self._catalog_view()
        cleaned = self._clean_path(path)
        valid_images = view.valid_images()
        if cleaned in valid_images:
            return cleaned

        lower = cleaned.lower()
        for valid in valid_images:
            if valid.lower() in lower or lower in valid.lower():
                return valid

        logger.warning(f"Invalid image path '{path}', using default: {view.default_image()}")
        return view.default_image()

    @staticmethod
    def _to_number(val, default=0):
        """强制转换为数字"""
        if isinstance(val, (int, float)):
            return val
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                try:
                    return float(val)
                except ValueError:
                    return default
        return default

    @staticmethod
    def _to_bool(val, default=False):
        """强制转换为布尔值"""
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        if isinstance(val, (int, float)):
            return bool(val)
        return default

    @staticmethod
    def _to_str(val, default=""):
        """强制转换为字符串"""
        if isinstance(val, str):
            return val
        if val is None:
            return default
        return str(val)

    @staticmethod
    def _clean_path(val, default=""):
        """清理文件路径：去除多余空格，特别是点号周围的空格"""
        s = MySekaiStorytellerPlugin._to_str(val, default)
        import re as _re
        s = _re.sub(r'\s*\.\s*', '.', s)
        s = _re.sub(r'\s*/\s*', '/', s)
        s = s.strip()
        return s if s else default

    VALID_SIDES = {"Center", "Left", "Right"}
    VALID_MOVE_SPEEDS = {"Slow", "Normal", "Fast", "Immediate"}
    VALID_LAYOUT_MODES = {"Normal"}

    @staticmethod
    def _animation_choices(view, model_id, field: str) -> set:
        """目录为空时只放行已知默认值，不把空白名单当作任意资源可用。"""
        if field == "motion":
            return view.valid_motions(model_id) or {view.default_motion(model_id)}
        return view.valid_facials(model_id) or {view.default_facial(model_id)}

    @staticmethod
    def _offscreen_position(side: str) -> dict:
        """同侧画外起点/终点：Left 走左侧画外、Right 走右侧画外、Center 从右侧画外斜向进出。"""
        return ({"side": "Right", "offset": 100} if side == "Center"
                else {"side": side, "offset": -100 if side == "Left" else 100})

    @staticmethod
    def _is_standing_motion(name: str) -> bool:
        lowered = (name or "").lower()
        return "default" in lowered or "stand" in lowered

    def _pick_stage_motion(self, view, model_id: int) -> str:
        """入场/退场兜底动作：跳过 default 站姿类，取清单里第一个有肢体表现的动作。"""
        model = view.model_by_id(model_id) or view.default_model()
        for name in model.get("motions") or []:
            if not self._is_standing_motion(name):
                return name
        return view.default_motion(model_id)

    def _validate_actions(self, data: dict, view, model_ids: set, label: str) -> None:
        if "actions" not in data:
            return
        actions = data["actions"]
        # 单句表演配额兜底（超出直接裁掉，不触发整场重试）：说话者一句内可连续演，至多 5 处变化、
        # 听者类事件（非说话者目标）至多 1 处、同一角色相邻变化至少隔 0.2。
        # 渲染层仍允许手工剧本最多 24 项，这里只约束插件产出。
        if not isinstance(actions, list) or len(actions) > 8:
            raise ValueError(f"{label}.actions 必须是最多 8 项的数组")
        normalized = []
        for index, action in enumerate(actions):
            where = f"{label}.actions[{index}]"
            if not isinstance(action, dict) or set(action) - {"at", "modelId", "motion", "facial"}:
                raise ValueError(f"{where} 只能包含 at/modelId/motion/facial")
            at = action.get("at")
            if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= 1:
                raise ValueError(f"{where}.at 必须是 0..1 的有限数字（时长比例）")
            model_id = action.get("modelId")
            if type(model_id) is not int or model_id not in model_ids:
                raise ValueError(f"{where}.modelId 必须是 models 中的角色整数 ID")
            cleaned = {"at": at, "modelId": model_id}
            for field in ("motion", "facial"):
                if field not in action:
                    continue
                value = action[field]
                if not isinstance(value, str) or value not in self._animation_choices(view, model_id, field):
                    raise ValueError(f"{where}.{field} 必须是该角色资源清单中的完整名称")
                cleaned[field] = value
            if len(cleaned) == 2:
                raise ValueError(f"{where} 至少需要一个 motion 或 facial")
            normalized.append(cleaned)
        normalized.sort(key=lambda action: action["at"])
        speaker_id = data.get("modelId")
        by_model: dict[int, list[dict]] = {}
        for action in normalized:
            by_model.setdefault(action["modelId"], []).append(action)
        kept: list[dict] = []
        for actor_id, items in by_model.items():
            is_listener = actor_id != speaker_id
            cap = 1 if is_listener else 5
            cap_name = "听者反应至多 1 处" if is_listener else "同一角色至多 5 处变化"
            last_at = None
            count = 0
            for action in items:
                if count >= cap:
                    logger.info(f"{label}.actions {cap_name}，已裁掉 {action.get('motion') or action.get('facial')}")
                    continue
                if last_at is not None and action["at"] - last_at < 0.2 - 1e-9:
                    logger.info(f"{label}.actions 同一角色相邻变化间隔过近（<0.2），已裁掉 {action.get('motion') or action.get('facial')}")
                    continue
                kept.append(action)
                last_at = action["at"]
                count += 1
        kept.sort(key=lambda action: action["at"])
        data["actions"] = kept

    def _normalize_scene(self, story_data: dict) -> None:
        """插件的舞台约束；非法换人交给原有 LLM 重试，不删台词、不代选退场者。

        单句表演配额（说话者 ≤5、听者 ≤1、同角色间隔 <0.2）在 _validate_actions 硬执行；
        表演密度交给提示词把握，这里不再做整场预算。
        """
        visible: dict[int, dict] = {}
        mode = None
        for index, snippet in enumerate(story_data["snippets"]):
            kind = snippet["type"]
            data = snippet.get("data", {})
            model_id = data.get("modelId")
            label = f"snippets[{index}] {kind}"
            if kind == "ChangeLayoutMode":
                mode = data.get("mode", "Normal")
                if mode not in self.VALID_LAYOUT_MODES | {"One", "Two"} or (mode == "One" and len(visible) > 1):
                    raise ValueError(f"{label} 只允许 Normal 下的单人/双人舞台，切换前须让多余角色退场")
                # One/Two 仅容错识别为意图，宿主实际只接收 Normal。
                data["mode"] = "Normal"
            elif kind == "LayoutAppear":
                if model_id in visible:
                    raise ValueError(f"{label} 角色已在场，不能重复入场")
                if len(visible) >= 2 or (mode == "One" and visible):
                    raise ValueError(f"{label} 同时最多两人在场；先 LayoutClear(wait:true) 完全滑出旧角色再入场")
                side = data["to"]["side"] if data["to"]["side"] in self.VALID_SIDES else "Left"
                position = {"side": side, "offset": 0}
                if any(p["side"] == position["side"] or "Center" in (p["side"], position["side"]) for p in visible.values()):
                    raise ValueError(f"{label} 双人必须占用不同的 Left/Right 槽位")
                # 槽位固定在画面内 offset:0；起点强制改写为同侧画外 → 滑入（入场动画系统）
                data["to"] = dict(position)
                data["from"] = self._offscreen_position(side)
                visible[model_id] = position
            elif kind in {"LayoutClear", "Move"}:
                if model_id not in visible:
                    raise ValueError(f"{label} 角色不在场")
                data["from"] = dict(visible[model_id])
                snippet["wait"] = True
                if kind == "LayoutClear":
                    # 退场 = 滑出到同侧画外（退场动画系统），槽位随之释放
                    data["to"] = self._offscreen_position(visible[model_id]["side"])
                    visible.pop(model_id)
                else:
                    position = data["to"]
                    if any(other != model_id and (p["side"] == position["side"] or "Center" in (p["side"], position["side"])) for other, p in visible.items()):
                        raise ValueError(f"{label} 不能移入已占用的槽位")
                    visible[model_id] = dict(position)
            elif kind in {"Talk", "Motion", "DoParam"}:
                if not (kind == "Talk" and model_id == -1) and model_id not in visible:
                    raise ValueError(f"{label} 说话/动作角色必须已入场且未退场")
                if kind == "Talk" and model_id == -1 and (data.get("motion") or data.get("facial")):
                    raise ValueError(f"{label} 旁白不能播放角色动作")
                for action in data.get("actions", []):
                    if action["modelId"] not in visible:
                        raise ValueError(f"{label}.actions 的角色 {action['modelId']} 不在场，不能表演听者反应")
                # 静默片段完成后才能换人；听者在说话期间的反应只能放在 Talk.actions。
                if kind == "Motion" and "actions" in data:
                    snippet["wait"] = True

    @staticmethod
    def _split_tts_text(tts: str, group_count: int) -> list[str]:
        """把一段 ttsText 均分成 group_count 份（优先按换行，其次按句子边界）。"""
        line_parts = [seg.strip() for seg in tts.split("\n") if seg.strip()]
        if len(line_parts) == group_count:
            return line_parts
        sentence_parts = [s for s in re.split(r"(?<=[。！？!?.])\s*", tts) if s.strip()]
        if len(sentence_parts) == group_count:
            return [s.strip() for s in sentence_parts]
        if len(sentence_parts) > group_count:
            base, extra = divmod(len(sentence_parts), group_count)
            buckets: list[str] = []
            idx = 0
            for i in range(group_count):
                take = base + (1 if i < extra else 0)
                buckets.append("".join(sentence_parts[idx:idx + take]).strip())
                idx += take
            return buckets
        if line_parts:
            # 保持原文连续顺序，不能按轮转分桶（否则 1/3/5 会跑到 2/4/6 前）。
            base, extra = divmod(len(line_parts), group_count)
            buckets = []
            index = 0
            for i in range(group_count):
                take = base + (1 if i < extra else 0)
                buckets.append("\n".join(line_parts[index:index + take]))
                index += take
            return buckets
        return [""] * group_count

    @classmethod
    def _split_overflow_talks(cls, story_data: dict) -> dict:
        """把超过 TALK_MAX_LINES 行的 Talk 拆成同角色的连续多条，台词一字不丢。

        排版上限只决定"怎么拆"，不减少总话量：保留说话人/modelId/voice；
        actions 按文本时长比例分段重映射，旧标量仅首条播放，后续 delay 取 0.15。
        """
        snippets = story_data.get("snippets")
        if not isinstance(snippets, list):
            return story_data

        result: list = []
        for snippet in snippets:
            if not isinstance(snippet, dict) or snippet.get("type") != "Talk":
                result.append(snippet)
                continue
            data = snippet.get("data") or {}
            lines = [l for l in str(data.get("content") or "").split("\n") if l.strip()]
            if len(lines) <= TALK_MAX_LINES:
                result.append(snippet)
                continue

            groups = [lines[i:i + TALK_MAX_LINES] for i in range(0, len(lines), TALK_MAX_LINES)]
            tts_groups = cls._split_tts_text(str(data.get("ttsText") or ""), len(groups))
            # TTS 时长尚未由宿主解析，用连续文本字宽近似分配时间；每个事件只归属一条。
            weights = [max(1.0, sum(_char_width_units(ch) for line in group for ch in line)) for group in groups]
            total = sum(weights)
            start = 0.0
            for gi, group in enumerate(groups):
                end = start + weights[gi] / total if gi < len(groups) - 1 else 1.0
                part_data = copy.deepcopy(data)
                part_data.update(content="\n".join(group), ttsText=tts_groups[gi])
                if "actions" in data:
                    part_data["actions"] = []
                    for action in data["actions"]:
                        at = action["at"]
                        if start <= at < end or (gi == len(groups) - 1 and at == 1):
                            event = copy.deepcopy(action)
                            event["at"] = min(1.0, max(0.0, (at - start) / (end - start)))
                            part_data["actions"].append(event)
                if gi:
                    # 旧标量只在原台词起点播放，不因分页重复点头/重置表情。
                    part_data["motion"] = ""
                    part_data["facial"] = ""
                part = {
                    "type": "Talk",
                    "wait": snippet.get("wait", False),
                    "delay": snippet.get("delay", 0) if gi == 0 else 0.15,
                    "data": part_data,
                }
                result.append(part)
                start = end
            logger.info(f"Talk 超过 {TALK_MAX_LINES} 行，已拆分为 {len(groups)} 条保留完整台词")

        story_data["snippets"] = result
        return story_data

    def _validate_and_fix_story(self, story_data: dict) -> dict:
        """验证并修复 AI 生成的 JSON 格式，确保符合 MSS API 要求。尽可能自动修复，不报错。"""
        if not isinstance(story_data, dict):
            raise ValueError("剧本必须是 JSON 对象")

        view = self._catalog_view()

        # modelId 绑定目录角色，不能出现同 ID 多模型或路径与 ID 指向不同角色。
        default_id = view.default_model().get("id", 1)
        if not isinstance(story_data.get("models"), list) or not story_data["models"]:
            story_data["models"] = [{"id": default_id, "model": view.default_model_path()}]
        model_ids = set()
        for i, model in enumerate(story_data["models"]):
            if not isinstance(model, dict):
                raise ValueError(f"models[{i}] 必须是角色对象")
            raw_id = model.get("id", default_id)
            model_id = self._to_number(raw_id, -1)
            if isinstance(raw_id, bool) or not math.isfinite(model_id) or int(model_id) != model_id or not view.model_by_id(model_id):
                raise ValueError(f"models[{i}].id 必须来自角色目录")
            if model_id in model_ids:
                raise ValueError(f"models[{i}].id 重复: {model_id}")
            model_ids.add(model_id)
            model["id"] = int(model_id)
            model["model"] = view.model_by_id(model_id)["path"]
            model["normal_scale"] = self._to_number(model.get("normal_scale"), 2.1)
            model["small_scale"] = self._to_number(model.get("small_scale"), 1.8)
            model["anchor"] = self._to_number(model.get("anchor"), 0.5)

        # 确保 images 是数组，为空则填充默认背景
        if "images" not in story_data or not isinstance(story_data.get("images"), list) or len(story_data["images"]) == 0:
            story_data["images"] = [{"id": 1, "image": view.default_image()}]
        else:
            for i, image in enumerate(story_data["images"]):
                if not isinstance(image, dict):
                    story_data["images"][i] = {"id": i + 1, "image": view.default_image()}
                    continue
                image["id"] = self._to_number(image.get("id"), i + 1)
                image["image"] = self._fix_image_path(image.get("image", ""))

        # 确保 snippets 是数组
        if "snippets" not in story_data or not isinstance(story_data.get("snippets"), list):
            raise ValueError("snippets 必须是数组")

        if len(story_data["snippets"]) == 0:
            raise ValueError("snippets 不能为空")

        valid_types = set(SNIPPET_SCHEMAS.keys())

        for i, snippet in enumerate(story_data["snippets"]):
            if not isinstance(snippet, dict) or not isinstance(snippet.get("type"), str) or snippet["type"] not in valid_types:
                raise ValueError(f"snippets[{i}] 必须是有效类型的片段对象")
            snippet_type = snippet["type"]
            snippet["wait"] = self._to_bool(snippet.get("wait"), False)
            snippet["delay"] = self._to_number(snippet.get("delay"), 0)
            if not math.isfinite(snippet["delay"]) or snippet["delay"] < 0:
                raise ValueError(f"snippets[{i}].delay 必须是非负有限秒数")

            needs_data = "data" in SNIPPET_SCHEMAS[snippet_type].get("properties", {})
            if needs_data:
                snippet.setdefault("data", {})
                if not isinstance(snippet["data"], dict):
                    raise ValueError(f"snippets[{i}].data 必须是对象")
            if snippet_type in {"Talk", "Motion", "LayoutAppear", "LayoutClear", "Move", "DoParam"}:
                data = snippet["data"]
                raw_id = data.get("modelId", default_id)
                model_id = self._to_number(raw_id, -2)
                if isinstance(raw_id, bool) or not math.isfinite(model_id) or int(model_id) != model_id or (model_id not in model_ids and not (snippet_type == "Talk" and model_id == -1)):
                    raise ValueError(f"snippets[{i}].modelId 必须是 models 中的角色整数 ID（旁白 Talk 可用 -1）")
                data["modelId"] = int(model_id)
                if snippet_type in {"Talk", "Motion"}:
                    self._validate_actions(data, view, model_ids, f"snippets[{i}]")

            if snippet_type == "Talk":
                data = snippet.get("data", {})
                default_model = view.default_model()
                data["speaker"] = self._to_str(data.get("speaker"), view.name_by_id(default_model.get("id")))
                content = self._to_str(data.get("content"), "...")
                content = content.replace("\\n", "\n")
                data["content"] = sanitize_display_text(content)
                data["modelId"] = self._to_number(data.get("modelId"), default_model.get("id", 1))
                data["voice"] = self._to_str(data.get("voice"), "")
                # 说话并发动作/表情：非法值回退该角色默认；空串 = 保持当前姿态不播
                talk_motion = self._clean_path(self._to_str(data.get("motion"), ""))
                if talk_motion and talk_motion not in view.valid_motions(data["modelId"]):
                    logger.warning(f"Talk motion '{talk_motion}' not available for {view.name_by_id(data['modelId'])}, falling back to {view.default_motion(data['modelId'])}")
                    talk_motion = view.default_motion(data["modelId"])
                data["motion"] = talk_motion
                talk_facial = self._clean_path(self._to_str(data.get("facial"), ""))
                if talk_facial and talk_facial not in view.valid_facials(data["modelId"]):
                    logger.warning(f"Talk facial '{talk_facial}' not available for {view.name_by_id(data['modelId'])}, falling back to {view.default_facial(data['modelId'])}")
                    talk_facial = view.default_facial(data["modelId"])
                data["facial"] = talk_facial
                snippet["data"] = data

            elif snippet_type == "LayoutAppear":
                data = snippet.get("data", {})
                data["modelId"] = self._to_number(data.get("modelId"), 1)
                model_id = data["modelId"]
                data["motion"] = self._to_str(data.get("motion"), "")
                data["facial"] = self._to_str(data.get("facial"), view.default_facial(model_id))
                for field in ("motion", "facial"):
                    if data[field] and data[field] not in self._animation_choices(view, model_id, field):
                        logger.warning(f"LayoutAppear {field} '{data[field]}' not available for {view.name_by_id(model_id)}, falling back")
                        data[field] = "" if field == "motion" else view.default_facial(model_id)
                # 入场必须有动作，且禁止 default 站姿滑入（站姿滑入等于站桩）
                if not data["motion"] or self._is_standing_motion(data["motion"]):
                    data["motion"] = self._pick_stage_motion(view, model_id)
                data["facialFirst"] = self._to_bool(data.get("facialFirst"), True)
                data["moveSpeed"] = self._to_str(data.get("moveSpeed"), "Normal")
                if data["moveSpeed"] not in self.VALID_MOVE_SPEEDS:
                    data["moveSpeed"] = "Normal"
                data["hologram"] = self._to_bool(data.get("hologram"), False)
                if "to" not in data or not isinstance(data.get("to"), dict):
                    data["to"] = {"side": "Left", "offset": 0}
                data["to"]["side"] = self._to_str(data["to"].get("side"), "Left")
                if data["to"]["side"] not in self.VALID_SIDES:
                    data["to"]["side"] = "Left"
                # 槽位永远在画面内 offset:0；起点写同侧画外 → 滑入（_normalize_scene 按槽位追踪覆盖）
                data["to"]["offset"] = 0
                data["from"] = self._offscreen_position(data["to"]["side"])
                snippet["data"] = data

            elif snippet_type == "LayoutClear":
                data = snippet.get("data", {})
                data["modelId"] = self._to_number(data.get("modelId"), 1)
                model_id = data["modelId"]
                data["moveSpeed"] = self._to_str(data.get("moveSpeed"), "Normal")
                if data["moveSpeed"] not in self.VALID_MOVE_SPEEDS:
                    data["moveSpeed"] = "Normal"
                if "from" not in data or not isinstance(data.get("from"), dict):
                    data["from"] = {"side": "Center", "offset": 0}
                data["from"]["side"] = self._to_str(data["from"].get("side"), "Center")
                if data["from"]["side"] not in self.VALID_SIDES:
                    data["from"]["side"] = "Center"
                data["from"]["offset"] = self._to_number(data["from"].get("offset"), 0)
                # 退场 = 滑出到同侧画外；from/to 由 _normalize_scene 按当前追踪槽位覆盖。
                data["to"] = self._offscreen_position(data["from"]["side"])
                # 退场必须有动作（禁止无动画消失），且禁止 default 站姿退场
                clear_motion = self._clean_path(self._to_str(data.get("motion"), ""))
                if clear_motion and clear_motion not in view.valid_motions(model_id):
                    logger.warning(f"LayoutClear motion '{clear_motion}' not available for {view.name_by_id(model_id)}, falling back")
                    clear_motion = ""
                if not clear_motion or self._is_standing_motion(clear_motion):
                    clear_motion = self._pick_stage_motion(view, model_id)
                data["motion"] = clear_motion
                clear_facial = self._clean_path(self._to_str(data.get("facial"), ""))
                if not clear_facial:
                    clear_facial = view.default_facial(model_id)
                elif clear_facial not in view.valid_facials(model_id):
                    logger.warning(f"LayoutClear facial '{clear_facial}' not available for {view.name_by_id(model_id)}, falling back to {view.default_facial(model_id)}")
                    clear_facial = view.default_facial(model_id)
                data["facial"] = clear_facial
                snippet["data"] = data

            elif snippet_type == "Motion":
                data = snippet.get("data", {})
                data["modelId"] = self._to_number(data.get("modelId"), 1)
                model_id = data["modelId"]
                has_actions = "actions" in data
                if has_actions or "duration" in data:
                    duration = data.get("duration", 2)
                    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 120:
                        raise ValueError(f"snippets[{i}].duration 必须是大于 0 且不超过 120 的有限秒数")
                    data["duration"] = duration
                motion = self._to_str(data.get("motion"), "" if has_actions else view.default_motion(model_id))
                facial = self._to_str(data.get("facial"), "" if has_actions else view.default_facial(model_id))
                if motion and motion not in self._animation_choices(view, model_id, "motion"):
                    logger.warning(f"Motion '{motion}' not available for {view.name_by_id(model_id)}, falling back to {view.default_motion(model_id)}")
                    motion = view.default_motion(model_id)
                if facial and facial not in self._animation_choices(view, model_id, "facial"):
                    logger.warning(f"Facial '{facial}' not available for {view.name_by_id(model_id)}, falling back to {view.default_facial(model_id)}")
                    facial = view.default_facial(model_id)
                data["motion"] = motion
                data["facial"] = facial
                data["facialFirst"] = self._to_bool(data.get("facialFirst"), True)
                snippet["data"] = data

            elif snippet_type == "Move":
                data = snippet.get("data", {})
                data["modelId"] = self._to_number(data.get("modelId"), 1)
                data["moveSpeed"] = self._to_str(data.get("moveSpeed"), "Normal")
                if data["moveSpeed"] not in self.VALID_MOVE_SPEEDS:
                    data["moveSpeed"] = "Normal"
                if "from" not in data or not isinstance(data.get("from"), dict):
                    data["from"] = {"side": "Center", "offset": 0}
                if "to" not in data or not isinstance(data.get("to"), dict):
                    data["to"] = {"side": "Left", "offset": 0}
                data["from"]["side"] = self._to_str(data["from"].get("side"), "Center")
                if data["from"]["side"] not in self.VALID_SIDES:
                    data["from"]["side"] = "Center"
                data["from"]["offset"] = self._to_number(data["from"].get("offset"), 0)
                data["to"]["side"] = self._to_str(data["to"].get("side"), "Left")
                if data["to"]["side"] not in self.VALID_SIDES:
                    data["to"]["side"] = "Left"
                data["to"]["offset"] = self._to_number(data["to"].get("offset"), 0)
                snippet["data"] = data

            elif snippet_type == "BlackOut":
                data = snippet.get("data", {})
                data["duration"] = self._to_number(data.get("duration"), 800)
                snippet["data"] = data

            elif snippet_type == "BlackIn":
                data = snippet.get("data", {})
                data["duration"] = self._to_number(data.get("duration"), 1000)
                snippet["data"] = data

            elif snippet_type == "ChangeBackgroundImage":
                data = snippet.get("data", {})
                data["imageId"] = self._to_number(data.get("imageId"), 1)
                snippet["data"] = data

            elif snippet_type == "Telop":
                data = snippet.get("data", {})
                data["content"] = sanitize_display_text(self._to_str(data.get("content"), ""), wrap=False)
                snippet["data"] = data

            elif snippet_type == "ChangeLayoutMode":
                data = snippet.get("data", {})
                data["mode"] = self._to_str(data.get("mode"), "Normal")
                snippet["data"] = data

            elif snippet_type == "DoParam":
                data = snippet.get("data", {})
                data["modelId"] = self._to_number(data.get("modelId"), 1)
                if not isinstance(data.get("params"), list):
                    data["params"] = []
                snippet["data"] = data

        # 先验证原时间线；不通过时由已有生成流程请求 LLM 修正，不删除角色/台词。
        self._normalize_scene(story_data)
        # 排版收尾：超行 Talk 拆成连续多条，台词不删减
        story_data = self._split_overflow_talks(story_data)
        return story_data

    def _extract_json_from_response(self, response: str):
        """从 AI 响应中提取 JSON"""
        if not response:
            return None

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        json_pattern = r"```(?:json)?\s*(.*?)\s*```"
        match = re.search(json_pattern, response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(response[start:end + 1])
            except json.JSONDecodeError:
                pass

        start = response.find("[")
        end = response.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(response[start:end + 1])
            except json.JSONDecodeError:
                pass

        logger.error(f"无法从响应中提取 JSON: {response[:200]}...")
        return None

    async def _export_video(self, story_data: dict, timeout: int = 600, progress=None) -> dict:
        """调用 MSS API 导出视频，并下载到本地"""
        try:
            export_timeout_ms = timeout * 1000

            body = {
                "story": story_data,
                "timeout": export_timeout_ms
            }

            client = await self._get_http_client()
            response = await client.post(
                f"{self.mss_api_url}/api/v1/export",
                json=body,
                timeout=httpx.Timeout(timeout + 60, read=timeout + 60),
            )

            if response.status_code == 429:
                return {
                    "success": False,
                    "message": "渲染服务正忙，请稍后再试",
                }
            if response.status_code != 200:
                return {
                    "success": False,
                    "message": f"渲染服务返回 HTTP {response.status_code}"
                }

            result = response.json()
            if progress:
                await progress(60)
            if not result.get("success"):
                return result

            download_url = result.get("downloadUrl", "")
            video_path = result.get("videoPath", "")

            logger.info(f"导出成功: videoPath={video_path}, downloadUrl={download_url}")

            local_path = None

            if download_url:
                full_url = f"{self.mss_api_url}{download_url}" if download_url.startswith("/") else download_url
                logger.info(f"Downloading video from: {full_url}")
                logger.info(f"[调试] video_dir: {self.video_dir}")
                logger.info(f"[调试] apifile_dir: {self.apifile_dir}")
                for attempt in range(3):
                    try:
                        client = await self._get_http_client()
                        dl_response = await client.get(
                            full_url,
                            timeout=httpx.Timeout(300.0, read=300.0),
                        )
                        if dl_response.status_code == 200 and len(dl_response.content) > 0:
                            timestamp = int(time.time())
                            local_path = str(self.video_dir / f"video_{timestamp}_{uuid.uuid4().hex[:8]}.mp4")
                            logger.info(f"[调试] 生成的本地路径：{local_path}")
                            if progress:
                                await progress(90)
                            with open(local_path, "wb") as f:
                                f.write(dl_response.content)
                            logger.info(f"Video downloaded: {local_path} ({len(dl_response.content)} bytes)")
                            break
                        else:
                            logger.warning(f"Download attempt {attempt + 1} failed: HTTP {dl_response.status_code}, size={len(dl_response.content)}")
                    except Exception as e:
                        logger.warning(f"Download attempt {attempt + 1} error: {e}")
                    if attempt < 2:
                        await asyncio.sleep(3)

            if not local_path and video_path and os.path.exists(video_path):
                local_path = video_path
                logger.info(f"Using local video path: {local_path}")

            if not local_path:
                return {
                    "success": False,
                    "message": f"视频导出成功但无法下载。downloadUrl={download_url}, videoPath={video_path}"
                }

            return {
                "success": True,
                "localPath": local_path,
                "downloadUrl": download_url,
                "fileSize": os.path.getsize(local_path) if local_path and os.path.exists(local_path) else 0,
                "duration": result.get("duration", 0),
                "frameCount": result.get("frameCount", 0)
            }

        except httpx.TimeoutException:
            return {
                "success": False,
                "message": f"视频导出超时（{timeout}秒），请稍后重试"
            }
        except httpx.ConnectError:
            return {
                "success": False,
                "message": "无法连接渲染服务，请确认 MySekaiStoryteller-API 正在运行",
            }
        except Exception as e:
            logger.error(f"视频导出失败: {e}")
            return {
                "success": False,
                "message": "视频导出失败，请稍后重试"
            }

    @staticmethod
    def _user_error(raw) -> str:
        """把内部异常转成用户可读的短提示，避免把堆栈或内部路径发到群里。"""
        text = str(raw or "").strip()
        lower = text.lower()
        if not text or text in ("timeout",):
            return "视频生成超时，请稍后重试"
        if "排队已满" in text or "队列已满" in text:
            return "当前排队已满，请稍后再试"
        if "不是有效 json" in lower or "jsondecodeerror" in lower:
            return "模型服务返回了空响应，请稍后再试；若反复出现请联系管理员检查该模型通道"
        if "无法解析" in text or "json" in lower:
            return "剧本生成失败，请换个说法再试一次"
        if "超时" in text or "timeout" in lower:
            return "视频生成超时，请稍后重试"
        if "无法连接" in text or "connect" in lower:
            return "渲染服务暂时连不上，请稍后再试"
        if "正忙" in text or "429" in text:
            return "渲染服务正忙，请稍后再试"
        if "未配置" in text or "llm" in lower:
            return "语言模型未就绪，请稍后再试或联系管理员"
        if "utf-8" in lower or "解码" in text or "decode" in lower:
            return "模型服务返回了异常内容，请稍后再试；若反复出现请联系管理员检查模型中转配置"
        if len(text) > 80 or "traceback" in lower or "/" in text or "\\" in text:
            return "视频生成失败，请稍后重试"
        return f"视频生成失败：{text}"

    async def _ensure_queue_processor_started(self):
        """确保队列处理器和清理任务已启动"""
        if self._queue_processor_started:
            return
        async with self._start_lock:
            if self._queue_processor_started:
                return
            await self.export_queue.start()
            await self._start_cleanup_task()
            self._queue_processor_started = True
            logger.info("视频导出队列处理器已启动")

    @filter.command("统计", alias={'stats', '统计信息', '导出统计'})
    async def stats_cmd(self, event: AstrMessageEvent):
        """查看视频导出统计"""
        yield event.plain_result(self.get_stats_report())

    @filter.command("视频对话", alias={'视频生成', '视频聊天'})
    async def mss_chat_mode(self, event: AstrMessageEvent, message: str):
        """
        聊天模式：AI 根据消息内容选择角色，生成短视频回复
        """
        if self.test_mode:
            yield event.plain_result("🔧 功能维护中，请稍后再试。")
            return

        if not self._get_provider():
            yield event.plain_result("LLM 提供商未配置，请在 Astrbot 中配置一个对话模型")
            return

        health = await self._check_mss_api_health()
        if health["status"] != "ok":
            yield event.plain_result(f"视频导出服务不可用\n\n{health['message']}")
            return

        user_id = str(event.get_sender_id())

        await self._ensure_queue_processor_started()

        # 剧本并行生成（不占导出队列），完成后自动入导出队列渲染并发送
        event_context = self._extract_event_context(event)
        backlog = await self._queue_backlog()
        yield event.plain_result(self._initial_reply(backlog, "chat"))
        asyncio.create_task(self._pipeline_chat(event, message, user_id, event_context, backlog))

    @filter.command("剧本生成", alias={'剧本对话', '故事生成', 'story', '生成剧本', '生成故事'})
    async def mss_story_mode(self, event: AstrMessageEvent, scene: str):
        """
        剧本模式：生成完整剧本视频
        """
        if self.test_mode:
            yield event.plain_result("🔧 功能维护中，请稍后再试。")
            return

        if not self._get_provider():
            yield event.plain_result("LLM 提供商未配置，请在 Astrbot 中配置一个对话模型")
            return

        health = await self._check_mss_api_health()
        if health["status"] != "ok":
            yield event.plain_result(f"视频导出服务不可用\n\n{health['message']}")
            return

        user_id = str(event.get_sender_id())
        await self._ensure_queue_processor_started()

        # 剧本并行生成（不占导出队列），完成后自动入导出队列渲染并发送
        event_context = self._extract_event_context(event)
        backlog = await self._queue_backlog()
        yield event.plain_result(self._initial_reply(backlog, "story"))
        asyncio.create_task(self._pipeline_story(event, scene, user_id, event_context, backlog))

    @filter.command("测试视频对话", alias={'测试视频生成', '测试视频聊天'})
    async def mss_test_chat_mode(self, event: AstrMessageEvent, message: str):
        """测试模式：AI 选角生成短视频回复，不受维护状态影响"""
        if not self._get_provider():
            yield event.plain_result("LLM 提供商未配置，请在 Astrbot 中配置一个对话模型")
            return

        health = await self._check_mss_api_health()
        if health["status"] != "ok":
            yield event.plain_result(f"视频导出服务不可用\n\n{health['message']}")
            return

        user_id = str(event.get_sender_id())

        await self._ensure_queue_processor_started()

        # 剧本并行生成（不占导出队列），完成后自动入导出队列渲染并发送
        event_context = self._extract_event_context(event)
        backlog = await self._queue_backlog()
        yield event.plain_result(self._initial_reply(backlog, "chat"))
        asyncio.create_task(self._pipeline_chat(event, message, user_id, event_context, backlog))

    @filter.command("测试剧本生成", alias={'测试故事生成', '测试story', '测试生成剧本'})
    async def mss_test_story_mode(self, event: AstrMessageEvent, scene: str):
        """测试模式：生成完整剧本视频，不受维护状态影响"""
        if not self._get_provider():
            yield event.plain_result("LLM 提供商未配置，请在 Astrbot 中配置一个对话模型")
            return

        health = await self._check_mss_api_health()
        if health["status"] != "ok":
            yield event.plain_result(f"视频导出服务不可用\n\n{health['message']}")
            return

        user_id = str(event.get_sender_id())

        await self._ensure_queue_processor_started()

        # 剧本并行生成（不占导出队列），完成后自动入导出队列渲染并发送
        event_context = self._extract_event_context(event)
        backlog = await self._queue_backlog()
        yield event.plain_result(self._initial_reply(backlog, "story"))
        asyncio.create_task(self._pipeline_story(event, scene, user_id, event_context, backlog))

    def _extract_event_context(self, event: AstrMessageEvent) -> dict:
        """提取事件上下文信息，用于后续消息发送"""
        try:
            return {
                "user_id": str(event.get_sender_id()),
                "platform": event.get_platform_name(),
                "message_id": getattr(event, 'message_id', ''),
                "group_id": getattr(event, 'group_id', ''),
                "unified_msg_origin": getattr(event, 'unified_msg_origin', ''),
            }
        except Exception as e:
            logger.warning(f"提取事件上下文失败: {e}")
            return {}

    async def _monitor_and_send_video(self, task_id: str, event: AstrMessageEvent, event_context: dict = None):
        """监控任务完成并发送视频"""
        try:
            result = await self.export_queue.wait_for_task(task_id, timeout=None)

            if not result:
                await self._send_safe_message(event, self._user_error("timeout"), event_context)
                return

            if result.get("status") == "completed":
                task_result = result.get("result", {})
                if task_result.get("success"):
                    await self._send_video_to_user(event, task_result, event_context)
                else:
                    await self._send_safe_message(
                        event,
                        self._user_error(task_result.get("error")),
                        event_context
                    )
            elif result.get("status") == "timeout":
                await self._send_safe_message(event, self._user_error("timeout"), event_context)
            elif result.get("status") == "cancelled":
                await self._send_safe_message(event, "任务已取消", event_context)
            elif result.get("status") == "failed":
                await self._send_safe_message(
                    event,
                    self._user_error(result.get("error")),
                    event_context
                )
            else:
                await self._send_safe_message(
                    event,
                    self._user_error(result.get("error") or result.get("status")),
                    event_context
                )

        except Exception as e:
            logger.error(f"监控任务异常: {e}")
            await self._send_safe_message(event, self._user_error(e), event_context)

    def _fix_double_slash_path(self, path: str) -> str:
        """修复双斜杠路径"""
        if not path:
            return path
        original = path
        while path.startswith('//'):
            path = path[1:]
        if original != path:
            logger.info(f"[路径修复] {original} -> {path}")
        return path

    def _get_accessible_download_urls(self, download_url: str) -> list:
        """从 download_url 生成多种可访问的 URL 变体"""
        urls = []
        if not download_url:
            return urls
        urls.append(download_url)
        from urllib.parse import urlparse
        parsed = urlparse(download_url)
        hostname = parsed.hostname or ""
        if hostname in ("127.0.0.1", "localhost"):
            urls.append(download_url.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal"))
            urls.append(download_url.replace("127.0.0.1", "localhost").replace("//localhost", "//127.0.0.1") if "localhost" not in download_url else download_url)
            host_ip = self._detect_host_ip()
            if host_ip:
                urls.append(download_url.replace("127.0.0.1", host_ip).replace("localhost", host_ip))
        if self.callback_api_base:
            cb_parsed = urlparse(self.callback_api_base)
            if cb_parsed.hostname and cb_parsed.hostname != hostname:
                from urllib.parse import urlunparse
                replaced = urlunparse((cb_parsed.scheme, cb_parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
                if replaced not in urls:
                    urls.append(replaced)
        return urls

    def _detect_host_ip(self):
        """检测宿主机可用的 IP 地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None

    async def _send_video_to_user(self, event: AstrMessageEvent, task_result: dict, event_context: dict = None):
        """发送视频给用户（优先使用已下载的本地文件）"""
        try:
            local_path = task_result.get("localPath")
            download_url = task_result.get("downloadUrl")
            if local_path:
                local_path = self._fix_double_slash_path(local_path)
            logger.info(f"[调试] task_result: local_path={local_path}, download_url={download_url}")
            message = task_result.get("message", "🎬 视频生成成功！")
            unified_msg_origin = event_context.get("unified_msg_origin", "") if event_context else ""
            user_id = event_context.get("user_id", "") if event_context else ""

            if not unified_msg_origin:
                unified_msg_origin = getattr(event, 'unified_msg_origin', '')
            if not user_id:
                user_id = str(event.get_sender_id())

            # 先发送艾特通知
            if user_id:
                try:
                    qq_id = int(user_id)
                    notify_chain = MessageChain(chain=[Comp.At(qq=qq_id)])
                except (ValueError, TypeError):
                    notify_chain = MessageChain(chain=[Comp.At(qq=user_id)])
                await self.context.send_message(unified_msg_origin, notify_chain)
                logger.info(f"[发送视频] 已发送艾特通知: user_id={user_id}")

            # 第1重：使用已下载的本地文件发送
            if local_path and os.path.exists(local_path):
                try:
                    video_chain = MessageChain(chain=[Comp.Video.fromFileSystem(path=local_path)])
                    await self.context.send_message(unified_msg_origin, video_chain)
                    logger.info(f"视频发送成功（本地文件）: {local_path}")
                    return
                except Exception as e:
                    logger.warning(f"本地文件发送失败：{e}，尝试 HTTP URL 方式...")

            # 第2重：使用 HTTP URL 发送（尝试多种 URL 变体）
            if download_url:
                for url_variant in self._get_accessible_download_urls(download_url):
                    try:
                        video_chain = MessageChain(chain=[Comp.Video.fromURL(url=url_variant)])
                        await self.context.send_message(unified_msg_origin, video_chain)
                        logger.info(f"视频发送成功（HTTP URL）: {url_variant}")
                        return
                    except Exception as e:
                        logger.warning(f"HTTP URL 发送失败 {url_variant}: {e}")

            # 第3重：使用 AstrBot 文件服务注册视频
            if local_path and os.path.exists(local_path):
                try:
                    video = Comp.Video(file=local_path)
                    file_service_url = await video.register_to_file_service()
                    if self.callback_api_base and file_service_url:
                        from urllib.parse import urlparse, urlunparse
                        parsed = urlparse(file_service_url)
                        cb_parsed = urlparse(self.callback_api_base)
                        file_service_url = urlunparse((cb_parsed.scheme, cb_parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
                        logger.info(f"[文件服务] 使用 callback_api_base 替换: {file_service_url}")
                    logger.info(f"文件服务注册成功: {file_service_url}")
                    video_chain = MessageChain(chain=[Comp.Video.fromURL(url=file_service_url)])
                    await self.context.send_message(unified_msg_origin, video_chain)
                    logger.info(f"视频发送成功（文件服务）: {file_service_url}")
                    return
                except Exception as e:
                    logger.warning(f"文件服务发送失败：{e}")

            # 第4重：手动注册到文件服务，用配置的 callback_api_base 或自检测 IP 构造可访问 URL
            if local_path and os.path.exists(local_path):
                try:
                    from astrbot.core import file_token_service, astrbot_config as ast_conf
                    token = await file_token_service.register_file(local_path)
                    if self.callback_api_base:
                        file_url = f"{self.callback_api_base}/api/file/{token}"
                    else:
                        dashboard_port = ast_conf.get("dashboard", {}).get("port", 6185)
                        host_ip = self._detect_host_ip()
                        if not host_ip:
                            raise RuntimeError("无法检测宿主机 IP 且未配置 callback_api_base")
                        file_url = f"http://{host_ip}:{dashboard_port}/api/file/{token}"
                    video_chain = MessageChain(chain=[Comp.Video.fromURL(url=file_url)])
                    await self.context.send_message(unified_msg_origin, video_chain)
                    logger.info(f"视频发送成功（手动文件服务）: {file_url}")
                    return
                except Exception as e:
                    logger.warning(f"手动文件服务发送失败：{e}")

            # 第5重：使用 normalized path 尝试直接发送
            if local_path and os.path.exists(local_path):
                try:
                    normalized_path = local_path.replace('\\', '/')
                    normalized_path = '/' + normalized_path.lstrip('/')
                    video_chain = MessageChain(chain=[Comp.Video(file=normalized_path, path=normalized_path)])
                    await self.context.send_message(unified_msg_origin, video_chain)
                    logger.info(f"视频发送成功（normalized path）: {normalized_path}")
                    return
                except Exception as e:
                    logger.warning(f"normalized path 发送失败：{e}")

            # 全部失败，发送路径提示
            if local_path and os.path.exists(local_path):
                await self.context.send_message(unified_msg_origin, MessageChain(chain=[Comp.Plain(f"📁 文件路径: {local_path}")]))
            else:
                await self.context.send_message(unified_msg_origin, MessageChain(chain=[Comp.Plain("⚠️ 但视频文件已丢失")]))

        except Exception as e:
            logger.error(f"发送视频失败: {e}")
            await self._send_safe_message(event, f"❌ 视频发送失败: {str(e)}", event_context)

    async def _send_safe_message(self, event: AstrMessageEvent, message: str, event_context: dict = None):
        """安全发送消息（避免异常）"""
        try:
            unified_msg_origin = event_context.get("unified_msg_origin", "") if event_context else getattr(event, 'unified_msg_origin', '')
            user_id = event_context.get("user_id", "") if event_context else str(event.get_sender_id())

            chain = []
            if user_id:
                chain.append(Comp.At(qq=user_id))
            chain.append(Comp.Plain(message))

            await self.context.send_message(unified_msg_origin, MessageChain(chain=chain))
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    async def _generate_chat_story(self, message: str, user_id: str) -> tuple:
        """LLM 生成对话剧本（含校验重试），返回 (story_data, 首个说话角色)。
        只做剧本阶段，不涉及视频导出——由调用方决定何时入导出队列。"""
        system_prompt, user_prompt = await self._build_chat_prompt(message, user_id)
        max_retries = 2
        story_data = None
        last_failure = "LLM 未返回内容"

        for attempt in range(max_retries + 1):
            llm_response = await self._call_llm_structured(user_prompt, system_prompt)
            if not llm_response:
                last_failure = "LLM 调用失败"
                if attempt < max_retries:
                    logger.warning(f"LLM 未返回内容，重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(1.0)
                continue

            story_data = self._extract_json_from_response(llm_response)
            if not story_data:
                if attempt < max_retries:
                    logger.warning(f"JSON解析失败，重试 ({attempt+1}/{max_retries})")
                    continue
                raise ValueError("AI 返回的内容无法解析为 JSON")

            try:
                story_data = self._validate_and_fix_story(story_data)
                break
            except ValueError as e:
                if attempt < max_retries:
                    logger.warning(f"验证失败，重试 ({attempt+1}/{max_retries}): {e}")
                    user_prompt = f"上次生成的剧本有问题: {e}\n请修正后重新输出完整JSON。原始要求：\n\n{user_prompt}"
                    continue
                raise e

        if not story_data:
            raise ValueError(f"{last_failure}（已重试 {max_retries} 次，请稍后再试）")

        story_data = await self._ensure_tts_text(story_data)

        # 添加聊天历史（role 记录本次实际说话的角色）
        first_content = ""
        first_speaker = ""
        for s in story_data.get("snippets", []):
            if s.get("type") == "Talk":
                first_content = s.get("data", {}).get("content", "")
                first_speaker = s.get("data", {}).get("speaker", "")
                break
        await self._add_chat_history(user_id, message, first_content, first_speaker)
        return story_data, first_speaker

    async def _generate_story(self, scene: str) -> dict:
        """LLM 生成剧本（含校验重试），只做剧本阶段。"""
        system_prompt, user_prompt = await self._build_prompt(scene)
        max_retries = 2
        story_data = None
        last_failure = "LLM 未返回内容"

        for attempt in range(max_retries + 1):
            llm_response = await self._call_llm_structured(user_prompt, system_prompt)
            if not llm_response:
                last_failure = "LLM 调用失败"
                if attempt < max_retries:
                    logger.warning(f"LLM 未返回内容，重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(1.0)
                continue

            story_data = self._extract_json_from_response(llm_response)
            if not story_data:
                if attempt < max_retries:
                    logger.warning(f"JSON解析失败，重试 ({attempt+1}/{max_retries})")
                    continue
                raise ValueError("AI 返回的内容无法解析为 JSON")

            try:
                story_data = self._validate_and_fix_story(story_data)
                break
            except ValueError as e:
                if attempt < max_retries:
                    logger.warning(f"验证失败，AI自动修正重试 ({attempt+1}/{max_retries}): {e}")
                    user_prompt = f"上次生成的剧本有问题: {e}\n请修正后重新输出完整JSON。原始要求：\n\n{user_prompt}"
                    continue
                raise e

        if not story_data:
            raise ValueError(f"{last_failure}（已重试 {max_retries} 次，请稍后再试）")

        return await self._ensure_tts_text(story_data)

    async def _queue_backlog(self) -> int:
        """命令到达时导出队列的积压估算（等待中 + 正在渲染）。"""
        try:
            stats = await self.export_queue.get_queue_stats()
            return (stats.get('pending_count', 0) or 0) + (stats.get('running_count', 0) or 0)
        except Exception as e:
            logger.warning(f"读取队列积压失败: {e}")
            return 0

    def _initial_reply(self, position: int, wait_kind: str) -> str:
        """命令即时回复：只提示生成中，不报排队位次与预计等待。"""
        return "视频生成中，生成时间较久，请耐心等待...."

    async def _enqueue_export_and_monitor(self, event, event_context: dict, user_id: str,
                                          story_data: dict, description: str,
                                          priority: int, wait_kind: str,
                                          reported_position: int = 0):
        """剧本完成后入导出队列并监控发送。剧本阶段已结束，这里只处理导出。"""
        try:
            task_id = await self.export_queue.add_task(
                user_id=user_id,
                coroutine_func=self._queued_export_and_send,
                priority=priority,
                kwargs={
                    "story_data": story_data,
                    "sender_id": user_id,
                    "description": description,
                    "event_context": event_context
                },
                progress_arg="progress"
            )
        except QueueFullError:
            await self._send_safe_message(event, "剧本已生成，但渲染队列已满，请稍后再试", event_context)
            return

        await self._monitor_and_send_video(task_id, event, event_context)

    async def _pipeline_chat(self, event, message: str, user_id: str, event_context: dict = None,
                             reported_position: int = 0):
        """对话模式流水线：剧本（受 _script_semaphore 并发）→ 导出队列（串行渲染）"""
        try:
            async with self._script_semaphore:
                story_data, first_speaker = await self._generate_chat_story(message, user_id)
            await self._enqueue_export_and_monitor(
                event, event_context, user_id=user_id,
                story_data=story_data,
                description=f"{first_speaker}的回复" if first_speaker else "角色回复",
                priority=1, wait_kind="chat", reported_position=reported_position
            )
        except Exception as e:
            logger.error(f"对话剧本生成失败: {e}")
            await self._send_safe_message(event, self._user_error(e), event_context)

    async def _pipeline_story(self, event, scene: str, sender_id: str, event_context: dict = None,
                              reported_position: int = 0):
        """剧本模式流水线：剧本（受 _script_semaphore 并发）→ 导出队列（串行渲染）"""
        try:
            async with self._script_semaphore:
                story_data = await self._generate_story(scene)
            await self._enqueue_export_and_monitor(
                event, event_context, user_id=sender_id,
                story_data=story_data,
                description=scene,
                priority=2, wait_kind="story", reported_position=reported_position
            )
        except Exception as e:
            logger.error(f"剧本生成失败: {e}")
            await self._send_safe_message(event, self._user_error(e), event_context)

    async def _queued_export_and_send(self, story_data: dict, sender_id: str = "", description: str = "视频", event_context: dict = None, progress=None) -> dict:
        """队列任务调用的视频导出方法"""
        timestamp = int(time.time())
        story_path = self.story_dir / f"story_{timestamp}_{uuid.uuid4().hex[:8]}.json"
        try:
            with open(str(story_path), "w", encoding="utf-8") as f:
                json.dump(story_data, f, ensure_ascii=False, indent=2)
            logger.info(f"剧本已保存: {story_path}")
        except Exception as e:
            logger.warning(f"保存剧本失败: {e}")

        task_key = f"export_{timestamp}_{uuid.uuid4().hex[:8]}"
        self.active_exports.add(task_key)
        start_time = time.time()

        try:
            result = await self._export_video(story_data, self.export_timeout, progress)

            self.active_exports.discard(task_key)
            elapsed = int(time.time() - start_time)

            # 记录统计数据
            await self.record_export(result.get("success", False), elapsed)

            if result.get("success"):
                local_path = result.get("localPath", "")
                download_url = result.get("downloadUrl", "")
                file_size = result.get("fileSize", 0)

                if local_path:
                    local_path = self._fix_double_slash_path(local_path)
                    file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0

                # 构建完整的下载 URL
                full_download_url = None
                if download_url:
                    full_download_url = f"{self.mss_api_url}{download_url}" if download_url.startswith("/") else download_url
                
                return {
                    "success": True,
                    "localPath": local_path,
                    "downloadUrl": full_download_url,
                    "message": f"🎬 视频导出成功！\n⏱️ 总耗时: {elapsed}秒\n📦 大小: {file_size / 1024 / 1024:.1f} MB",
                    "elapsed": elapsed,
                    "fileSize": file_size
                }
            else:
                return {
                    "success": False,
                    "error": result.get("message", "未知错误")
                }
        except Exception as e:
            self.active_exports.discard(task_key)
            logger.error(f"视频导出异常: {e}")
            raise

    async def _export_and_send_video(self, story_data: dict, event, description: str = "视频"):
        """导出视频并发送给用户（聊天模式和剧本模式共用）"""
        timestamp = int(time.time())
        story_path = self.story_dir / f"story_{timestamp}_{uuid.uuid4().hex[:8]}.json"
        try:
            with open(str(story_path), "w", encoding="utf-8") as f:
                json.dump(story_data, f, ensure_ascii=False, indent=2)
            logger.info(f"剧本已保存: {story_path}")
        except Exception as e:
            logger.warning(f"保存剧本失败: {e}")

        task_key = f"export_{timestamp}_{id(story_data)}"
        self.active_exports.add(task_key)
        start_time = time.time()

        result = await self._export_video(story_data, self.export_timeout)

        self.active_exports.discard(task_key)
        elapsed = int(time.time() - start_time)

        if result.get("success"):
            local_path = result.get("localPath", "")
            download_url = result.get("downloadUrl", "")
            file_size = result.get("fileSize", 0)

            if local_path:
                local_path = self._fix_double_slash_path(local_path)
                file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0

            # 优先使用已下载的本地文件发送
            if local_path and os.path.exists(local_path):
                try:
                    yield event.chain_result([
                        Comp.Plain(f"视频导出成功！\n总耗时: {elapsed}秒\n大小: {file_size / 1024 / 1024:.1f} MB"),
                        Comp.Video.fromFileSystem(path=local_path)
                    ])
                    return
                except Exception as e:
                    logger.warning(f"Video.fromFileSystem failed: {e}, trying URL...")

            # 第2重：使用 HTTP URL 发送（尝试多种 URL 变体）
            video_url = None
            if download_url:
                video_url = f"{self.mss_api_url}{download_url}" if download_url.startswith("/") else download_url

            if video_url:
                for url_variant in self._get_accessible_download_urls(video_url):
                    try:
                        yield event.chain_result([
                            Comp.Plain(f"视频导出成功！\n总耗时: {elapsed}秒\n大小: {file_size / 1024 / 1024:.1f} MB"),
                            Comp.Video.fromURL(url=url_variant)
                        ])
                        return
                    except Exception as e:
                        logger.warning(f"Video.fromURL failed ({url_variant}): {e}")

            # 第3重：使用 AstrBot 文件服务注册视频
            if local_path and os.path.exists(local_path):
                try:
                    video = Comp.Video(file=local_path)
                    file_service_url = await video.register_to_file_service()
                    if self.callback_api_base and file_service_url:
                        from urllib.parse import urlparse, urlunparse
                        parsed = urlparse(file_service_url)
                        cb_parsed = urlparse(self.callback_api_base)
                        file_service_url = urlunparse((cb_parsed.scheme, cb_parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
                        logger.info(f"[文件服务] 使用 callback_api_base 替换: {file_service_url}")
                    logger.info(f"文件服务注册成功: {file_service_url}")
                    yield event.chain_result([
                        Comp.Plain(f"视频导出成功！\n总耗时: {elapsed}秒\n大小: {file_size / 1024 / 1024:.1f} MB"),
                        Comp.Video.fromURL(url=file_service_url)
                    ])
                    return
                except Exception as e:
                    logger.warning(f"文件服务发送失败：{e}")

            # 第4重：手动注册到文件服务，用配置的 callback_api_base 或自检测 IP 构造可访问 URL
            if local_path and os.path.exists(local_path):
                try:
                    from astrbot.core import file_token_service, astrbot_config as ast_conf
                    token = await file_token_service.register_file(local_path)
                    if self.callback_api_base:
                        file_url = f"{self.callback_api_base}/api/file/{token}"
                    else:
                        dashboard_port = ast_conf.get("dashboard", {}).get("port", 6185)
                        host_ip = self._detect_host_ip()
                        if not host_ip:
                            raise RuntimeError("无法检测宿主机 IP 且未配置 callback_api_base")
                        file_url = f"http://{host_ip}:{dashboard_port}/api/file/{token}"
                    yield event.chain_result([
                        Comp.Plain(f"视频导出成功！\n总耗时: {elapsed}秒\n大小: {file_size / 1024 / 1024:.1f} MB"),
                        Comp.Video.fromURL(url=file_url)
                    ])
                    return
                except Exception as e:
                    logger.warning(f"手动文件服务发送失败：{e}")

            if local_path and os.path.exists(local_path):
                normalized_path = local_path.replace('\\', '/')
                normalized_path = '/' + normalized_path.lstrip('/')
                try:
                    yield event.chain_result([
                        Comp.Plain(f"视频导出成功！\n总耗时: {elapsed}秒\n大小: {file_size / 1024 / 1024:.1f} MB"),
                        Comp.Video(file=normalized_path, path=normalized_path)
                    ])
                except Exception as e:
                    logger.warning(f"Video file send also failed: {e}")
                    try:
                        yield event.chain_result([
                            Comp.Plain(f"视频导出成功！\n总耗时: {elapsed}秒\n大小: {file_size / 1024 / 1024:.1f} MB"),
                            Comp.Video(file=normalized_path)
                        ])
                    except Exception as e2:
                        logger.error(f"All video send methods failed: {e2}")
                        yield event.plain_result(f"视频导出成功但发送失败\n下载地址: {video_url}\n文件路径: {normalized_path}\n大小: {file_size / 1024 / 1024:.1f} MB")
            else:
                yield event.plain_result(f"视频导出成功但文件丢失")
        else:
            yield event.plain_result(f"视频导出失败: {result.get('message', '未知错误')}")

    @filter.command_group("mssadmin")
    def mssadmin(self):
        """MySekaiStoryteller 管理指令组（子指令仅管理员）"""
        pass

    @mssadmin.command("status", alias={'状态', '系统状态', '查看状态'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        """查看插件状态"""
        health = await self._check_mss_api_health()
        queue_stats = await self.export_queue.get_queue_stats()
        
        status_text = (
            f"📊 系统状态\n\n"
            f"{'✅' if health['status'] == 'ok' else '❌'} 视频服务: {'正常' if health['status'] == 'ok' else '不可用'}\n"
            f"🎬 运行中: {queue_stats.get('running_count', 0)}/{self.max_concurrent_exports}\n"
            f"⏳ 排队中: {queue_stats.get('pending_count', 0)}\n\n"
        )
        status_text += self.get_stats_report()
        yield event.plain_result(status_text)

    @mssadmin.command("queue", alias={'队列', '排队', '队列状态'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def queue_status_cmd(self, event: AstrMessageEvent):
        """查看队列状态"""
        queue_stats = await self.export_queue.get_queue_stats()
        
        status_text = (
            f"📊 队列状态:\n"
            f"⏳ 排队: {queue_stats['pending_count']}\n"
            f"🔄 运行: {queue_stats['running_count']}\n"
            f"✅ 完成: {queue_stats['total_completed']}\n"
            f"❌ 失败: {queue_stats['total_failed']}"
        )
        yield event.plain_result(status_text)

    @mssadmin.command("cancel", alias={'取消', '终止', '停止'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cancel_task_cmd(self, event: AstrMessageEvent, task_id: str):
        """取消指定任务"""
        success = await self.export_queue.cancel_task(task_id)
        if success:
            yield event.plain_result(f"✅ 任务 {task_id[:8]} 已取消")
        else:
            yield event.plain_result(f"❌ 无法取消任务 {task_id[:8]}")

    @mssadmin.command("cleanup", alias={'清理', '清理文件', '删除临时文件'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cleanup(self, event: AstrMessageEvent):
        """清理文件"""
        cleaned = 0
        try:
            for directory in [self.video_dir, self.story_dir]:
                if not directory.exists():
                    continue
                for file in directory.iterdir():
                    try:
                        if file.is_file():
                            file.unlink()
                            cleaned += 1
                    except Exception as e:
                        logger.error(f"清理文件失败 {file}: {e}")
        except Exception as e:
            logger.error(f"清理文件失败: {e}")
        yield event.plain_result(f"已清理 {cleaned} 个文件")

    @mssadmin.command("resources", alias={'资源列表', '模型列表', '资源'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def resources_cmd(self, event: AstrMessageEvent):
        """查看渲染宿主当前可用的角色/背景/BGM 资源"""
        data = await self._catalog.refresh()
        view = self._catalog.view()
        if not view.models:
            yield event.plain_result("❌ 无法获取资源目录，请确认渲染宿主已启动且版本支持 /api/v1/resources")
            return

        lines = ["🎭 可用角色（模型清单 resources/models/models.yaml）："]
        for m in view.models:
            lines.append(
                f"  {view.short_name(m)}(id={m['id']}) — 动作 {len(m.get('motions') or [])} 个，表情 {len(m.get('facials') or [])} 个"
            )
        image_details = data.get("imageDetails") or []
        bgm_details = data.get("bgmDetails") or []
        if image_details:
            lines.append(f"🖼️ 可用背景（{len(image_details)}，见 resources/images/images.yaml）：")
            for d in image_details:
                name = d.get("name") or d.get("file")
                desc = (d.get("description") or "").strip()
                suffix = f" — {desc}" if desc else ""
                lines.append(f"  {d.get('file')}（{name}）{suffix}")
        else:
            images = data.get("images") or []
            lines.append(f"🖼️ 可用背景（{len(images)}）：{', '.join(images) or '无'}")
        if bgm_details:
            lines.append(f"🎵 可用 BGM（{len(bgm_details)}，见 resources/audio/bgm/bgm.yaml）：")
            for d in bgm_details:
                name = d.get("name") or d.get("file")
                desc = (d.get("description") or "").strip()
                suffix = f" — {desc}" if desc else ""
                lines.append(f"  {d.get('file')}（{name}）{suffix}")
            lines.append("  · 全局默认 BGM 由 bgm.yaml 的 path 决定，本列表就是导出的背景音乐候选")
        else:
            bgm = data.get("bgm") or []
            lines.append(f"🎵 可用 BGM（{len(bgm)}）：{', '.join(bgm) or '无'}（在宿主 config.yaml 的 bgm 节启用）")
        lines.append("💡 新增模型：放入宿主 resources/models/ 并在 models.yaml 登记；新增背景：放入 resources/images/ 并在 images.yaml 写描述；新增 BGM：放入 resources/audio/bgm/ 并在 bgm.yaml 写描述，约 30 秒后自动感知")
        yield event.plain_result("\n".join(lines))

    @mssadmin.command("setapi", alias={'设置api', '设置API', '更新api'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_api(self, event: AstrMessageEvent, url: str):
        """设置 MSS API 地址"""
        self.mss_api_url = url
        self.config["mss_api_url"] = url
        self.config.save_config()
        # API 地址变更后，资源目录客户端同步切到新地址
        self._catalog = ResourceCatalog(url)
        health = await self._check_mss_api_health()
        if health["status"] == "ok":
            yield event.plain_result(f"✅ API地址已更新: {url}")
        else:
            yield event.plain_result(f"⚠️ 地址已更新但连接失败: {url}")

    async def terminate(self):
        """AstrBot 插件终止时调用，用于优雅保存数据"""
        logger.info("MySekaiStoryteller 插件正在关闭，保存持久化数据...")

        # 保存所有持久化数据
        self._save_stats()
        self._save_chat_history()
        self._save_session_timestamps()

        # 停止异步清理任务
        await self._stop_cleanup_task()

        # 停止队列处理器
        await self.export_queue.stop()

        # 关闭 HTTP 客户端
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

        # 关闭资源目录客户端
        if self._catalog and self._catalog._client and not self._catalog._client.is_closed:
            await self._catalog._client.aclose()

        logger.info("MySekaiStoryteller 插件已安全关闭")
