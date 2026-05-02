"""Prompts module — system skeletons + per-trigger-kind playbooks.

PROMPT_VERSION is the single source of truth for the prompt revision used
on every compose. Bumping it invalidates the response cache for affected
entries (cache key includes prompt_version). Bump on any prompt change.
"""

PROMPT_VERSION = "v5"
