#!/usr/bin/env python3
"""Shared registry lookups for free-stealth-bench.

A *provider* (providers.yaml) binds an endpoint and a model list to a default
*harness*; the harness (harnesses.yaml) is the agent that actually ran the task.
Sidecars written before harness support have no provider/harness/target fields,
so both judge.py and leaderboard.py have to reconstruct them. Take the harness
from the provider's declared default harness — never reuse the provider id as
the harness, or a provider such as `openrouter` (default harness `openai`)
would show up in the harness list as if it were an agent.
"""
import os
import sys
from functools import lru_cache

ROOT = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_FILE = (os.environ.get('PROVIDERS_FILE')
                  or os.environ.get('BENCH_PROVIDERS_FILE')
                  or os.path.join(ROOT, 'providers.yaml'))


@lru_cache(maxsize=1)
def provider_harness_map():
    """Return {provider_id: default_harness_id} from providers.yaml.

    Falls back to an empty mapping (with a warning) when PyYAML or the registry
    is unavailable, so the judge and leaderboard still build using their
    historical fallback instead of crashing.
    """
    try:
        import yaml
    except ImportError:
        print('bench_config: PyYAML not installed; provider->harness inference '
              'unavailable (pip install pyyaml)', file=sys.stderr)
        return {}
    try:
        with open(PROVIDERS_FILE, encoding='utf-8') as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - keep builds resilient
        print(f'bench_config: cannot read {PROVIDERS_FILE}: {exc}', file=sys.stderr)
        return {}
    providers = doc.get('providers') if isinstance(doc, dict) else None
    if not isinstance(providers, dict):
        return {}
    out = {}
    for pid, cfg in providers.items():
        if isinstance(cfg, dict):
            out[str(pid)] = str(cfg.get('default_harness') or cfg.get('harness') or pid)
    return out


def infer_harness(provider, recorded=None):
    """Harness id for a run: recorded value if present, else provider's default.

    `provider` is only used as a last-resort fallback for providers missing from
    the registry; in that case we cannot know which agent was used.
    """
    if recorded:
        return str(recorded)
    return provider_harness_map().get(str(provider or '')) or str(provider or '')
