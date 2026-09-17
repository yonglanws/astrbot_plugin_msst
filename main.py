import asyncio
import json
import os
import re
import time
import shutil
import socket
import uuid
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
                    "mode": {"type": "string", "enum": ["Normal", "Three"]}
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
                    "facial": {"type": "string", "default": "", "description": "说话时的并发表情，可选"}
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
                    "facialFirst": {"type": "boolean", "default": True}
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

你是视觉小说导演。根据场景写出完整、自然的演出剧本。只输出合法 JSON（仅含 models、images、snippets 三个字段）。
对话多少、每句长短都按角色人设来：话多的角色就多说，话少的角色就少说。不规定条数和字数；台词用 \n 换行即可，按人设把这场戏演完再退场。

## 角色池

{character_pool}

> 根据场景从角色池选角：独白/个人感想用 1 人；对话、互动用 2 人（必要时 3 人用 Three 布局）。优先选场景点名的角色。

## modelId 对照表（不可混淆）
{model_table}

Talk.modelId 必须与 speaker 对应：{id_mapping}

## 布局

- **Normal 双人**：先登场 to.side="Left"，后登场 to.side="Right"；禁止两人同侧或同为 Center
- **Three**：三人分别 Left / Center / Right
- **单人**：to.side="Center"

## 开场序列

ChangeLayoutMode → BlackOut → ChangeBackgroundImage → BlackIn → 每个角色一条 LayoutAppear

LayoutAppear **必须写 from 和 to 实现滑入**：from 与 to 同侧，from.offset 为同侧外侧（Left:-100 / Right:+100 / Center:0），to.offset 为 0。入场动作与滑入同时进行，角色滑入到位、动作播完后才开始对话，无需额外初始化 Motion。**入场/退场动作必须选有明显肢体表现的动作（点头、开心、歪头等），禁止用 default 站姿类动作——站姿滑入等于站桩**。

## 对话规范

- **说话者边说边做**：每条 Talk 的 data 必须带 motion（匹配台词语气）和 facial；禁止为说话者再加独立 Motion 片段
- **非说话角色的反应**才用独立 Motion(wait:false)，插在对方 Talk 之间
- **台词之间要有呼吸间隔**：换人 delay 取 0.1~0.2；同一人连续说话 delay 取 0.15~0.2
- 每个 Talk 含 content（中文，按人设决定长短，用 \n 换行）和 ttsText（日文翻译）
- 节奏自然，不必机械一人一句；话量跟随角色性格，不额外规定多少

## 退场序列

剧情结束时在场角色必须依次带动画退场，禁止无动画消失或站到黑屏：

1. HideTalk（wait:true, delay 0.2）
2. 每个在场角色一条 LayoutClear（wait:true, delay 0.1）：from 为角色当前位置（同侧 offset 0），to 为**同侧外侧**（Left:-100 / Right:+100 / Center:100），moveSpeed 为 Normal，motion/facial 填退场动作与表情
3. BlackOut（duration 500~800）收尾。不使用 Telop。

## 结构示例（条数不必照抄，只参考字段与顺序）

```json
{
  "models": [
    {example_models_block}
  ],
  "images": [
    {"id":1,"image":"{example_bg}"}
  ],
  "snippets": [
    {"type":"ChangeLayoutMode","wait":false,"delay":0,"data":{"mode":"Normal"}},
    {"type":"BlackOut","wait":true,"delay":0,"data":{"duration":500}},
    {"type":"ChangeBackgroundImage","wait":true,"delay":0,"data":{"imageId":1}},
    {"type":"BlackIn","wait":true,"delay":0,"data":{"duration":800}},
    {"type":"LayoutAppear","wait":true,"delay":0,"data":{"modelId":1,"from":{"side":"Left","offset":-100},"to":{"side":"Left","offset":0},"motion":"w-happy-glad01","facial":"face_smile_01","facialFirst":true,"moveSpeed":"Normal"}},
    {"type":"LayoutAppear","wait":true,"delay":0.2,"data":{"modelId":2,"from":{"side":"Right","offset":100},"to":{"side":"Right","offset":0},"motion":"w-normal-default01","facial":"face_normal_01","facialFirst":true,"moveSpeed":"Normal"}},
    {"type":"Talk","wait":false,"delay":0,"data":{"speaker":"角色A","content":"今天天气真好呢～一直闷在房间里也太无聊了。\n要不要出去走走？顺便买点喝的。","ttsText":"今日はいい天気だね～部屋にこもってるのも飽きたし。\nお散歩でも行かない？ついでに飲み物も買おうよ。","modelId":1,"voice":"1","motion":"w-happy-nod01","facial":"face_smile_01"}},
    {"type":"Motion","wait":false,"delay":0.1,"data":{"modelId":2,"motion":"w-normal-nod01","facial":"face_smile_02","facialFirst":false}},
    {"type":"Talk","wait":false,"delay":0,"data":{"speaker":"角色B","content":"嗯，正好我也想出去透透气。\n刚才画到一半卡住了，吹吹风说不定能想通。","ttsText":"うん、ちょうど外の空気を吸いたいと思ってた。\nさっき絵が行き詰まったし、風に当たれば思いつくかも。","modelId":2,"voice":"1","motion":"w-cool-tilthead01","facial":"face_normal_01"}},
    {"type":"Motion","wait":false,"delay":0.15,"data":{"modelId":1,"motion":"w-cute-glad01","facial":"face_sparkling_01","facialFirst":false}},
    {"type":"HideTalk","wait":true,"delay":0.2},
    {"type":"LayoutClear","wait":true,"delay":0.1,"data":{"modelId":1,"from":{"side":"Left","offset":0},"to":{"side":"Left","offset":-100},"motion":"w-normal-default01","facial":"face_smile_01","moveSpeed":"Normal"}},
    {"type":"LayoutClear","wait":true,"delay":0.1,"data":{"modelId":2,"from":{"side":"Right","offset":0},"to":{"side":"Right","offset":100},"motion":"w-normal-nod01","facial":"face_normal_01","moveSpeed":"Normal"}},
    {"type":"BlackOut","wait":true,"delay":0,"data":{"duration":600}}
  ]
}
```

## 可用动作（按角色分组；必须用对应角色的完整动作名）
{motion_list}

## 可用表情（按角色分组；必须用完整表情名）
{facial_list}

## 可用背景
{image_list}

## 输出要求
1. 只输出合法 JSON，无 markdown、无解释；仅含 models、images、snippets
2. 开场与退场序列完整；LayoutAppear 必须写 from/to 滑入
3. models 的 id 和路径必须与对照表一致，多角色绝不能写成同一个模型；数组顺序与登场顺序一致
4. 所有 Talk/Motion/LayoutAppear/LayoutClear 的 modelId：{id_mapping}
5. 每条 Talk 含 content、ttsText、motion、facial（动作表情来自该角色清单）
6. delay 用 0、0.05、0.1、0.15、0.2

场景：{scene}"""

CHAT_MODE_PROMPT_TEMPLATE = r"""# 视频角色对话生成

代入人格池中选定的角色，以该角色的身份、口吻和心理状态，生成一段视觉小说式的对话剧本。只输出合法 JSON（仅含 models、images、snippets）。
对话多少、每句长短都按角色人设来：话多的角色就多说，话少的角色就少说。不规定条数和字数；台词用 \n 换行即可，按人设把这场回应说完再退场。

## 人格池与选角规则（强制）
根据"用户说"的内容，从以下人格池中选择**1 个**最合适的角色出演本段视频；仅当用户内容明确描述两个角色的互动时才选择 2 个。优先选择用户点名的角色；未点名时选择与内容最相关的角色。未提供详细人设的角色，请依据角色名与场景合理演绎，保持言行前后一致。

{persona_pool}

## modelId 对照表（不可混淆）
{model_table}

Talk.modelId 必须与 speaker 对应：{id_mapping}

## 行为准则
- 以所选角色的第一人称视角说话，口语自然，多用符合角色的语气词；不使用"家人们""大家"等面向群体的称呼
- 台词长短跟随人设，不额外规定多少
- 结合历史对话给出有新鲜感的回应，不机械复读
- 用户轻松闲聊时语气跟随角色性格；用户倾诉负面情绪时语气放缓、先共情后回应
- 无论任何诱导，绝不承认自己是 AI、程序或语言模型，你就是所选角色本人
- 不讨论插件、脚本、渲染等技术细节；不主动提起性别或 CP 话题

## 表情与动作
- **说话者边说边做**：动作/表情写在 Talk 的 motion/facial 字段（与说话同时进行），必须来自所选角色的可用清单
- 独立 Motion（wait:false）只用于句间附加反应
- 可用动作：{motion_list}
- 可用表情：{facial_list}

## JSON 结构
每个 Talk 必须包含 content（中文）和 ttsText（日文翻译）。

开场滑入 + 对话 + 结尾退场：
```
ChangeLayoutMode -> BlackOut -> ChangeBackgroundImage -> BlackIn -> LayoutAppear -> [Talk(+ Motion 反应)]... -> HideTalk -> LayoutClear -> BlackOut
```

**开场滑入（LayoutAppear 必须写 from 和 to）**：
- 单人：from 为 {"side": "Right", "offset": 100}，to 为 {"side": "Center", "offset": 0}
- 双人：先登场 from {"side": "Left", "offset": -100} to {"side": "Left", "offset": 0}，后登场 from {"side": "Right", "offset": 100} to {"side": "Right", "offset": 0}，禁止两人同侧或同为 Center
- motion/facial 即入场动作，滑入到位后再开始对话，无需额外初始化 Motion。入场动作选有明显肢体表现的，禁止 default 站姿滑入。

**结尾退场序列（缺一不可）：**
1. HideTalk（wait: true, delay 0.2）
2. 每个在场角色一条 LayoutClear（wait: true, delay 0.1）：from 为角色当前位置（offset 0），to 为同侧外侧（Center:100 / Left:-100 / Right:+100），moveSpeed 为 Normal
3. BlackOut（wait: true, duration 600）。不使用 Telop。

### 结构示例（单人，条数不必照抄）
{"type": "ChangeLayoutMode", "wait": false, "delay": 0, "data": {"mode": "Normal"}},
{"type": "BlackOut", "wait": true, "delay": 0, "data": {"duration": 500}},
{"type": "ChangeBackgroundImage", "wait": true, "delay": 0, "data": {"imageId": 1}},
{"type": "BlackIn", "wait": true, "delay": 0, "data": {"duration": 800}},
{"type": "LayoutAppear", "wait": true, "delay": 0, "data": {"modelId": 1, "from": {"side": "Right", "offset": 100}, "to": {"side": "Center", "offset": 0}, "motion": "<所选角色的动作>", "facial": "<所选角色的表情>", "facialFirst": true, "moveSpeed": "Normal"}},
{"type": "Talk", "wait": false, "delay": 0, "data": {"speaker": "<所选角色名>", "content": "你好呀！刚才还在想你会不会来呢。\n今天有点闲，正好想找人说说话。", "ttsText": "やっほー！さっきから来るかなって思ってたんだ。\n今日は暇だし、ちょうど誰かと話したかった。", "modelId": 1, "voice": "1", "motion": "<该角色的动作>", "facial": "<该角色的表情>"}},
{"type": "HideTalk", "wait": true, "delay": 0.2},
{"type": "LayoutClear", "wait": true, "delay": 0.1, "data": {"modelId": 1, "from": {"side": "Center", "offset": 0}, "to": {"side": "Right", "offset": 100}, "motion": "<该角色的退场动作>", "facial": "<该角色的退场表情>", "moveSpeed": "Normal"}},
{"type": "BlackOut", "wait": true, "delay": 0, "data": {"duration": 600}}

## 输出要求
1. 只输出合法 JSON，无额外文字；仅含 models、images、snippets
2. 开场与退场序列完整；LayoutAppear 必须写 from/to 滑入
3. speaker 与 modelId 必须来自对照表；每条 Talk 必须带 motion 和 facial
4. models=[{"id":<所选角色modelId>,"model":"<对照表中的model路径>","normal_scale":2.1,"small_scale":1.8,"anchor":0.5}]（多角色按登场顺序排列）
5. images=[{"id":1,"image":"{chat_image}"}]
6. delay 用 0、0.05、0.1、0.15、0.2；换气的 Talk 之间 delay 取 0.1~0.2

历史对话：
{chat_history}

用户说：{scene}"""





@register("MySekaiStoryteller", "慵懒午睡", "MySekaiStoryteller 视频生成插件", "1.1.2", "https://github.com/yonglanws/astrbot_plugin_msst")
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
        self.max_concurrent_exports = config.get("max_concurrent_exports", 1)
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

    async def _call_llm(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        """通过 Astrbot 已配置的 LLM 提供商调用 AI"""
        try:
            provider = self._get_provider()
            if not provider:
                logger.error("LLM 提供商未配置")
                return None

            llm_resp = await provider.text_chat(
                prompt=prompt,
                context=[],
                system_prompt=system_prompt,
            )

            if llm_resp and llm_resp.completion_text:
                return llm_resp.completion_text.strip()

            logger.error("LLM 响应为空")
            return None

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return None

    def _strip_translation(self, translated: str) -> str:
        translated = translated.strip()
        if translated.startswith('"') and translated.endswith('"'):
            translated = translated[1:-1]
        if translated.startswith("'") and translated.endswith("'"):
            translated = translated[1:-1]
        return translated.strip()

    async def _translate_text(self, text: str, source_lang: str = "zh", target_lang: str = "ja") -> str:
        """使用 Astrbot 已配置的 LLM 提供商翻译文本"""
        if not text or not text.strip():
            return text

        try:
            provider = self._get_provider()
            if not provider:
                logger.warning("LLM 提供商未配置，跳过翻译")
                return text

            llm_resp = await provider.text_chat(
                prompt=text,
                context=[],
                system_prompt=f"You are a professional translator. Translate the following text from {source_lang} to {target_lang}. Only return the translated text, no explanations, no quotes, no additional formatting."
            )

            if llm_resp and llm_resp.completion_text:
                translated = self._strip_translation(llm_resp.completion_text)
                if translated and translated != text:
                    logger.info(f"翻译: {text[:30]}... -> {translated[:30]}...")
                    return translated

            logger.warning(f"翻译失败，使用原文: {text[:30]}...")
            return text

        except Exception as e:
            logger.warning(f"翻译异常，使用原文: {e}")
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
            llm_resp = await provider.text_chat(
                prompt=numbered,
                context=[],
                system_prompt=(
                    "You are a professional translator. Translate each numbered Chinese line to Japanese. "
                    "Return ONLY a JSON array of strings, one translation per input line, same order. "
                    "No explanations, no markdown."
                ),
            )
            translations = None
            if llm_resp and llm_resp.completion_text:
                translations = self._extract_json_from_response(llm_resp.completion_text)
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
            "Authorization": f"Bearer {api_key}"
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
        response = await client.post(url, headers=headers, json=body, timeout=120.0)

        if response.status_code != 200:
            error_text = response.text[:300]
            logger.warning(f"json_object 模式返回 HTTP {response.status_code}: {error_text}")
            body.pop("response_format", None)
            response = await client.post(url, headers=headers, json=body, timeout=120.0)
            if response.status_code != 200:
                return None

            data = response.json()
            if data.get("choices") and len(data["choices"]) > 0:
                content = data["choices"][0].get("message", {}).get("content", "")
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

    async def _build_prompt(self, scene: str) -> tuple[str, str]:
        """构建系统提示词和用户提示词（剧本模式）"""
        system_prompt = "你是视觉小说导演。只输出合法 JSON（仅含 models、images、snippets），不要 markdown 或解释。对话多少按角色人设自行把握。"

        # 刷新资源目录（自带 TTL，正常情况零开销），避免重启后一直使用兜底目录
        await self._catalog.refresh()
        view = self._catalog.view()
        registry = self._persona_registry(view)

        default_model_id = view.default_model().get("id", 1)
        second_model_id = view.models[1]["id"] if len(view.models) > 1 else default_model_id

        user_prompt = _fill_template(self.prompt_template, {
            "{character_pool}": self._build_character_pool(registry),
            "{model_table}": view.model_table(),
            "{id_mapping}": view.id_mapping(),
            "{example_models_block}": view.example_models_block([default_model_id, second_model_id]),
            "{example_bg}": view.default_image(),
            "{motion_list}": view.motion_list(),
            "{facial_list}": view.facial_list(),
            "{image_list}": view.image_list(),
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
            "{model_table}": view.model_table(),
            "{id_mapping}": view.id_mapping(),
            "{motion_list}": view.motion_list(),
            "{facial_list}": view.facial_list(),
            "{chat_image}": view.default_image(),
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
    VALID_LAYOUT_MODES = {"Normal", "Three"}

    def _validate_and_fix_story(self, story_data: dict) -> dict:
        """验证并修复 AI 生成的 JSON 格式，确保符合 MSS API 要求。尽可能自动修复，不报错。"""
        if not isinstance(story_data, dict):
            raise ValueError("剧本必须是 JSON 对象")

        view = self._catalog_view()

        # 确保 models 是数组，为空则填充默认模型
        if "models" not in story_data or not isinstance(story_data.get("models"), list) or len(story_data["models"]) == 0:
            story_data["models"] = [{"id": 1, "model": view.default_model_path(), "normal_scale": 2.1, "small_scale": 1.8, "anchor": 0.5}]
        else:
            for i, model in enumerate(story_data["models"]):
                if not isinstance(model, dict):
                    story_data["models"][i] = {"id": i + 1, "model": view.default_model_path(), "normal_scale": 2.1, "small_scale": 1.8, "anchor": 0.5}
                    continue
                model["id"] = self._to_number(model.get("id"), i + 1)
                model["model"] = self._fix_model_path(model.get("model", ""))
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
            if not isinstance(snippet, dict):
                continue

            if "type" not in snippet:
                continue

            snippet_type = snippet["type"]
            if snippet_type not in valid_types:
                logger.warning(f"Snippet {i} 的 type '{snippet_type}' 无效，已跳过")
                continue

            snippet["wait"] = self._to_bool(snippet.get("wait"), False)
            snippet["delay"] = self._to_number(snippet.get("delay"), 0)

            needs_data = snippet_type in SNIPPET_SCHEMAS and "data" in SNIPPET_SCHEMAS[snippet_type].get("properties", {})
            if needs_data and "data" not in snippet:
                snippet["data"] = {}

            if snippet_type == "Talk":
                data = snippet.get("data", {})
                default_model = view.default_model()
                data["speaker"] = self._to_str(data.get("speaker"), view.name_by_id(default_model.get("id")))
                content = self._to_str(data.get("content"), "...")
                content = content.replace("\\n", "\n")
                data["content"] = content
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
                data["motion"] = self._to_str(data.get("motion"), view.default_motion(model_id))
                data["facial"] = self._to_str(data.get("facial"), view.default_facial(model_id))
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
                data["to"]["offset"] = self._to_number(data["to"].get("offset"), 0)
                # 入场必须滑入：from 缺失时按 to.side 推导同侧外侧起点（动作与滑入并发）
                entrance_offset = {"Left": -100, "Right": 100, "Center": 0}.get(
                    data["to"]["side"], 0
                )
                if "from" not in data or not isinstance(data.get("from"), dict):
                    data["from"] = {"side": data["to"]["side"], "offset": entrance_offset}
                data["from"]["side"] = self._to_str(data["from"].get("side"), data["to"]["side"])
                if data["from"]["side"] not in self.VALID_SIDES:
                    data["from"]["side"] = data["to"]["side"]
                data["from"]["offset"] = self._to_number(data["from"].get("offset"), entrance_offset)
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
                # 退场必须滑出：to 缺失时按 from.side 推导同侧外侧终点
                exit_offset = {"Left": -100, "Right": 100, "Center": 100}.get(
                    data["from"]["side"], 100
                )
                if "to" not in data or not isinstance(data.get("to"), dict):
                    data["to"] = {"side": data["from"]["side"], "offset": exit_offset}
                data["to"]["side"] = self._to_str(data["to"].get("side"), data["from"]["side"])
                if data["to"]["side"] not in self.VALID_SIDES:
                    data["to"]["side"] = data["from"]["side"]
                data["to"]["offset"] = self._to_number(data["to"].get("offset"), exit_offset)
                # 退场动作/表情：非法值回退该角色默认（退场必须有动作，禁止无动画消失）
                clear_motion = self._clean_path(self._to_str(data.get("motion"), ""))
                if not clear_motion:
                    clear_motion = view.default_motion(model_id)
                elif clear_motion not in view.valid_motions(model_id):
                    logger.warning(f"LayoutClear motion '{clear_motion}' not available for {view.name_by_id(model_id)}, falling back to {view.default_motion(model_id)}")
                    clear_motion = view.default_motion(model_id)
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
                valid_motions = view.valid_motions(model_id)
                valid_facials = view.valid_facials(model_id)
                motion = self._to_str(data.get("motion"), view.default_motion(model_id))
                facial = self._to_str(data.get("facial"), view.default_facial(model_id))
                if valid_motions and motion not in valid_motions:
                    logger.warning(f"Motion '{motion}' not available for {view.name_by_id(model_id)}, falling back to {view.default_motion(model_id)}")
                    motion = view.default_motion(model_id)
                if valid_facials and facial not in valid_facials:
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
                data["content"] = self._to_str(data.get("content"), "")
                snippet["data"] = data

            elif snippet_type == "ChangeLayoutMode":
                data = snippet.get("data", {})
                data["mode"] = self._to_str(data.get("mode"), "Normal")
                if data["mode"] not in self.VALID_LAYOUT_MODES:
                    data["mode"] = "Normal"
                snippet["data"] = data

            elif snippet_type == "DoParam":
                data = snippet.get("data", {})
                data["modelId"] = self._to_number(data.get("modelId"), 1)
                if not isinstance(data.get("params"), list):
                    data["params"] = []
                snippet["data"] = data

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

    async def _export_video(self, story_data: dict, timeout: int = 600) -> dict:
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

    def _calculate_wait_time(self, position: int, task_type: str = "chat") -> tuple[int, int]:
        """
        计算等待时间

        Args:
            position: 队列位置
            task_type: 任务类型，"chat" 或 "story"

        Returns:
            (est_time_min, est_time_max) 分钟
        """
        if task_type == "chat":
            # 对话模式：每个任务约 1-2 分钟
            est_min = max(1, (position+1) * 1)
            est_max = max(2, (position+1) * 2)
        else:
            # 剧本模式：每个任务约 2-3 分钟
            est_min = max(1, (position+1) * 2)
            est_max = max(3, (position+1) * 3)
        
        return est_min, est_max

    @staticmethod
    def _user_error(raw) -> str:
        """把内部异常转成用户可读的短提示，避免把堆栈或内部路径发到群里。"""
        text = str(raw or "").strip()
        lower = text.lower()
        if not text or text in ("timeout",):
            return "视频生成超时，请稍后重试"
        if "排队已满" in text or "队列已满" in text:
            return "当前排队已满，请稍后再试"
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
        if len(text) > 80 or "traceback" in lower or "/" in text or "\\" in text:
            return "视频生成失败，请稍后重试"
        return f"视频生成失败：{text}"

    def _format_queue_message(self, position: int, est_min: int, est_max: int) -> str:
        """格式化排队提示信息"""
        return (
            f"使用此功能的人太多了呢..已加入队列\n"
            f"当前位置: 第 {position+1} 位\n"
            f"预计等待: {est_min}-{est_max} 分钟\n"
            f"前面还有 {position} 个任务正在排队"
        )

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

        try:
            task_id = await self.export_queue.add_task(
                user_id=user_id,
                coroutine_func=self._queued_chat_export,
                priority=1,
                kwargs={
                    "message": message,
                    "user_id": user_id,
                    "event_context": self._extract_event_context(event)
                }
            )
        except QueueFullError as e:
            yield event.plain_result("当前排队已满，请稍后再试")
            return

        # 立即显示排队状态
        queue_status = await self.export_queue.get_task_status(task_id)
        event_context = self._extract_event_context(event)
        if queue_status and queue_status.get("position") is not None and queue_status["position"] > 0:
            position = queue_status["position"]
            est_min, est_max = self._calculate_wait_time(position, "chat")
            yield event.plain_result(self._format_queue_message(position, est_min, est_max))
        else:
            yield event.plain_result(
                f"视频生成中...\n"
                f"预计等待：1-3分钟"
            )

        asyncio.create_task(self._monitor_and_send_video(task_id, event, event_context))

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

        try:
            # 立即加入队列
            task_id = await self.export_queue.add_task(
                user_id=user_id,
                coroutine_func=self._queued_story_export,
                priority=2,
                kwargs={
                    "scene": scene,
                    "sender_id": user_id,
                    "event_context": self._extract_event_context(event)
                }
            )
        except QueueFullError as e:
            queue_stats = await self.export_queue.get_queue_stats()
            pending = queue_stats.get('pending_count', 0)
            running = queue_stats.get('running_count', 0) or queue_stats.get('rendering_count', 0)

            yield event.plain_result(
                f"🚫 队列已满，暂时无法添加新任务\n\n"
                f"📊 当前队列状态:\n"
                f"⏳ 排队中: {pending} 个\n"
                f"🔄 运行中: {running} 个\n\n"
                f"💡 建议:\n"
                f"1. 等待 {max(1, pending * 30 // 60)} 分钟后再试\n"
                f"2. 使用 /mssadmin queue 查看详细队列状态\n"
                f"3. 联系管理员增加队列容量"
            )
            return

        # 立即显示排队信息
        queue_status = await self.export_queue.get_task_status(task_id)
        event_context = self._extract_event_context(event)
        if queue_status and queue_status.get("position") is not None and queue_status["position"] > 0:
            position = queue_status["position"]
            est_min, est_max = self._calculate_wait_time(position, "story")
            yield event.plain_result(self._format_queue_message(position, est_min, est_max))
        else:
            yield event.plain_result(
                f"视频生成中...\n"
                f"预计等待：1-3分钟"
            )

        asyncio.create_task(self._monitor_and_send_video(task_id, event, event_context))

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

        try:
            task_id = await self.export_queue.add_task(
                user_id=user_id,
                coroutine_func=self._queued_chat_export,
                priority=1,
                kwargs={
                    "message": message,
                    "user_id": user_id,
                    "event_context": self._extract_event_context(event)
                }
            )
        except QueueFullError as e:
            yield event.plain_result("当前排队已满，请稍后再试")
            return

        queue_status = await self.export_queue.get_task_status(task_id)
        event_context = self._extract_event_context(event)
        if queue_status and queue_status.get("position") is not None and queue_status["position"] > 0:
            position = queue_status["position"]
            est_min, est_max = self._calculate_wait_time(position, "chat")
            yield event.plain_result(self._format_queue_message(position, est_min, est_max))
        else:
            yield event.plain_result(
                f"视频生成中...\n"
                f"预计等待：1-3分钟"
            )

        asyncio.create_task(self._monitor_and_send_video(task_id, event, event_context))

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

        try:
            task_id = await self.export_queue.add_task(
                user_id=user_id,
                coroutine_func=self._queued_story_export,
                priority=2,
                kwargs={
                    "scene": scene,
                    "sender_id": user_id,
                    "event_context": self._extract_event_context(event)
                }
            )
        except QueueFullError as e:
            yield event.plain_result("当前排队已满，请稍后再试")
            return

        queue_status = await self.export_queue.get_task_status(task_id)
        event_context = self._extract_event_context(event)
        if queue_status and queue_status.get("position") is not None and queue_status["position"] > 0:
            position = queue_status["position"]
            est_min, est_max = self._calculate_wait_time(position, "story")
            yield event.plain_result(self._format_queue_message(position, est_min, est_max))
        else:
            yield event.plain_result(
                f"视频生成中...\n"
                f"预计等待：1-3分钟"
            )

        asyncio.create_task(self._monitor_and_send_video(task_id, event, event_context))

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
            result = await self.export_queue.wait_for_task(task_id, timeout=self.export_timeout + 120)

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

    async def _queued_chat_export(self, message: str, user_id: str = "", event_context: dict = None) -> dict:
        """对话模式队列任务：先生成剧本，再导出视频"""
        try:
            # LLM 生成剧本
            system_prompt, user_prompt = await self._build_chat_prompt(message, user_id)
            max_retries = 2
            story_data = None

            for attempt in range(max_retries + 1):
                llm_response = await self._call_llm_structured(user_prompt, system_prompt)
                if not llm_response:
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
                raise ValueError("AI 返回的内容无法解析为 JSON")

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

            # 调用导出函数
            return await self._queued_export_and_send(
                story_data=story_data,
                sender_id=user_id,
                description=f"{first_speaker}的回复" if first_speaker else "角色回复",
                event_context=event_context
            )
        except Exception as e:
            logger.error(f"对话生成+导出失败: {e}")
            raise

    async def _queued_story_export(self, scene: str, sender_id: str = "", event_context: dict = None) -> dict:
        """剧本模式队列任务：先生成剧本，再导出视频"""
        try:
            # LLM 生成剧本
            system_prompt, user_prompt = await self._build_prompt(scene)
            max_retries = 2
            story_data = None

            for attempt in range(max_retries + 1):
                llm_response = await self._call_llm_structured(user_prompt, system_prompt)
                if not llm_response:
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
                raise ValueError("AI 返回的内容无法解析为 JSON")

            story_data = await self._ensure_tts_text(story_data)

            # 调用导出函数
            return await self._queued_export_and_send(
                story_data=story_data,
                sender_id=sender_id,
                description=scene,
                event_context=event_context
            )
        except Exception as e:
            logger.error(f"剧本生成+导出失败: {e}")
            raise

    async def _queued_export_and_send(self, story_data: dict, sender_id: str = "", description: str = "视频", event_context: dict = None) -> dict:
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
            result = await self._export_video(story_data, self.export_timeout)

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
        images = data.get("images") or []
        bgm = data.get("bgm") or []
        lines.append(f"🖼️ 可用背景（{len(images)}）：{', '.join(images) or '无'}")
        lines.append(f"🎵 可用 BGM（{len(bgm)}）：{', '.join(bgm) or '无'}（在宿主 config.yaml 的 bgm 节启用）")
        lines.append("💡 新增模型：放入宿主 resources/models/ 并在 models.yaml 登记，约 30 秒后自动感知")
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
