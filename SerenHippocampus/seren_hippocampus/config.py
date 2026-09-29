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
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_validator

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


class ModelLifecycleConfig(BaseModel):
    """Start the small model when it is needed, stop it when it is not.
    See seren_hippocampus.model_lifecycle for the why (a Nano floor, a 4 GB
    card). Off by default: a server that is simply left running still works."""
    manage: bool = False
    # THE EASY WAY: name the server and the model file, and the hippocampus
    # builds the command itself - <server> -m <model_path> --host/--port (both
    # from model.url, so they cannot disagree) <server_args>. Setting both
    # turns management on; `manage` need not be set. Chad, 28 Sept 2026: the
    # hand-written `start` line was the part nobody could set up without
    # help, and Starwright asked for a url and nothing else.
    server: str = ""                     # path to llama-server (or a compatible server)
    model_path: str = ""                 # path to the .gguf it serves
    server_args: str = "-ngl 99 -c 8192"   # everything else on the line
    # THE ESCAPE HATCH: a whole command line that starts the server (llama-server,
    # ollama serve ...). Wins over server/model_path when set.
    start: str = ""
    # Optional command that stops it. Blank = stop the process we started.
    stop: str = ""
    cwd: str = ""
    # Or ask this node's Observatory to start / stop a registered service.
    observatory_url: str = ""
    observatory_service: str = ""
    observatory_token: str = Field(default="", repr=False)
    observatory_token_env: str = ""
    observatory_token_keyring: str = ""
    # Blank = the model url without /v1, plus /health (llama.cpp answers there).
    health_url: str = ""
    ready_timeout_seconds: int = 240
    poll_seconds: float = 2.0
    # How long the model stays up after its last call, so a review and a
    # redraft reuse it. Stopped on the first tick after that.
    keep_warm_seconds: int = 300
    # After a failed start, wait this long before trying again.
    retry_after_seconds: int = 900

    def resolve_observatory_token(self) -> str:
        return resolve_token(inline=self.observatory_token or None,
                             keyring_ref=self.observatory_token_keyring or None,
                             env_var=self.observatory_token_env or None)


class ModelConfig(BaseModel):
    """The small model. Empty url = mechanical mode: drafts are built from
    thresholds and verbatim flags alone, with no attach / supersede, because
    those are judgement calls."""
    url: str = "http://localhost:8090/v1"
    name: str = "default"
    timeout_seconds: int = 120
    max_tokens: int = 900
    # Merged into every chat request. The default turns OFF the "thinking"
    # of Qwen3-style models (llama.cpp and vLLM honour chat_template_kwargs;
    # servers that do not know it ignore it). Seen live 25 Sept 2026: a 2B
    # model spent every token of a redraft on hidden reasoning at 4.7 tok/s,
    # hit the timeout, and never wrote the JSON. {} sends nothing extra.
    extra_body: dict = Field(default_factory=lambda: {"chat_template_kwargs": {"enable_thinking": False}})
    lifecycle: ModelLifecycleConfig = Field(default_factory=ModelLifecycleConfig)


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
    # The draft cap: attempts per chain, the last submitted terminal (the
    # reviewer may then edit on approve, and a denial ends the chain - no
    # endless draft/critique loop). Held to 1-10: 1 = one draft, no redraft.
    max_attempts: int = 3
    # How many existing cores to show the model per cluster as attach /
    # supersede candidates.
    candidate_cores: int = 5
    # Sleep at a wall-clock time instead of every interval_seconds: "03:30"
    # (local time, HH:MM). Empty keeps the drifting interval. Either way a
    # process that boots overdue sleeps after warmup_seconds, not a full
    # interval later - a month with the computer off is not a month of waiting.
    at: str = ""
    warmup_seconds: int = 300
    # A sleep that comes after a gap longer than this many intervals is a
    # CATCH-UP: it drafts and purges but does not age out or sweep, so nothing
    # that never had its chance is trashed before someone has looked.
    gap_grace_intervals: float = 3.0
    # THE BRIEF IS THE GATE. Every tick (tend_interval_seconds) the
    # hippocampus checks Memory for an open brief. One there means sleep now:
    # the draft cycle runs on it. None means wait. Once bedtime has passed
    # (interval_seconds / at, above) the misses are counted, and every
    # brief_check_misses of them the hippocampus asks: it leaves an intent in
    # Memory's near-term tier where the main model will see it, and emits a
    # brief_requested event for whatever listens on the webhook.
    brief_request: bool = True
    brief_check_misses: int = 3
    # brief_pull: with no main model to write briefs, let the small model pull
    # one from the fragments after brief_check_misses misses and sleep on it.
    # Off by design: the brief is the main model's, and it is what turns a
    # one-off into an inside joke. A headless install may turn it on.
    brief_pull: bool = False
    # Where the hippocampus keeps its own last-run record (it has no store).
    state_path: str = "~/.seren-hippocampus/state.json"

    @field_validator("max_attempts")
    @classmethod
    def _cap(cls, v: int) -> int:
        # 0 would mean no draft at all, 999 a chain that runs until the
        # reviewer gives up: both are typos, not choices.
        return max(1, min(10, int(v)))

    @field_validator("interval_seconds")
    @classmethod
    def _bedtime_floor(cls, v: int) -> int:
        # Bedtime less than ten minutes after the last sleep would ask for a
        # brief on nearly every tick.
        return max(600, int(v))


class RippleConfig(BaseModel):
    """Ask the model, don't just tell the log (seren_hippocampus.ripple has the
    why). type "" is off; "script" runs `command` (an argument list, or one
    string split like a shell would, never run BY a shell); "endpoint" POSTs
    the event and its message to `url`. `messages` overrides the default
    wording per event; {message}, {event} and {draft_id} fill the command."""
    type: str = ""
    command: Union[list[str], str] = Field(default_factory=lambda: ["claude", "-p", "{message}"])
    cwd: str = ""
    # Whose account the script runs as (seren_sinew.runas). Blank = this
    # service's own - refused when that is root or LocalSystem.
    run_as: str = ""
    # true: the message goes on the command's stdin instead of {message} - for
    # `ssh desktop claude -p` with neither Lodestar nor an Observatory, where
    # a remote shell would re-parse an argument.
    stdin: bool = False
    timeout_seconds: int = 900
    url: str = ""
    bearer_token: str = Field(default="", repr=False)
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""
    events: list[str] = Field(default_factory=lambda: ["brief_requested", "draft_submitted", "tend_resubmitted"])
    messages: dict[str, str] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v in ("", "off", "none"):
            return ""
        if v not in ("script", "endpoint"):
            raise ValueError(f"ripple.type must be script, endpoint or empty, not {v!r}")
        return v

    def resolve_bearer(self) -> str:
        return resolve_token(inline=self.bearer_token or None,
                             keyring_ref=self.bearer_token_keyring or None,
                             env_var=self.bearer_token_env or None)


class NotifyConfig(BaseModel):
    """Where to say that something happened. Events are always kept on the
    service (GET /events); a webhook_url gets each one POSTed as JSON, best
    effort, with the bearer below. This is the seed of "shoot me a text":
    Lodestar, Symposium or a messaging bridge sits at the other end."""
    webhook_url: str = ""
    bearer_token: str = Field(default="", repr=False)
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""
    # Which events go out. Everything the service emits: draft_submitted,
    # sleep_failed, sleep_done, tend_resubmitted, chain_ended, purged, catch_up.
    events: list[str] = Field(default_factory=lambda: ["draft_submitted", "sleep_failed", "chain_ended", "purged"])

    @field_validator("events")
    @classmethod
    def _renamed_events(cls, v: list[str]) -> list[str]:
        # docket_submitted until 25 Sept 2026; a config naming it still subscribes
        return ["draft_submitted" if e == "docket_submitted" else e for e in v]
    timeout_seconds: float = 10.0

    def resolve_bearer(self) -> str:
        return resolve_token(inline=self.bearer_token or None,
                             keyring_ref=self.bearer_token_keyring or None,
                             env_var=self.bearer_token_env or None)


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
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    ripple: RippleConfig = Field(default_factory=RippleConfig)
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
        notify=_block(NotifyConfig, data.get("notify"), "notify"),      # type: ignore[arg-type]
        ripple=_block(RippleConfig, data.get("ripple"), "ripple"),      # type: ignore[arg-type]
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
