"""Central configuration: ignore patterns, allowed extensions, output paths, LLM settings."""

from __future__ import annotations

import json
import os
from pathlib import Path

MEMORY_DIR_NAME = ".memory"
CMSIGNORE_FILENAME = ".cmsignore"

# gitignore-style patterns (spec section 4). Applied before the extension whitelist.
DEFAULT_IGNORES: list[str] = [
    # Version control
    ".git/",
    ".gitignore",
    ".gitattributes",
    # Python
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".Python",
    "build/",
    "develop-eggs/",
    "dist/",
    "downloads/",
    "eggs/",
    ".eggs/",
    "lib/",
    "lib64/",
    "parts/",
    "sdist/",
    "var/",
    "wheels/",
    "*.egg-info/",
    ".installed.cfg",
    "*.egg",
    "venv/",
    ".venv/",
    "env/",
    ".env/",
    "ENV/",
    "env.bak/",
    "venv.bak/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".coverage",
    "htmlcov/",
    ".tox/",
    ".nox/",
    ".pytype/",
    # Node / JS / TS
    "node_modules/",
    "npm-debug.log*",
    "yarn-debug.log*",
    "yarn-error.log*",
    ".pnp/",
    ".pnp.js",
    ".yarn/",
    "bower_components/",
    # Build / output
    "out/",
    "*.min.js",
    "*.map",
    ".next/",
    ".nuxt/",
    ".cache/",
    # IDE / editor
    ".vscode/",
    ".idea/",
    "*.swp",
    "*.swo",
    "*~",
    ".project",
    ".classpath",
    ".settings/",
    "*.sublime-project",
    "*.sublime-workspace",
    # OS
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Logs & temp
    "*.log",
    "*.tmp",
    "*.temp",
    "logs/",
    "tmp/",
    "temp/",
    # Generated build output committed to the repo (dist/ is above; these cover
    # the common `dist-<target>` convention — dist-lib, dist-electron, dist-ssr…)
    "dist-*/",
    ".output/",
    ".svelte-kit/",
    ".astro/",
    "storybook-static/",
    # Dependency lockfiles — generated, often committed, never useful to analyse
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    # The memory system's own output, workspace link, scope + agent config
    ".memory/",
    ".cms/",
    ".claude/",
    "cms.workspace.json",
    ".cmsscope.json",
    # Library published-version snapshots (immutable copies, not source)
    ".versions/",
]

# extension -> language name; also acts as the inclusion whitelist
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".md": "markdown",
    ".txt": "text",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "config",
    ".cfg": "config",
    ".sh": "shell",
    ".bash": "shell",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript-react",
    ".tsx": "typescript-react",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
}

# --- User config file (simple place to enter API keys) ---------------------

USER_CONFIG_PATH = Path.home() / ".cms" / "config.json"

# config-file key -> environment variable it feeds
CONFIG_KEYS: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "anthropic_model": "CMS_ANTHROPIC_MODEL",
    "provider": "CMS_PROVIDER",
    "openai_api_key": "CMS_OPENAI_API_KEY",
    "openai_base_url": "CMS_OPENAI_BASE_URL",
    "openai_model": "CMS_OPENAI_MODEL",
}


def load_user_config() -> dict:
    try:
        return json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_config(cfg: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def apply_user_config() -> None:
    """Feed config-file values into the environment; real env vars win."""
    cfg = load_user_config()
    for key, env_var in CONFIG_KEYS.items():
        value = cfg.get(key)
        if value and env_var not in os.environ:
            os.environ[env_var] = str(value)


apply_user_config()

# --- Feature flags -----------------------------------------------------------
# Comprehension-layer surfaces can be disabled at runtime without code changes
# (rollback lever). Unset or anything but 0/false/off means enabled.

FEATURE_FLAGS: dict[str, str] = {
    "human_view": "CMS_HUMAN_VIEW",
    "annotations": "CMS_ANNOTATIONS",
    "flow_review": "CMS_FLOW_REVIEW",
}


def flags() -> dict[str, bool]:
    return {name: os.environ.get(env, "1").strip().lower() not in ("0", "false", "off")
            for name, env in FEATURE_FLAGS.items()}


# --- LLM / summary settings ------------------------------------------------

# Provider selection: "anthropic" | "openai" | "mock".
# Unset -> anthropic if ANTHROPIC_API_KEY is present, else mock.
ENV_PROVIDER = "CMS_PROVIDER"

ANTHROPIC_MODEL = os.environ.get("CMS_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_BASE_URL = os.environ.get("CMS_OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_MODEL = os.environ.get("CMS_OPENAI_MODEL", "llama3.1")

MAX_TOKENS = 1024
# Source larger than this gets head+tail truncated before being sent to the LLM.
MAX_SOURCE_CHARS = 16_000

# --- Library (reusable agent-context assets) --------------------------------

# Project-scope assets live in a visible, git-tracked dir at the project root.
LIBRARY_DIR_NAME = "skills"
# User-scope assets are shared across every project on this machine.
LIBRARY_USER_DIR = Path.home() / ".cms" / "library"
# Composed context larger than this raises the `oversized` warning (~6k tokens).
LIBRARY_WARN_CHARS = 24_000


def library_builtin_dir() -> Path:
    """The read-only built-in asset dir shipped with Atlas.

    Resolves to the Atlas checkout's own skills/ dir; CMS_LIBRARY_BUILTIN
    overrides (tests, packaged installs). A missing dir simply yields an
    empty built-in scope.
    """
    override = os.environ.get("CMS_LIBRARY_BUILTIN")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / LIBRARY_DIR_NAME
