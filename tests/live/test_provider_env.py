"""#55: which values of `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` Claude Code treats
as true. No API cost: a truthy value sends the run to a cloud provider without credentials
(fails locally or at the provider); a falsy one sends it to the first-party API with an invalid
key (rejected with 401). The two failures are told apart by `provider_of_error`; its patterns
are guesses, so a mismatch fails with the text seen (and records it)."""

from __future__ import annotations

import pytest

from .harness import LiveEnv, provider_of_error

TRUTHY = ["1", "true", "yes", "on"]
FALSY = ["0", "false", "no", "off", "", "2", "enabled"]
PROVIDERS = {
    "CLAUDE_CODE_USE_BEDROCK": (
        "bedrock",
        {
            "AWS_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": "/dev/null",
        },
    ),
    "CLAUDE_CODE_USE_VERTEX": (
        "vertex",
        {
            "ANTHROPIC_VERTEX_PROJECT_ID": "live-test-project",
            "CLOUD_ML_REGION": "us-east5",
            "GCE_METADATA_HOST": "127.0.0.1:1",
        },
    ),
}


@pytest.mark.live
@pytest.mark.parametrize("value", TRUTHY + FALSY, ids=lambda v: repr(v))
@pytest.mark.parametrize("variable", list(PROVIDERS))
def test_cloud_provider_switch_values(cheap: LiveEnv, variable: str, value: str) -> None:
    provider, extra = PROVIDERS[variable]
    env = {
        **extra,
        variable: value,
        "ANTHROPIC_API_KEY": "sk-ant-live-test-invalid-key",  # rejected, never billed
    }
    proc = cheap.claude_run(
        ["-p", "--model", cheap.model, "--", "Reply OK."], extra_env=env, auth=False, timeout=180
    )
    text = proc.stdout + proc.stderr
    seen = provider_of_error(text)
    cheap.observe(f"{variable}={value!r}", {"classified": seen, "text": text.strip()[:200]})
    expected = provider if value in TRUTHY else "anthropic"
    assert seen == expected, text[:500]
