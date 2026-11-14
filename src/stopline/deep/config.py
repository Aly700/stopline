"""Optional provider configuration; never source the credentials file."""

from dataclasses import dataclass, field
import importlib.util
import os
from pathlib import Path
import re


PROVIDERS = (
    ('google_genai', ('GEMINI_API_KEY', 'GOOGLE_API_KEY'), 'gemini-3.1-flash-lite', 'langchain_google_genai'),
    ('anthropic', ('ANTHROPIC_API_KEY',), 'claude-haiku-5-5', 'langchain_anthropic'),
    ('openai', ('OPENAI_API_KEY',), 'gpt-6-luna', 'langchain_openai'),
)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    key: str = field(repr=False)


def read_setting(name: str) -> str:
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    try:
        with (Path.home() / '.stopline_env').open() as source:
            for line in source:
                found = re.match(r'\s*(?:export\s+)?' + re.escape(name) + r'\s*=\s*(.*?)\s*$', line)
                if found:
                    value = found.group(1)
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
                        value = value[1:-1]
                    return value.strip()
    except (OSError, UnicodeError):
        pass
    return ''


def select_model() -> ModelConfig | None:
    for provider, names, default, _ in PROVIDERS:
        for name in names:
            key = read_setting(name)
            if key:
                model = os.environ.get('STOPLINE_AGENT_MODEL', default).strip().removeprefix('models/')
                if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}', model):
                    raise ValueError('Invalid model name.')
                return ModelConfig(provider, model, key)
    return None


def model_configured() -> bool:
    return any(read_setting(name) for _, names, _, _ in PROVIDERS for name in names)


def packages_available(provider: str) -> bool:
    package = next(p for name, _, _, p in PROVIDERS if name == provider)
    try:
        return all(importlib.util.find_spec(p) is not None for p in ('deepagents', 'langchain', 'langsmith', package))
    except (ImportError, ValueError):
        return False


def make_model(config: ModelConfig):
    # SDK retries are disabled: the shared middleware retries only rate limits.
    options = dict(model=config.model, api_key=config.key, timeout=20, max_retries=0)
    if config.provider == 'google_genai':
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(**options, vertexai=False)
    if config.provider == 'anthropic':
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(**options, max_tokens=2048)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(**options, use_responses_api=False)
