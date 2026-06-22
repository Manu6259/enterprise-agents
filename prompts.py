"""Prompt loader — versioned system prompts per agent.

Each agent has a folder under ``prompts/<agent_name>/`` containing one
markdown file per version (``v1.md``, ``v2.md``, ...). The agent picks
which version to use via a ``PROMPT_VERSION`` constant in its own code:

    from prompts import load_prompt

    PROMPT_VERSION = "v2"
    SYSTEM_PROMPT = load_prompt("sales_intelligence", PROMPT_VERSION)

Why a separate module:
  * One canonical place to resolve filesystem paths — no surprises when
    a script is run from a different working directory.
  * Cheap caching so importing prompts in multiple agents doesn't re-read
    files for every request.
  * Loud, early errors (FileNotFoundError on import) rather than silent
    fallback to a missing or stale prompt at request time.

To introduce a new prompt version: drop a new ``vN.md`` file alongside
the existing ones and bump the ``PROMPT_VERSION`` constant in the agent.
``git log -- prompts/<agent>/`` is then the audit trail.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=64)
def load_prompt(agent_name: str, version: str) -> str:
    """Read ``prompts/<agent_name>/<version>.md`` and return its content.

    Raises FileNotFoundError if the version doesn't exist — preferable
    to silently returning empty text, which would produce a broken
    agent that's hard to diagnose.
    """
    path = _PROMPTS_DIR / agent_name / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt not found: {path}. "
            f"Expected versions under {_PROMPTS_DIR / agent_name}/."
        )
    return path.read_text(encoding="utf-8").strip()


def list_versions(agent_name: str) -> list[str]:
    """Return every available version for an agent, sorted lexically.

    Useful for eval runners that want to sweep across all known prompt
    versions. Returns an empty list if the agent folder is missing.
    """
    agent_dir = _PROMPTS_DIR / agent_name
    if not agent_dir.is_dir():
        return []
    return sorted(p.stem for p in agent_dir.glob("v*.md"))
