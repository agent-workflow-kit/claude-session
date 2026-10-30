"""claude-session: runs and supervises Claude Code sessions."""

from __future__ import annotations


def __getattr__(name: str) -> str:
    # The version comes from the installed package metadata (pyproject.toml is the single source).
    # It is looked up lazily so the hook path does not pay for importlib.metadata.
    if name == "__version__":
        from importlib.metadata import version

        return version("claude-session")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
