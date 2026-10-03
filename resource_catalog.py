"""渲染宿主资源目录客户端 —— 插件的"角色注册表"数据源。

从渲染宿主的 GET /api/v1/resources 拉取资源目录（模型/角色/动作/表情/背景/语音/BGM），
为提示词构建与故事校验提供唯一事实来源。本模块不依赖 astrbot，可独立测试。
"""

import asyncio
import re
import time

import httpx

CATALOG_TTL_SECONDS = 300
FETCH_TIMEOUT_SECONDS = 10.0
# 角色动作/表情不超过该数量时，提示词给出完整清单；过多时回退前缀分组样例，
# 避免清单撑爆提示词（实测大角色可达 200+ 动作）
SUMMARIZE_FULL_LIMIT = 60

# 目录不可用时的最小兜底（保证插件可降级工作：prompt 有锚点、校验只放行默认值）
FALLBACK_CATALOG: dict = {
    "models": [
        {
            "id": 1,
            "name": "晓山瑞希",
            "shortName": "瑞希",
            "path": "20mizuki/20mizuki_normal/20mizuki_normal.model3.json",
            "motions": [],
            "facials": [],
            "defaultMotion": "w-normal-default01",
            "defaultFacial": "face_smile_01",
        }
    ],
    "images": ["bg_e000401.jpg"],
    "imageDetails": [
        {
            "file": "bg_e000401.jpg",
            "name": "粉色房间·白天",
            "description": "少女卧室，白天阳光。适合居家闲聊、轻松日常。",
        }
    ],
    "voices": [],
    "bgm": [],
    "bgmDetails": [],
}


def _model_prefix(motion_name: str) -> str:
    """w-happy-nod01 -> w-happy-"""
    parts = motion_name.split("-")
    return "-".join(parts[:-1]) + "-" if len(parts) > 1 else motion_name


def _facial_prefix(facial_name: str) -> str:
    """face_smile_01 -> face_smile"""
    parts = facial_name.rsplit("_", 1)
    return parts[0] if len(parts) > 1 else facial_name


def _summarize_names(names: list, prefix_fn) -> str:
    """压缩长名单：少量时直接给完整名（AI 只能输出清单里出现过的完整名，
    只给样例必然编造）；超过阈值才回退"前缀（种类数）: 样例"的分组形式控制长度。"""
    if not names:
        return "（目录暂未提供）"
    if len(names) <= SUMMARIZE_FULL_LIMIT:
        return "，".join(sorted(names))
    groups: dict[str, list] = {}
    for name in names:
        groups.setdefault(prefix_fn(name), []).append(name)
    lines = []
    for prefix in sorted(groups):
        samples = "、".join(sorted(groups[prefix])[:2])
        lines.append(f"{prefix}*（{len(groups[prefix])}个，如 {samples}）")
    return "，".join(lines)


class ResourceCatalog:
    """从渲染宿主拉取并缓存资源目录。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._data: dict | None = None
        self._fetched_at: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._refresh_lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> dict:
        """拉取目录；成功刷新缓存，失败时沿用上一次数据（无数据则用兜底）。"""
        now = time.monotonic()
        if not force and self._data is not None and now - self._fetched_at < CATALOG_TTL_SECONDS:
            return self._data

        async with self._refresh_lock:
            now = time.monotonic()
            if not force and self._data is not None and now - self._fetched_at < CATALOG_TTL_SECONDS:
                return self._data
            try:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS)
                resp = await self._client.get(f"{self.base_url}/api/v1/resources")
                resp.raise_for_status()
                body = resp.json()
                if body.get("success") and isinstance(body.get("models"), list):
                    self._data = body
                    self._fetched_at = now
            except Exception:  # noqa: BLE001 —— 目录拉取失败必须静默降级
                pass

            if self._data is None:
                self._data = FALLBACK_CATALOG
                self._fetched_at = now
            return self._data

    def view(self) -> "CatalogView":
        """同步快照视图（若无数据先返回兜底；正常流程先 await refresh()）。"""
        return CatalogView(self._data or FALLBACK_CATALOG)


class CatalogView:
    """资源目录快照：prompt 构建与故事校验的统一数据面。"""

    def __init__(self, data: dict):
        self.data = data
        self.models: list[dict] = [m for m in data.get("models", []) if m.get("path")]

    # ----- 基础查询 -----

    def model_by_id(self, model_id) -> dict | None:
        for m in self.models:
            if m.get("id") == model_id:
                return m
        return None

    def default_model(self) -> dict:
        return self.models[0] if self.models else self.model_by_id(1) or {}

    def default_model_path(self) -> str:
        return self.default_model().get("path", FALLBACK_CATALOG["models"][0]["path"])

    def image_details(self) -> list[dict]:
        details = [d for d in (self.data.get("imageDetails") or []) if d.get("file")]
        if details:
            return details
        return [{"file": name, "name": name, "description": ""} for name in (self.data.get("images") or [])]

    def bgm_details(self) -> list[dict]:
        details = [d for d in (self.data.get("bgmDetails") or []) if d.get("file")]
        if details:
            return details
        return [{"file": name, "name": name, "description": ""} for name in (self.data.get("bgm") or [])]

    def default_image(self) -> str:
        details = self.image_details()
        if details:
            return details[0]["file"]
        images = self.data.get("images") or []
        return images[0] if images else FALLBACK_CATALOG["images"][0]

    def short_name(self, model: dict) -> str:
        return model.get("shortName") or model.get("name") or f"model{model.get('id')}"

    def name_by_id(self, model_id) -> str:
        m = self.model_by_id(model_id)
        return self.short_name(m) if m else f"model{model_id}"

    # ----- 校验白名单 -----

    def valid_model_paths(self) -> set:
        return {m["path"] for m in self.models}

    def valid_images(self) -> set:
        files = set(self.data.get("images") or [])
        files.update(d["file"] for d in self.image_details() if d.get("file"))
        return files

    def valid_motions(self, model_id) -> set:
        """返回该角色动作集合；空集合表示"目录未提供，不校验"。"""
        m = self.model_by_id(model_id)
        return set(m.get("motions") or []) if m else set()

    def valid_facials(self, model_id) -> set:
        m = self.model_by_id(model_id)
        return set(m.get("facials") or []) if m else set()

    def default_motion(self, model_id) -> str:
        m = self.model_by_id(model_id)
        return (m.get("defaultMotion") if m else "") or "w-normal-default01"

    def default_facial(self, model_id) -> str:
        m = self.model_by_id(model_id)
        return (m.get("defaultFacial") if m else "") or "face_smile_01"

    def canonical_animation(self, model_id, field: str, value) -> str | None:
        """把 AI 写的动作/表情名尽量修正成目录中的完整名；修不了返回 None。

        匹配顺序：精确 → 忽略大小写/空白 → 忽略尾部数字（w-happy-glad02 → w-happy-glad01，
        同名多编号时视为歧义不猜）→ 唯一前缀（AI 只写到 w-happy 且该前缀仅对应一个动作）。
        校验层据此把"必须是完整名称"的硬失败变成自动修复。
        """
        if not isinstance(value, str):
            return None
        v = value.strip()
        if not v:
            return None
        names = self.valid_motions(model_id) if field == "motion" else self.valid_facials(model_id)
        if not names:
            return None
        if v in names:
            return v
        lowered = {n.lower(): n for n in sorted(names)}
        hit = lowered.get(v.lower())
        if hit:
            return hit
        compact = {n.replace(" ", "").lower(): n for n in sorted(names)}
        hit = compact.get(v.replace(" ", "").lower())
        if hit:
            return hit
        stem = re.sub(r"\d+$", "", v.lower())
        stem_hits = [n for n in sorted(names) if re.sub(r"\d+$", "", n.lower()) == stem]
        if len(stem_hits) == 1:
            return stem_hits[0]
        prefix_hits = [n for n in sorted(names) if n.lower().startswith(v.lower())]
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        return None

    # ----- prompt 文本块 -----

    def model_table(self) -> str:
        lines = []
        for m in self.models:
            lines.append(f"- {self.short_name(m)} → modelId={m['id']}, model路径={m['path']}")
        return "\n".join(lines)

    def id_mapping(self) -> str:
        return ", ".join(f"{self.short_name(m)}={m['id']}" for m in self.models)

    def motion_list(self) -> str:
        lines = []
        for m in self.models:
            lines.append(f"{self.short_name(m)}({m['id']})：{_summarize_names(m.get('motions') or [], _model_prefix)}")
        return "\n".join(lines)

    def facial_list(self) -> str:
        lines = []
        for m in self.models:
            lines.append(f"{self.short_name(m)}({m['id']})：{_summarize_names(m.get('facials') or [], _facial_prefix)}")
        return "\n".join(lines)

    def image_list(self) -> str:
        details = self.image_details()
        if not details:
            return "（目录暂未提供背景）"
        lines = []
        for d in details:
            name = d.get("name") or d["file"]
            desc = (d.get("description") or "").strip()
            if desc:
                lines.append(f"- {d['file']}（{name}）：{desc}")
            else:
                lines.append(f"- {d['file']}（{name}）")
        return "\n".join(lines)

    def bgm_list(self) -> str:
        """BGM 提示词块：列出局点的带描述清单。

        剧本 JSON 不支持单条 story 切换 BGM，AI 通过描述了解可选曲目；
        全局默认 BGM 由宿主 audio/bgm/bgm.yaml 的 path 决定。
        """
        details = self.bgm_details()
        if not details:
            return "（目录暂未提供 BGM）"
        lines = []
        for d in details:
            name = d.get("name") or d["file"]
            desc = (d.get("description") or "").strip()
            if desc:
                lines.append(f"- {d['file']}（{name}）：{desc}")
            else:
                lines.append(f"- {d['file']}（{name}）")
        return "\n".join(lines)

    def model_ref(self, model_id) -> str:
        """角色池档案首行引用：modelId:N, model:路径"""
        m = self.model_by_id(model_id)
        if not m:
            return f"modelId:{model_id}"
        return f"modelId:{m['id']}, model:{m['path']}"

    def example_models_block(self, ids: list) -> str:
        """JSON 示例中的 models 数组内容（按给定 id 顺序）。"""
        entries = []
        for model_id in ids:
            m = self.model_by_id(model_id) or self.default_model()
            entries.append(
                '{"id":%s,"model":"%s","normal_scale":2.1,"small_scale":1.8,"anchor":0.5}'
                % (m.get("id"), m.get("path"))
            )
        return ",\n    ".join(entries)
