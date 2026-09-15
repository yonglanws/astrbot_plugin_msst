"""人格配置解析与绑定 —— 自定义提示词的统一数据源。

从插件配置（_conf_schema.json 的 personas，template_list 类型）读取
"角色名 -> 整段人设提示词"映射，并与渲染宿主资源目录中的模型条目绑定。
本模块不依赖 astrbot，可独立测试；告警由调用方转发到 astrbot logger。

设计约定：默认（未配置任何人格）不注入任何内置人设，未配置的角色
由调用方使用通用演绎兜底文本。
"""

from dataclasses import dataclass

PERSONA_CONFIG_KEY = "personas"


@dataclass
class PersonaBinding:
    """人格配置与资源目录模型的绑定结果。"""

    model: dict
    """资源目录中的模型条目（含 id/name/shortName/path）"""

    prompt: str | None
    """该角色配置的整段人设提示词；None 表示未配置"""


class PersonaRegistry:
    """人格注册表：配置解析 + 目录绑定 + 人设查询。

    生命周期：每次构建提示词时 from_config() 现读现解析（配置即 dict，开销可忽略），
    WebUI 修改配置并重载插件后自然生效，无需缓存失效逻辑。
    解析/绑定过程中的问题统一收集在 warnings，由调用方决定如何呈现。
    """

    def __init__(self):
        # key 为配置里的角色名原样保留（全名或短名均可命中）
        self._prompts: dict[str, str] = {}
        self.warnings: list[str] = []

    @classmethod
    def from_config(cls, config) -> "PersonaRegistry":
        """从插件配置构建注册表；容错解析，坏条目跳过并记告警。"""
        registry = cls()
        raw = config.get(PERSONA_CONFIG_KEY) if isinstance(config, dict) else None
        if raw is None:
            return registry
        if not isinstance(raw, list):
            registry.warnings.append("personas 配置格式异常（应为列表），已忽略全部人格配置")
            return registry

        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                registry.warnings.append(f"personas 第 {i + 1} 条格式异常（应为对象），已跳过")
                continue
            name = str(item.get("character_name") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not name:
                registry.warnings.append(f"personas 第 {i + 1} 条缺少 character_name，已跳过")
                continue
            if name in registry._prompts:
                registry.warnings.append(f"角色 '{name}' 配置了多条人格，仅保留第一条")
                continue
            if not prompt:
                registry.warnings.append(f"角色 '{name}' 的人设提示词为空，该角色将走通用演绎")
            registry._prompts[name] = prompt
        return registry

    def warn_unmatched(self, view) -> None:
        """把无法匹配到资源目录的角色名追加到 warnings（全名/短名均不命中）。"""
        known: set = set()
        for m in view.models:
            known.add(m.get("name"))
            known.add(m.get("shortName"))
        for name in self._prompts:
            if name not in known:
                self.warnings.append(
                    f"人格绑定的角色 '{name}' 不在渲染宿主资源目录中，该条配置不生效"
                )

    def prompt_for(self, model: dict) -> str | None:
        """查询某目录模型的人设 prompt（配置角色名与全名或短名一致即命中）。

        未配置返回 None，调用方使用通用演绎兜底。
        """
        for key in (model.get("name"), model.get("shortName")):
            if key and key in self._prompts:
                prompt = self._prompts[key]
                return prompt or None
        return None

    def resolve(self, view) -> list[PersonaBinding]:
        """返回全部目录模型的绑定视图（含未配置角色），并顺带记录未命中告警。"""
        self.warn_unmatched(view)
        return [PersonaBinding(model=m, prompt=self.prompt_for(m)) for m in view.models]
