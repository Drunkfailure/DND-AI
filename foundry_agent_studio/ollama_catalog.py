"""Curated Ollama library model names for UI suggestions (not exhaustive; type any name Ollama accepts)."""

# Chat / general LLM — common `ollama pull` names (no :tag defaults to latest).
SUGGESTED_CHAT_MODELS: tuple[str, ...] = (
    "llama3.2",
    "llama3.1",
    "llama3",
    "mistral",
    "mistral-nemo",
    "mixtral",
    "phi3",
    "phi3.5",
    "gemma2",
    "qwen2.5",
    "deepseek-r1",
    "codellama",
    "command-r",
    "neural-chat",
    "llama2",
    "starling-lm",
    "nous-hermes2",
    "dolphin-mixtral",
    "wizard-vicuna",
    "solar",
    "yi",
    "tinyllama",
    "falcon2",
)

# Embedding models for `/api/embeddings`.
SUGGESTED_EMBEDDING_MODELS: tuple[str, ...] = (
    "nomic-embed-text",
    "mxbai-embed-large",
    "snowflake-arctic-embed",
    "all-minilm",
)
