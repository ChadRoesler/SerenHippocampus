"""
Config for SerenHippocampus. Follows the family convention (Memory leads):

    server:    host / port / the three bearer pointers / allow_open_lan
    memory:    where SerenMemory is, and the bearer to present to it
    model:     the small model's OpenAI-compatible endpoint
    sleep:     the cycle - interval, tend interval, thresholds, attempts

Resolution: --config -> $SEREN_HIPPOCAMPUS_CONFIG ->
~/seren-hippocampus/seren-hippocampus.yaml -> defaults. Lenient: a missing
or malformed file falls back, a bad value logs and keeps the default.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from seren_meninges import resolve_token
from seren_meninges.config import ServerConfig as _SharedServer, apply_env_overrides, read_yaml

DEFAULT_PORT = 7424          # the last slot in the brain band: 7420 Memory, 7421 Margin, 7422 Loci, 7423 Callosum
ENV_PREFIX = "SEREN_HIPPOCAMPUS"


class ServerConfig(BaseModel):
    """Pydantic twin of the shared block, like Memory's and Loci's."""
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    bearer_token: str = Field(default="", repr=False)
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""
    allow_open_lan: bool = False

    def resolve_bearer(self) -> str:
        return resolve_token(inline=self.bearer_token or None,
                             keyring_ref=self.bearer_token_keyring or None,
                             env_var=self.bearer_token_env or None)


class MemoryTarget(BaseModel):
    """SerenMemory, and the bearer the hippocampus presents to it. Same three
    pointers as every server block; `token*` accepted too, the way the
    Callosum spells a store's outbound bearer."""
    url: str = "http://127.0.0.1:7420"
    bearer_token: str = Field(default="", repr=False)
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""
    timeout_seconds: float = 30.0

    def resolve_bearer(self) -> str:
        return resolve_token(inline=self.bearer_token or None,
                             keyring_ref=self.bearer_token_keyring or None,
                             env_var=self.bearer_token_env or None)


class ModelConfig(BaseModel):
    """The small model. Empty url = mechanical mode: dockets are built from
    thresholds and verbatim flags alone, with no attach / supersede, because
    those are judgement calls."""
    url: str = "http://localhost:8090/v1"
    name: str = "default"
    timeout_seconds: int = 120
    max_tokens: int = 900


class SleepConfig(BaseModel):
    # "thread": the loops run inside this process. "external": something
    # POSTs /sleep and /tend on its own schedule (systemd timers, Lodestar).
    mode: str = "thread"
    # ~20h, deliberately not 24: the window drifts through the day.
    interval_seconds: int = 20 * 3600
    # How often denied operations are picked up and resubmitted.
    tend_interval_seconds: int = 300
    max_entries_per_run: int = 500
    # A topic cluster needs this many short-terms to be proposed at all,
    # unless a brief hint, a pin or a verbatim flag says otherwise.
    promote_min_evidence: int = 3
    # Attempts per chain before the last docket is submitted terminal (the
    # reviewer may then edit on approve).
    max_attempts: int = 3
    # How many existing cores to show the model per cluster as attach /
    # supersede candidates.
    candidate_cores: int = 5
    # Where the hippocampus keeps its own last-run record (it has no store).
    state_path: str = "~/.seren-hippocampus/state.json"


class UpdatesConfig(BaseModel):
    """"Is there a newer seren-hippocampus" checking. Core in seren-meninges,
    on by default, cosmetic, opt-outable."""
    enabled: bool = True
    check_interval_hours: float = 6.0
    index_url: str = "https://pypi.org/pypi/{distribution}/json"
    allow_prerelease: bool = False


class HippocampusConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryTarget = Field(default_factory=MemoryTarget)
    model: ModelConfig = Field(default_factory=ModelConfig)
    sleep: SleepConfig = Field(default_factory=SleepConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)

    def resolved_state_path(self) -> Path:
        p = Path(os.path.expanduser(self.sleep.state_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


_DEFAULT_CONFIG_PATH = Path.home() / "seren-hippocampus" / "seren-hippocampus.yaml"


def _resolve_config_path(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit).expanduser()
    env = os.getenv(f"{ENV_PREFIX}_CONFIG")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_CONFIG_PATH if _DEFAULT_CONFIG_PATH.exists() else None


def _server_block(raw: Any) -> ServerConfig:
    shared = _SharedServer.from_dict(raw if isinstance(raw, dict) else {}, default_port=DEFAULT_PORT)
    shared = apply_env_overrides(shared, prefix=ENV_PREFIX)
    return ServerConfig(host=shared.host, port=shared.port, bearer_token=shared.bearer_token,
                        bearer_token_env=shared.bearer_token_env,
                        bearer_token_keyring=shared.bearer_token_keyring,
                        allow_open_lan=shared.allow_open_lan)


def _memory_block(raw: Any) -> MemoryTarget:
    d = dict(raw) if isinstance(raw, dict) else {}
    # the Callosum's spelling for an outbound bearer is accepted as the same pointer
    for short, long_ in (("token", "bearer_token"), ("token_env", "bearer_token_env"),
                         ("token_keyring", "bearer_token_keyring")):
        if short in d and not d.get(long_):
            d[long_] = d.pop(short)
        else:
            d.pop(short, None)
    try:
        return MemoryTarget(**{k: v for k, v in d.items() if k in MemoryTarget.model_fields})
    except Exception as e:  # noqa: BLE001
        print(f"[seren-hippocampus] config: bad memory block ({e}); using defaults")
        return MemoryTarget()


def _block(model: type[BaseModel], raw: Any, name: str) -> BaseModel:
    d = raw if isinstance(raw, dict) else {}
    try:
        return model(**{k: v for k, v in d.items() if k in model.model_fields})
    except Exception as e:  # noqa: BLE001
        print(f"[seren-hippocampus] config: bad {name} block ({e}); using defaults")
        return model()


def load_config(explicit_path: Optional[str] = None) -> HippocampusConfig:
    path = _resolve_config_path(explicit_path)
    data: dict[str, Any] = {}
    if path is not None:
        try:
            data = read_yaml(str(path))
        except Exception as e:  # noqa: BLE001
            print(f"[seren-hippocampus] config: failed to read {path}: {e} (using defaults)")
            data = {}
    cfg = HippocampusConfig(
        server=_server_block(data.get("server")),
        memory=_memory_block(data.get("memory")),
        model=_block(ModelConfig, data.get("model"), "model"),      # type: ignore[arg-type]
        sleep=_block(SleepConfig, data.get("sleep"), "sleep"),      # type: ignore[arg-type]
        updates=_block(UpdatesConfig, data.get("updates"), "updates"),  # type: ignore[arg-type]
    )
    off = os.getenv(f"{ENV_PREFIX}_UPDATES_ENABLED")
    if off is not None:
        cfg.updates.enabled = off.strip().lower() not in ("0", "false", "no", "off")
    url = os.getenv(f"{ENV_PREFIX}_MEMORY_URL")
    if url:
        cfg.memory.url = url
    murl = os.getenv(f"{ENV_PREFIX}_MODEL_URL")
    if murl is not None:
        cfg.model.url = murl
    return cfg
