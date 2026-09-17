#!/usr/bin/env python3
"""提示词构建端到端测试。

用 stub 替代 astrbot 模块后加载插件主类，通过资源目录执行
_build_prompt / _build_chat_prompt，断言：
1. 输出无残留占位符
2. 对照表/动作清单/背景清单来自资源目录
3. 人格配置注入：配置的整段人设进入角色池（全名/短名均可绑定），
   未配置角色走通用演绎，无效绑定被跳过并产生告警
4. 占位符单遍替换：用户内容/人格文本中的 '{xxx}' 字样不会被二次展开
5. 聊天模式为人格池选角结构（选角规则/对照表/动作清单/退场序列）

用法：
  python scripts/test-prompt-build.py                     # 使用真实渲染宿主（默认 http://127.0.0.1:9881）
  python scripts/test-prompt-build.py http://host:9881    # 指定宿主地址
  python scripts/test-prompt-build.py --offline           # 离线模式（内置模拟目录，无需宿主）

注意：本仓库目录名需保持 astrbot_plugin_msst（插件以该包名导入）。
"""

import asyncio
import json
import re
import sys
import time
import types
from pathlib import Path

# 仓库根即插件包目录；把其父目录加入 sys.path 以支持包名导入
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT.parent))

# ---------------------------------------------------------------------------
# astrbot stub：只提供 import main.py 所需的最小表面
# ---------------------------------------------------------------------------
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")
star_mod = types.ModuleType("astrbot.api.star")
comp_mod = types.ModuleType("astrbot.api.message_components")


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass


def _passthrough_decorator(*args, **kwargs):
    def wrap(func):
        return func

    return wrap


class _FakeCommandGroup:
    """@filter.command_group 装饰器返回值：携带 .command 子装饰器"""

    def command(self, *args, **kwargs):
        return _passthrough_decorator


def _command_group_decorator(*args, **kwargs):
    def wrap(func):
        return _FakeCommandGroup()

    return wrap


event_mod.filter = types.SimpleNamespace(
    command=_passthrough_decorator,
    command_group=_command_group_decorator,
    permission_type=_passthrough_decorator,
    PermissionType=types.SimpleNamespace(ADMIN="ADMIN", MEMBER="MEMBER"),
)
event_mod.AstrMessageEvent = _Dummy
event_mod.MessageEventResult = _Dummy
event_mod.MessageChain = _Dummy
star_mod.Context = _Dummy
star_mod.Star = _Dummy
star_mod.register = _passthrough_decorator


def _noop(*args, **kwargs):
    pass


api.logger = types.SimpleNamespace(info=_noop, warning=_noop, error=_noop, debug=_noop)
api.AstrBotConfig = dict
api.event = event_mod
api.message_components = comp_mod
astrbot.api = api
astrbot.api.event = event_mod
astrbot.api.star = star_mod
astrbot.api.message_components = comp_mod

sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.api.message_components": comp_mod,
    }
)

from astrbot_plugin_msst import main  # noqa: E402
from astrbot_plugin_msst.persona import PersonaRegistry  # noqa: E402
from astrbot_plugin_msst.resource_catalog import ResourceCatalog  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{[a-z_][a-z0-9_]*\}")

# 离线模式的模拟资源目录（两个模型，覆盖全名/短名绑定与多角色场景）
OFFLINE_CATALOG = {
    "success": True,
    "models": [
        {
            "id": 1,
            "name": "晓山瑞希",
            "shortName": "瑞希",
            "path": "20mizuki/20mizuki_normal/20mizuki_normal.model3.json",
            "motions": ["w-normal-default01", "w-happy-nod01", "w-happy-glad01", "w-cute-glad01"],
            "facials": ["face_smile_01", "face_smile_02", "face_sparkling_01"],
            "defaultMotion": "w-normal-default01",
            "defaultFacial": "face_smile_01",
        },
        {
            "id": 2,
            "name": "东云绘名",
            "shortName": "绘名",
            "path": "19ena/19ena_normal/19ena_normal.model3.json",
            "motions": ["w-normal-default01", "w-cool-tilthead01"],
            "facials": ["face_normal_01", "face_smile_01"],
            "defaultMotion": "w-normal-default01",
            "defaultFacial": "face_normal_01",
        },
    ],
    "images": ["bg_test_01.jpg", "bg_test_02.jpg"],
    "imageDetails": [
        {
            "file": "bg_test_01.jpg",
            "name": "测试白天房间",
            "description": "白天阳光的房间，适合轻松日常。",
        },
        {
            "file": "bg_test_02.jpg",
            "name": "测试夜晚房间",
            "description": "夜晚台灯的房间，适合深夜倾诉。",
        },
    ],
    "voices": [],
    "bgm": [],
}


class MinimalSelf:
    """只提供 prompt 构建所需的最小宿主表面，复用插件类的真实方法。"""

    prompt_template = main.DEFAULT_PROMPT_TEMPLATE
    _cleanup_expired_sessions = main.MySekaiStorytellerPlugin._cleanup_expired_sessions
    _persona_registry = main.MySekaiStorytellerPlugin._persona_registry
    _build_character_pool = main.MySekaiStorytellerPlugin._build_character_pool
    _build_prompt = main.MySekaiStorytellerPlugin._build_prompt
    _build_chat_prompt = main.MySekaiStorytellerPlugin._build_chat_prompt

    def __init__(self, base_url: str, config: dict | None = None):
        self._catalog = ResourceCatalog(base_url)
        self.config = config if config is not None else {"personas": []}
        self.chat_history = {}
        self._user_session_timestamps = {}
        self._session_timeout_seconds = 1800
        self._history_lock = asyncio.Lock()

    def _save_chat_history(self):
        pass

    def _save_session_timestamps(self):
        pass

    def seed_offline_catalog(self):
        """预置目录数据并标记为新鲜，使 refresh() 跳过网络请求。"""
        self._catalog._data = json.loads(json.dumps(OFFLINE_CATALOG))
        self._catalog._fetched_at = time.monotonic()


async def run(base_url: str, offline: bool) -> int:
    fails = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    def make_self(config: dict | None = None) -> MinimalSelf:
        s = MinimalSelf(base_url, config=config)
        if offline:
            s.seed_offline_catalog()
        return s

    # =========================================================================
    # 场景一：空人格配置（默认状态，无人设提示词）
    # =========================================================================
    s = make_self()
    _, story_prompt = await s._build_prompt("深夜的内心独白")
    _, chat_prompt = await s._build_chat_prompt("你好呀", "test-user")
    view = s._catalog.view()
    if not view.models:
        print("FAIL: 资源目录为空，请确认宿主已启动且支持 /api/v1/resources（或使用 --offline）")
        return 1

    # --- 剧本模式 ---
    leftovers = set(PLACEHOLDER_RE.findall(story_prompt))
    check("剧本 prompt 无残留占位符", not leftovers, f"残留: {sorted(leftovers)}")
    check("剧本 prompt 含对照表", "modelId=1" in story_prompt and ".model3.json" in story_prompt)
    check("剧本 prompt 含真实模型路径", view.models[0]["path"] in story_prompt)
    check(
        "剧本 prompt 含动作清单",
        "可用动作" in story_prompt and f"{view.models[0].get('shortName', '')}(1)" in story_prompt,
    )
    check("剧本 prompt 含背景清单", (view.data.get("images") or [""])[0] in story_prompt)
    check("剧本 prompt 含背景描述并要求按场景选图", "按场景内容自行选择" in story_prompt)
    check("剧本 prompt 含场景", "深夜的内心独白" in story_prompt)
    check("剧本 prompt 示例 speaker 已泛化", '"speaker":"角色A"' in story_prompt)
    check("剧本 prompt 示例含 Talk 并发动作", '"motion":"w-happy-nod01"' in story_prompt and "说话者边说边做" in story_prompt)
    check("剧本 prompt 含退场序列规则", "退场序列" in story_prompt and '"type":"LayoutClear"' in story_prompt)
    check("剧本 prompt 示例退场带动作滑出", '"to":{"side":"Left","offset":-100},"motion"' in story_prompt)
    check("剧本 prompt 要求滑入登场", "必须写 from" in story_prompt)
    check("剧本 prompt 不限制对话条数", "单人7-10条" not in story_prompt and "每角色3-5句" not in story_prompt)
    check("剧本 prompt 不限制台词行数", "最多3行" not in story_prompt and "2～4 句" not in story_prompt)
    check("剧本 prompt 按人设把握话量", "按角色人设" in story_prompt)
    check("剧本 prompt 建议约三分钟并限制五分钟", "约 3 分钟" in story_prompt and "5 分钟" in story_prompt)
    check("剧本 prompt 限制真冬过于开心的表情", "朝比奈真冬" in story_prompt and "过于开心" in story_prompt)

    # 角色池：空配置 → 全部角色通用演绎，且无任何内置人设残留
    check(
        "剧本角色池覆盖全部目录角色（通用演绎）",
        story_prompt.count("基本档案与性格：未提供详细人设") == len(view.models),
        f"通用演绎 {story_prompt.count('基本档案与性格：未提供详细人设')} 处 / 目录 {len(view.models)} 个角色",
    )
    check("剧本 prompt 无内置档案残留（瑞希）", "伪阳角" not in story_prompt and "网名Amia" not in story_prompt)
    check("剧本 prompt 无内置档案残留（真冬表情倾向）", "倾向规则" not in story_prompt)

    # --- 聊天模式 ---
    leftovers = set(PLACEHOLDER_RE.findall(chat_prompt))
    check("聊天 prompt 无残留占位符", not leftovers, f"残留: {sorted(leftovers)}")
    check("聊天 prompt 含人格池", "人格池" in chat_prompt and "选角规则" in chat_prompt)
    check("聊天 prompt 含选角规则", "选择**1 个**" in chat_prompt)
    check("聊天 prompt 含对照表", "modelId=1" in chat_prompt)
    check("聊天 prompt 含动作清单", "可用动作" in chat_prompt)
    check("聊天 prompt 含背景清单并要求按氛围选图", "按对话氛围自行选择" in chat_prompt and (view.data.get("images") or [""])[0] in chat_prompt)
    check("聊天 prompt 含滑入登场", '"from": {"side": "Right", "offset": 100}' in chat_prompt)
    check("聊天 prompt 含退场序列", "HideTalk" in chat_prompt and '"type": "LayoutClear"' in chat_prompt)
    check("聊天 prompt 不限制对话条数", "5-8条对话" not in chat_prompt)
    check("聊天 prompt 不限制台词行数", "最多 2 个" not in chat_prompt and "最多3行" not in chat_prompt and "2～4 句" not in chat_prompt)
    check("聊天 prompt 按人设把握话量", "按角色人设" in chat_prompt)
    check("聊天 prompt 含首次对话历史", "（首次对话）" in chat_prompt)
    check("聊天 prompt 含场景", "你好呀" in chat_prompt)
    check(
        "聊天角色池为通用演绎（无人设）",
        chat_prompt.count("基本档案与性格：未提供详细人设") == len(view.models),
    )
    check("聊天 prompt 无内置瑞希人设残留", "嗨嗨！" not in chat_prompt and "Amia" not in chat_prompt)

    # =========================================================================
    # 场景二：配置人格（全名 + 短名绑定 + 无效绑定 + 坏条目）
    # =========================================================================
    config_with_personas = {
        "personas": [
            {"__template_key": "persona", "character_name": "瑞希", "prompt": "自定义人设标记MIZUKI123：元气的MV师。"},
            {"character_name": "东云绘名", "prompt": "绘名人设标记ENA456：自尊心强的画师。"},
            {"character_name": "不存在角色", "prompt": "无效人设标记DEAD789"},
            {"prompt": "缺少角色名的标记"},
            "not-a-dict",
        ]
    }
    s2 = make_self(config_with_personas)
    _, story_prompt2 = await s2._build_prompt("清晨的对话")
    _, chat_prompt2 = await s2._build_chat_prompt("早上好", "test-user")

    # 绑定断言随目录内容自适应：角色在目录中则人设必须注入，不在则必须被跳过
    mizuki_bound = any(m.get("name") == "晓山瑞希" or m.get("shortName") == "瑞希" for m in view.models)
    ena_bound = any(m.get("name") == "东云绘名" or m.get("shortName") == "绘名" for m in view.models)

    check(
        "短名绑定生效（瑞希）",
        ("自定义人设标记MIZUKI123" in story_prompt2) == mizuki_bound,
        "目录含瑞希" if mizuki_bound else "目录无瑞希（该条应被跳过）",
    )
    check(
        "全名绑定生效（东云绘名）",
        ("绘名人设标记ENA456" in story_prompt2) == ena_bound,
        "目录含绘名" if ena_bound else "目录无绘名（该条应被跳过）",
    )
    check("无效绑定的配置不进入角色池", "无效人设标记DEAD789" not in story_prompt2)
    registry2 = PersonaRegistry.from_config(config_with_personas)
    unbound = sum(1 for m in view.models if registry2.prompt_for(m) is None)
    check(
        "未配置人格的角色走通用演绎",
        story_prompt2.count("基本档案与性格：未提供详细人设") == unbound,
        f"通用演绎 {story_prompt2.count('基本档案与性格：未提供详细人设')} 处 / 未绑定 {unbound} 个角色",
    )
    check(
        "聊天模式同样注入配置人格",
        ("自定义人设标记MIZUKI123" in chat_prompt2) == mizuki_bound,
    )

    registry = PersonaRegistry.from_config(config_with_personas)
    registry.warn_unmatched(view)
    check("无效绑定产生告警", any("不在渲染宿主资源目录中" in w for w in registry.warnings))
    check("缺少角色名产生告警", any("缺少 character_name" in w for w in registry.warnings))
    check("坏条目产生告警", any("格式异常" in w for w in registry.warnings))

    # =========================================================================
    # 场景三：占位符单遍替换（注入防护）
    # =========================================================================
    config_inject = {
        "personas": [
            {"character_name": "瑞希", "prompt": "人设文本里出现占位符样字样{scene}与{model_table}结束"}
        ]
    }
    s3 = make_self(config_inject)
    tricky_scene = "场景里写了{character_pool}字样"
    _, story_prompt3 = await s3._build_prompt(tricky_scene)

    check("人格文本中的占位符字样不被二次展开", "{scene}与{model_table}结束" in story_prompt3)
    check("用户场景中的占位符字样不被二次展开", "{character_pool}字样" in story_prompt3)
    check("场景值正常注入", tricky_scene in story_prompt3)

    # =========================================================================
    # 场景四：队列并发与错误反馈
    # =========================================================================
    from astrbot_plugin_msst.queue_manager import VideoExportQueue, QueueFullError, TaskStatus

    async def _ok_job(delay=0.02):
        await asyncio.sleep(delay)
        return {"success": True}

    async def _fail_job():
        raise ValueError("boom")

    q = VideoExportQueue(max_concurrent=2, max_queue_size=4, default_timeout=2, default_max_retries=0)
    await q.start()
    try:
        await asyncio.gather(*(q.start() for _ in range(8)))
        ids = await asyncio.gather(*(q.add_task(f"u{i}", _ok_job, priority=1) for i in range(4)))
        check("并发入队不丢任务", len(set(ids)) == 4)
        try:
            await q.add_task("overflow", _ok_job)
            check("队列满时拒绝入队", False)
        except QueueFullError:
            check("队列满时拒绝入队", True)

        results = await asyncio.gather(*(q.wait_for_task(tid, timeout=3) for tid in ids))
        check("并发任务全部完成", all(r and r.get("status") == "completed" for r in results))

        fail_id = await q.add_task("fail-user", _fail_job, max_retries=0)
        fail_result = await q.wait_for_task(fail_id, timeout=3)
        check("失败任务进入 failed 终态", fail_result and fail_result.get("status") == "failed")

        retry_q = VideoExportQueue(max_concurrent=1, max_queue_size=4, default_timeout=2, default_max_retries=1)
        await retry_q.start()
        attempts = {"n": 0}

        async def _flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")
            return {"success": True}

        retry_id = await retry_q.add_task("retry-user", _flaky)
        retry_result = await retry_q.wait_for_task(retry_id, timeout=3)
        check("失败后自动重试并完成", retry_result and retry_result.get("status") == "completed" and attempts["n"] == 2)
        await retry_q.stop()

        grace_q = VideoExportQueue(
            max_concurrent=1,
            max_queue_size=2,
            default_timeout=1,
            default_max_retries=0,
            timeout_grace_seconds=2,
        )
        await grace_q.start()
        late_done = {"n": 0}

        async def _late_finish():
            await asyncio.sleep(1.4)
            late_done["n"] += 1
            return {"success": True, "localPath": "late.mp4"}

        late_id = await grace_q.add_task("late-user", _late_finish)
        late_result = await grace_q.wait_for_task(late_id, timeout=4)
        check(
            "超时宽限期内仍交付结果",
            late_result and late_result.get("status") == "completed" and late_done["n"] == 1,
        )
        await grace_q.stop()
    finally:
        await q.stop()

    check("用户错误反馈隐藏堆栈", "Traceback" not in main.MySekaiStorytellerPlugin._user_error("Traceback (most recent call last):\n  File"))
    check("用户错误反馈隐藏路径", "D:\\tmp\\x.py" not in main.MySekaiStorytellerPlugin._user_error("failed at D:\\tmp\\x.py"))
    check("JSON 失败转成可读提示", "换个说法" in main.MySekaiStorytellerPlugin._user_error("AI 返回的内容无法解析为 JSON"))

    print("\n" + ("ALL PROMPT BUILD TESTS PASSED" if not fails else f"{len(fails)} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    offline = "--offline" in args
    args = [a for a in args if a != "--offline"]
    base_url = args[0] if args else "http://127.0.0.1:9881"
    code = asyncio.run(run(base_url, offline))
    sys.exit(code)
