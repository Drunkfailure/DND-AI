"""Optional LLM classifiers for whether to persist short-term / long-term memory."""

from __future__ import annotations

from foundry_agent_studio.db import Agent
from foundry_agent_studio.ollama_client import chat_completion


async def should_persist_stm_exchange(
    base: str, agent: Agent, user_line: str, assistant_line: str
) -> bool:
    policy = (agent.memory_stm_filter or "").strip()
    if not policy:
        return True
    prompt = (
        f"Policy for what belongs in short-term (recent) memory:\n{policy}\n\n"
        f"User: {user_line}\nCharacter: {assistant_line}\n\n"
        "Should this exchange be stored in short-term memory? Answer only YES or NO."
    )
    try:
        reply = await chat_completion(base, agent.model, 0.0, [("user", prompt)])
    except Exception:
        return True
    return "yes" in reply.lower()[:80]


async def should_run_ltm_semantic(
    base: str, agent: Agent, user_line: str, assistant_line: str
) -> bool:
    if not agent.memory_long_term_enabled:
        return False
    policy = (agent.memory_ltm_filter or "").strip()
    if not policy:
        return True
    prompt = (
        f"Policy for long-term (persistent) character memory:\n{policy}\n\n"
        f"User: {user_line}\nCharacter: {assistant_line}\n\n"
        "Should this exchange be summarized into long-term memory? Answer only YES or NO."
    )
    try:
        reply = await chat_completion(base, agent.model, 0.0, [("user", prompt)])
    except Exception:
        return True
    return "yes" in reply.lower()[:80]
