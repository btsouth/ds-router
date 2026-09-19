"""Validate user-editable structure before consumers access nested values."""

import math


def validate(cfg):
    def mapping(value, name):
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{name} must be a mapping")
        return value or {}

    mapping(cfg, "config")
    for block in ("providers", "models"):
        for name, value in mapping(cfg.get(block), block).items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{block} names must be nonempty strings")
            mapping(value, f"{block}.{name}")
            if block == "providers" and value is None:
                raise ValueError(f"providers.{name} must be a mapping")
            if block == "models":
                for provider, model in (value or {}).items():
                    if not isinstance(provider, str) or (model is not None and not isinstance(model, str)):
                        raise ValueError(f"models.{name} must map provider names to model ID strings")
            else:
                for key in ("base_url", "key_env", "quota", "session_header"):
                    if key in value and value[key] is not None and not isinstance(value[key], str):
                        raise ValueError(f"providers.{name}.{key} must be a string")
    for key in ("default_model", "default_provider"):
        if key in cfg and not isinstance(cfg[key], str):
            raise ValueError(f"{key} must be a string")
    routing = mapping(cfg.get("routing"), "routing")
    conc = mapping(routing.get("concurrency"), "routing.concurrency")
    weights = mapping(routing.get("window_weights"), "routing.window_weights")
    for block, key in ((routing, "reuse_collector_state"), (conc, "enabled")):
        if key in block and not isinstance(block[key], bool):
            raise ValueError(f"routing.{key} must be true or false")
    for block, keys in ((routing, ("skip_at", "quota_ttl_seconds")),
                        (conc, ("pressure_per_over", "max_pressure")),
                        (weights, tuple(weights))):
        for key in keys:
            if key not in block:
                continue
            try:
                number = float(block[key])
            except (TypeError, ValueError):
                raise ValueError(f"routing.{key} must be a finite nonnegative number") from None
            if isinstance(block[key], bool) or not math.isfinite(number) or number < 0:
                raise ValueError(f"routing.{key} must be a finite nonnegative number")
            # Consumers should receive numbers, including when YAML quotes them.
            block[key] = number
    if "skip_at" in routing and not 0 < routing["skip_at"] <= 1:
        raise ValueError("routing.skip_at must be greater than zero and at most one")
    peak = mapping(cfg.get("peak"), "peak")
    mapping(peak.get("windows"), "peak.windows")
    mapping(cfg.get("gateway"), "gateway")
    return cfg
