#!/usr/bin/env python3
"""Apply the router's decision to Hermes.

Reads everything from this repo's config.yaml, so provider endpoints and model
ids have exactly one definition. The shell wrapper (ds-switch) only parses
arguments and delegates here.

    apply.py                 apply the router's recommendation
    apply.py --show          print the decision without writing
    apply.py --off           hand routing back to the default provider
    apply.py <provider>      pin a provider explicitly
    apply.py --check         report whether the Hermes config looks consistent
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CONFIG = HERE / "config.yaml"


class ConfigError(RuntimeError):
    """config.yaml is missing, unreadable, or not the shape it claims to be."""


class ConfigReadError(RuntimeError):
    """The Hermes config could not be READ, which is not the same as a key being unset."""


def load_config() -> dict:
    """This repo's config.yaml, validated enough to fail with a message rather than
    a traceback. A stranger editing this file is the normal case, so a typo has to
    read as a diagnosis, not a stack trace."""
    import yaml

    try:
        raw = CONFIG.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {CONFIG}: {exc}") from exc
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG.name} is not valid YAML: {str(exc)[:200]}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"{CONFIG.name} must be a mapping, found {type(cfg).__name__}")
    for block_name in ("providers", "models"):
        block = cfg.get(block_name)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"{CONFIG.name}: {block_name!r} must be a mapping of names, "
                              f"found {type(block).__name__}")
        for key, entry in block.items():
            if block_name == "providers" and not isinstance(entry, dict):
                raise ConfigError(f"{CONFIG.name}: providers.{key} must be a mapping "
                                  f"(base_url, key_env), found {type(entry).__name__}")
            if block_name == "models" and not isinstance(entry, dict):
                raise ConfigError(f"{CONFIG.name}: models.{key} must map providers to model "
                                  f"ids, found {type(entry).__name__}")
    return cfg


def _run(*args: str) -> tuple[int, str, str]:
    """Run a hermes CLI command, returning (returncode, stdout, stderr)."""
    proc = subprocess.run(["hermes", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def hermes(*args: str) -> str:
    """Run a hermes CLI command that must succeed, returning stripped stdout."""
    code, out, err = _run(*args)
    if code != 0:
        raise RuntimeError(f"hermes {' '.join(args)} failed: {(err or out)[:200]}")
    return out


# Hermes answers an unset key with this notice AND a nonzero exit code, so the
# exit status alone cannot mean "the read failed". Treating it as a failure made
# every unset key a read error, which on a machine that had never been routed
# refused to write anything at all.
_NOT_SET_NOTICE = "config key not set"


def _config_get(key: str) -> str:
    """The stored value of a config key, or '' when it is genuinely unset.

    Three outcomes, deliberately distinct:
    * the value was read                     -> the value
    * the CLI said the key is not set        -> '' (an answer, not a failure)
    * anything else nonzero                  -> raise, because an unread value must
      not be confused with an empty one: that is what made the rollback restore a
      key it had never read as "unset", deleting whatever was really there.
    """
    code, out, err = _run("config", "get", key)
    notice = _NOT_SET_NOTICE in f"{out}\n{err}".lower()
    if notice:
        return ""
    if code != 0:
        raise ConfigReadError(f"hermes config get {key} failed ({(err or out)[:200]})")
    for line in reversed(out.splitlines()):
        value = line.strip()
        if value:
            return value
    return ""


def current_provider() -> str:
    """The provider Hermes is configured to use, or '' when unset."""
    try:
        return _config_get("model.provider")
    except Exception as exc:
        raise ConfigReadError(f"could not read model.provider ({exc})") from exc


def current_value(key: str) -> str:
    """The stored value of a config key, or '' when unset. Raises when unreadable."""
    try:
        return _config_get(key)
    except Exception as exc:
        raise ConfigReadError(f"could not read {key} ({exc})") from exc


def router_decision(sticky: str | None, alias: str) -> dict:
    """Ask router.py for a decision. Raises on failure."""
    args = ["./router.py", "--dry-run", "--json", "--model", alias]
    if sticky:
        # A provider whose reading failed is held rather than abandoned, so this is
        # what keeps that rule safe: when the reading is bad, one real request decides
        # whether the provider is answering at all. See --verify-sticky.
        args += ["--sticky", sticky, "--verify-sticky"]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=str(HERE))
    if proc.returncode != 0:
        raise RuntimeError(f"router exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def model_id_for(provider: str, alias: str, models: dict) -> str:
    """The id *provider* calls *alias*, or '' when it does not serve it.

    The config keeps one alias->provider->id table, so a provider serves an
    alias when models[alias][provider] is set.
    """
    table = models.get(alias) or {}
    return str(table.get(provider) or "")


# The three keys that must agree. Written together or not at all: a provider
# pointing at another provider's model id is rejected at request time with
# "Model not supported on this endpoint", which is confusing to diagnose.
_ROUTED_KEYS = ("model.provider", "model.default", "model.base_url")


def _read_before_write(key: str) -> str:
    """The value to restore if a later key fails to write.

    Deliberately raises rather than returning '': an unread value would be restored
    as "unset", which deletes whatever is really there. Better to refuse the write
    than to make the rollback destructive.
    """
    try:
        return current_value(key)
    except ConfigReadError as exc:
        raise ConfigReadError(
            f"{exc}. Refusing to write: the rollback would have to restore this key, and "
            "an unread value cannot be told apart from an empty one"
        ) from exc


def set_provider(name: str, spec: dict, alias: str, *, dry: bool,
                 models: dict | None = None) -> tuple[str, str]:
    """Write model.provider/default/base_url for one provider, as a unit.

    Returns (model_id, base_url). Raises when the alias is not served there,
    rather than writing a provider with no usable model.

    Each key is written with a separate `hermes config set` call, so any of them
    can fail part-way. If one does, the previous values are restored: leaving
    provider and model disagreeing produces a config that fails on the next
    request, and the user has no way to tell it was half-written.
    """
    model_id = model_id_for(name, alias, models or {})
    if not model_id:
        offered = ", ".join(sorted(k for k, t in (models or {}).items() if (t or {}).get(name))) or "none"
        raise RuntimeError(
            f"provider {name!r} does not serve alias {alias!r} (it serves: {offered})"
        )
    base_url = str(spec.get("base_url") or "")
    if dry:
        return model_id, base_url

    previous = {key: _read_before_write(key) for key in _ROUTED_KEYS}
    pending = [("model.provider", name), ("model.default", model_id)]
    if base_url:
        pending.append(("model.base_url", base_url))

    written: list[str] = []
    try:
        for key, value in pending:
            hermes("config", "set", key, value)
            written.append(key)
    except Exception as exc:
        restored, failed = [], []
        for key in reversed(written):
            old = previous.get(key)
            try:
                if old:
                    hermes("config", "set", key, old)
                else:
                    hermes("config", "unset", key)
                restored.append(key)
            except Exception:
                failed.append(key)
        detail = f"restored {', '.join(restored)}" if restored else "nothing needed restoring"
        if failed:
            detail += f"; COULD NOT RESTORE {', '.join(failed)}"
        raise RuntimeError(
            f"writing {name!r} failed after {len(written)} of {len(pending)} keys "
            f"({exc}); {detail}. Check `hermes config get model.provider`."
        ) from exc
    return model_id, base_url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", help="pin a provider explicitly")
    ap.add_argument("--show", action="store_true", help="print the decision, write nothing")
    ap.add_argument("--off", action="store_true", help="hand routing back to the default provider")
    ap.add_argument("--check", action="store_true", help="report config consistency, write nothing")
    ap.add_argument("--alias", default=None, help="model alias to route")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    providers = cfg.get("providers") or {}
    models = cfg.get("models") or {}
    alias = args.alias or cfg.get("default_model")
    default_provider = str(cfg.get("default_provider") or "")

    if not providers:
        print("No providers declared in config.yaml", file=sys.stderr)
        return 1

    if args.check:
        return check(cfg, providers, models, alias)

    if args.off:
        if not default_provider or default_provider not in providers:
            print("config.yaml needs a 'default_provider' naming one of: "
                  + ", ".join(providers), file=sys.stderr)
            return 2
        try:
            model_id, _ = set_provider(default_provider, providers[default_provider], alias,
                                       dry=args.show, models=models)
        except ConfigReadError as exc:
            print(str(exc), file=sys.stderr)
            return 6
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 5
        print(f"Routing off — Hermes is back on {default_provider} ({model_id})."
              + ("  [--show: nothing written]" if args.show else ""))
        return 0

    if args.provider:
        chosen, reason = args.provider, "pinned by hand"
        if chosen not in providers:
            print(f"Unknown provider: {chosen}", file=sys.stderr)
            print("Known: " + ", ".join(providers), file=sys.stderr)
            return 2
    else:
        # Stay on whatever Hermes already uses unless there is a real reason to
        # move: each move rebuilds the agent and resets the prompt cache.
        try:
            sticky = current_provider()
        except ConfigReadError as exc:
            print(f"{exc}; leaving the Hermes config untouched. A sticky provider that "
                  "cannot be read is not the same as one that is unset, and acting as if "
                  "it were would move a healthy conversation.", file=sys.stderr)
            return 6
        if sticky not in providers:
            sticky = None
        try:
            decision = router_decision(sticky, alias)
        except Exception as exc:
            print(f"Router failed to produce a decision ({exc}); leaving Hermes config untouched.",
                  file=sys.stderr)
            return 3
        chosen = decision.get("chosen") or ""
        reason = decision.get("reason") or ""
        if not chosen:
            print(f"Router refused to choose: {reason}", file=sys.stderr)
            print("Leaving Hermes config untouched.", file=sys.stderr)
            return 4

    try:
        model_id, _ = set_provider(chosen, providers[chosen], alias, dry=args.show, models=models)
    except ConfigReadError as exc:
        print(str(exc), file=sys.stderr)
        return 6
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 5

    if args.show:
        print(f"Would set: provider={chosen} model={model_id}")
        print(f"Reason:    {reason}")
        return 0

    print(f"Routed to {chosen} ({model_id})")
    print(f"  reason: {reason}")
    return 0


def check(cfg: dict, providers: dict, models: dict, alias: str) -> int:
    """Report whether the config can actually drive Hermes. Writes nothing."""
    problems, notes = [], []
    unset_keys = 0
    for name, spec in providers.items():
        if not spec.get("base_url"):
            problems.append(f"{name}: no base_url")
        if not spec.get("key_env"):
            problems.append(f"{name}: no key_env")
        elif not _env_present(str(spec["key_env"])):
            unset_keys += 1
            notes.append(f"{name}: key_env {spec['key_env']} is not set in this shell "
                         f"(fine if Hermes loads it from .env)")
        model_id = model_id_for(name, alias, models)
        if not model_id:
            notes.append(f"{name}: does not serve alias {alias!r}")
        else:
            notes.append(f"{name}: {alias} -> {model_id}")

    default_provider = str(cfg.get("default_provider") or "")
    if default_provider not in providers:
        problems.append(f"default_provider {default_provider!r} is not one of: {', '.join(providers)}")

    seen = str(cfg.get("default_model") or "")
    if seen != alias:
        problems.append(f"default_model {seen!r} != alias {alias!r} (router runs with {alias!r})")

    print(f"alias            : {alias}")
    print(f"default_provider : {default_provider or '(unset)'}")
    print(f"providers        : {len(providers)} ({', '.join(providers)})")
    for note in notes:
        print(f"  note  {note}")
    for problem in problems:
        print(f"  ERROR {problem}")
    if problems:
        print(f"{len(problems)} problem(s)")
    elif providers and unset_keys == len(providers):
        # Consistent, but nothing can be fetched: install.sh repeats this line, and a
        # bare "config OK" over four "key is not set" notes reads as "ready to route".
        from paths import env_file
        print(f"config OK, but no provider key is set in this shell or in "
              f"{env_file()}: routing will not work until one is")
    else:
        print("config OK")
    return 0 if not problems else 1


def _env_present(name: str) -> bool:
    import os
    from paths import env_file
    if os.environ.get(name):
        return True
    try:
        return any(line.split("=", 1)[0].strip() == name
                   for line in env_file().read_text().splitlines()
                   if "=" in line and not line.strip().startswith("#"))
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
