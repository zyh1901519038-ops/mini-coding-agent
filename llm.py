"""LLM 客户端：OpenAI 兼容接口 + 离线 Mock（无需 API key 也能演示/测试）。

只用标准库 urllib，避免任何三方依赖。
环境变量：
    LLM_BASE_URL   默认 https://api.deepseek.com/v1
    LLM_API_KEY    没有则进入 mock 模式
    LLM_MODEL      默认 deepseek-chat
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")


class LLM:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.model = model or DEFAULT_MODEL

    @property
    def online(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """返回统一格式：{"content": str, "tool_calls": [{"id","name","arguments"}]}"""
        if not self.online:
            raise RuntimeError("未配置 LLM_API_KEY，请使用 MockLLM")

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        for attempt in range(3):  # 简单的重试
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return self._normalize(data)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    continue
                raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}") from e
            except urllib.error.URLError as e:
                if attempt < 2:
                    continue
                raise RuntimeError(f"LLM 网络错误：{e}") from e
        raise RuntimeError("LLM 调用失败")

    @staticmethod
    def _normalize(data: dict) -> dict:
        msg = data["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            args_raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {"__raw__": args_raw}  # 让工具层报错，模型自己修
            calls.append({"id": c.get("id", ""), "name": c["function"]["name"], "arguments": args})
        return {"content": msg.get("content") or "", "tool_calls": calls}


class MockLLM:
    """脚本化的假模型：按预定顺序发出工具调用，最后给结论。

    用于：① 无 API key 时演示整个 Agent 循环；② 单元测试。
    """

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls_made = 0

    online = True  # 便于 agent 统一处理

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        if self.script:
            step = self.script.pop(0)
            self.calls_made += 1
            if "final" in step:
                return {"content": step["final"], "tool_calls": []}
            return {
                "content": step.get("think", ""),
                "tool_calls": [
                    {
                        "id": f"call_{self.calls_made}",
                        "name": step["tool"],
                        "arguments": step.get("args", {}),
                    }
                ],
            }
        return {"content": "（mock 脚本已执行完毕）", "tool_calls": []}
