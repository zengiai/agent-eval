"""LLM-as-Judge 客户端 + Prompt 模板管理器。

为评测器提供 LLM 调用能力，支持：
  - 从 YAML 模板加载、缓存、渲染 Prompt
  - OpenAI 兼容 API 调用（支持 DashScope / DeepSeek 等兼容服务）
  - 自动重试 + 指数退避
  - JSON 响应解析（自动剥离 markdown 代码块）
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional

import httpx
import yaml

logger = logging.getLogger(__name__)

# ── 默认 LLM Judge 配置 ─────────────────────────────────────────
DEFAULT_MODEL = "qwen3.7-max"
DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 1024


class PromptManager:
    """Prompt 模板管理器。

    从 YAML 文件加载模板，支持缓存和变量注入。
    模板路径相对於 prompts_dir。
    """

    def __init__(self, prompts_dir: Optional[str] = None):
        if prompts_dir is None:
            prompts_dir = str(Path(__file__).resolve().parent.parent / "evaluators" / "prompts")
        self._prompts_dir = Path(prompts_dir)
        self._cache: Dict[str, Dict] = {}

    def load(self, prompt_path: str) -> Dict:
        """加载 prompt 模板（相对路径，不含 .yaml 后缀）。

        例: load("generation/factual_accuracy")
        """
        if prompt_path in self._cache:
            return self._cache[prompt_path]

        full_path = self._prompts_dir / f"{prompt_path}.yaml"
        if not full_path.exists():
            raise FileNotFoundError(f"Prompt 模板不存在: {full_path}")

        with open(full_path, "r", encoding="utf-8") as f:
            template = yaml.safe_load(f)

        self._cache[prompt_path] = template
        return template

    def render(self, prompt_path: str, variables: Dict[str, Any]) -> Dict[str, str]:
        """渲染模板为 system / user 消息。

        Returns:
            {"system": "...", "user": "..."}
        """
        template = self.load(prompt_path)
        system = template.get("system_prompt", "")
        user_tpl = template.get("user_prompt_template", "")
        user = user_tpl.format(**variables)
        return {"system": system, "user": user}

    def get_metadata(self, prompt_path: str) -> Dict:
        """获取模板元信息（model, temperature, max_tokens 等）。"""
        template = self.load(prompt_path)
        return {
            "version": template.get("version", "1.0.0"),
            "model": template.get("model", DEFAULT_MODEL),
            "temperature": template.get("temperature", DEFAULT_TEMPERATURE),
            "max_tokens": template.get("max_tokens", DEFAULT_MAX_TOKENS),
        }


class LLMJudge:
    """LLM-as-Judge 客户端。

    封装 OpenAI 兼容 API 调用，提供：
      - judge()：单次调用 + JSON 解析
      - judge_with_retry()：带重试的调用
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
        prompt_manager: Optional[PromptManager] = None,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._max_retries = max_retries
        self._timeout = timeout
        self._prompts = prompt_manager or PromptManager()

    @property
    def prompts(self) -> PromptManager:
        return self._prompts

    def judge(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """单次 LLM Judge 调用，解析 JSON 响应。

        Returns:
            解析后的 JSON dict，包含 _raw 字段存放原始响应文本。
        """
        temp = temperature if temperature is not None else self._temperature

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temp,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        url = f"{self._base_url}/chat/completions"
        logger.debug("LLM Judge 调用: model=%s, url=%s", self._model, url)

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        raw_content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        parsed = _parse_json_response(raw_content)
        parsed["_raw"] = raw_content
        parsed["_usage"] = usage
        return parsed

    def judge_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """带指数退避重试的 LLM Judge 调用。"""
        last_error = None
        for attempt in range(self._max_retries):
            try:
                return self.judge(system_prompt, user_prompt, temperature, max_tokens)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "LLM Judge 调用失败 (attempt %d/%d)，%ds 后重试: %s",
                        attempt + 1, self._max_retries, wait, e,
                    )
                    time.sleep(wait)
                else:
                    logger.error("LLM Judge 调用最终失败: %s", e)

        raise RuntimeError(f"LLM Judge 调用失败（{self._max_retries} 次重试后）: {last_error}")

    def judge_by_template(
        self,
        prompt_path: str,
        variables: Dict[str, Any],
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """通过模板路径 + 变量直接完成一次 Judge 调用。

        例:
            judge_by_template("generation/factual_accuracy", {
                "query": "今天天气如何",
                "response": "今天晴天",
                "gold_answer": "今天晴，气温 25°C",
            })
        """
        rendered = self._prompts.render(prompt_path, variables)
        meta = self._prompts.get_metadata(prompt_path)
        return self.judge_with_retry(
            system_prompt=rendered["system"],
            user_prompt=rendered["user"],
            temperature=temperature if temperature is not None else meta["temperature"],
            max_tokens=meta["max_tokens"],
        )

    def is_available(self) -> bool:
        """检查 LLM Judge 是否可用（已配置 API Key）。"""
        return bool(self._api_key)


# ── 辅助函数 ────────────────────────────────────────────────────

def _parse_json_response(raw: str) -> Dict[str, Any]:
    """从 LLM 返回文本中提取 JSON。

    处理常见格式：
      - 纯 JSON: {"key": "value"}
      - Markdown 代码块: ```json ... ```
      - 无标记代码块: ``` ... ```
    """
    text = raw.strip()

    # 策略 1：提取 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return json.loads(match.group(1))

    # 策略 2：提取第一个 { 到最后一个 }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return json.loads(text[first_brace:last_brace + 1])

    # 策略 3：直接解析全文
    return json.loads(text)
