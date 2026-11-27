"""One catalog of every model this computer can reach, and the settings that choose one.

  agent models  claude/claude-code/opus, codex/codex/<m>, pi/<provider>/<m>   (agents.py, scanned at startup)
  custom agents custom/<key>/<m>: a command the reader added in the settings, run as one of the agents (agents.py)
  API models    <model id> from an env provider or a provider added in the settings (providers.py)

An API id that two providers share belongs to the env provider. A saved choice includes its destination,
so discovery can never redirect document text to a different provider with the same model id.
"""
from contextvars import copy_context
import hashlib
import json
import os
import stat
import warnings

from . import agents, bridges, chatgpt, local_access, providers
from ._storage_layout import DirectoryFiles
from .store import HOME, OLD_HOMES, read_text

SETTINGS = HOME / "settings.json"
# How much the model may think, per task, in levels every caller maps onto its own (llm.py). Not a setting: a reader is
# here to read. Measured on one paper (2026-10-04): thinking adds time, not quality, to the levels and the
# translation, while the links need some.
TASKS = ("ladder", "links", "translate", "ask")
LEVELS = ("default", "off", "low", "medium", "high")
DEFAULT_THINKING = {"ladder": "off", "links": "low", "translate": "off", "ask": "low"}
LEGACY_ALIASES = {"fable", "opus", "sonnet", "haiku"}
NO_MODEL = "No model is available: choose a model and its provider in Settings"
# the Anthropic API spoken natively (official SDK), offered as one more preset in the settings
ANTHROPIC_PRESET = {"id": "anthropic", "name": "Anthropic", "base": "https://api.anthropic.com", "protocol": "anthropic",
                    "fixed": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"], "keyUrl": "https://console.anthropic.com/settings/keys"}


def load_settings():
    s = {"model": None, "model_target": None, "ask_model": None, "ask_model_target": None, "ask_effort": None,
         "efforts": {}, "providers": [], "custom_agents": [], "use_thoughtdag_env": False,
         "pdf_tier": "fast", "pdf_online": True, "engine_path": None, "zotero": {},
         "lang": None}   # the reader's language as the page last told it (the page keeps its own: pf-ui-lang), for other programs
    old_home = next((h for h in OLD_HOMES if (h / "settings.json").exists()), None)
    if not SETTINGS.exists() and old_home:   # from before the name TextFold: copied, kept
        DirectoryFiles(SETTINGS.parent).write(SETTINGS.name, (old_home / "settings.json").read_bytes())
    if SETTINGS.exists():
        if os.name == "posix" and SETTINGS.lstat().st_mode & 0o077:
            _restrict_old_settings()
        old = json.loads(read_text(SETTINGS))
        s.update({k: v for k, v in old.items() if k in s})
        if not s["model"] and old.get("backend") == "claude-cli":  # the settings of v0.4
            s["model"] = f"claude/claude-code/{old.get('cli_model') or 'sonnet'}"
        if old.get("use_thoughtdag_env") is True and not os.environ.get("TEXTFOLD_NO_MODELS"):
            import_thoughtdag_env(s)
            save_settings(s)
    return s


def import_thoughtdag_env(s):
    """Until v0.8 the keys in ThoughtDAG's .env (a sibling folder) were read live. The reader now stands on its own:
    the providers found there are copied once into its own settings, keys and model lists included, and the link is cut."""
    have = {p["key"] for p in s["providers"]}
    for p in providers.env_providers(True):
        if p["source"] != "thoughtdag" or p["key"] in have:
            continue
        s["providers"].append({"key": p["key"], "preset": p["key"], "name": p["name"], "base": p["base"], "api_key": p["api_key"],
                               "models": [{"id": m["id"]} for m in p["models"]][:providers.MAX_MODELS]})
    s["use_thoughtdag_env"] = False


def _restrict_old_settings():
    """Older versions could save readable-by-others API keys. Restrict the original inode."""
    try:
        fd = os.open(SETTINGS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
            try:
                os.fchmod(fd, stat.S_IMODE(info.st_mode) & 0o700)
            except OSError:
                warnings.warn("This filesystem could not restrict access to TextFold settings.", RuntimeWarning)
    finally:
        os.close(fd)


def save_settings(s):
    # DirectoryFiles creates a private temporary file before writing any credential.
    DirectoryFiles(SETTINGS.parent).write(SETTINGS.name, json.dumps(s, indent=1, ensure_ascii=False).encode("utf-8"))


def provider_list(s, live=None):
    if os.environ.get("TEXTFOLD_NO_MODELS"):   # (agents.scan): no provider, no bridge
        return []
    env = providers.env_providers(s.get("use_thoughtdag_env") is True,
                                  providers.ollama_models() if live is None else live["ollama"])
    ui = []
    for p in providers.ui_providers(s):
        raw = next(x for x in s["providers"] if x["key"] == p["key"])
        if raw.get("protocol") == "anthropic":
            p["protocol"] = "anthropic"
        ui.append(p)
    # the ChatGPT bridge, when it runs, joins without being added (unless it was added by hand)
    bridge = bridges.bridge_provider(providers.NOT_TEXT) if live is None else live["bridge"]
    if bridge and not any(p["base"].rstrip("/") == bridges.BRIDGE for p in ui):
        env.append(bridge)
    plan = chatgpt.connection.provider()
    return env + ui + ([plan] if plan else [])


def job_context(field="model"):
    """A queued task can spend plan usage only with the authorization and model chosen when it was submitted."""
    context = copy_context()
    if not chatgpt.reader_key() or chatgpt.job_choice.get() is not None:
        return context
    s = load_settings()
    if field == "ask_model" and not s.get(field):
        field = "model"
    chosen = s.get(field) or ""
    plan = chatgpt.connection.provider() if chosen.startswith(chatgpt.MODEL_PREFIX) else None
    choice = ()
    if plan and s.get(f"{field}_target") == _provider_target(plan) and any(m["id"] == chosen for m in plan["models"]):
        choice = (plan["connection"], chosen)
    context.run(chatgpt.job_choice.set, choice)
    return context


def catalog(s=None, live=None):
    s = s or load_settings()
    out, seen = [], set()
    approved = [row for row in s.get("custom_agents") or [] if local_access.approved_agent(SETTINGS.parent, row.get("command"))]
    for m in agents.models() + agents.custom_models(approved):
        spec = _resolve(m["id"], s, [])
        out.append({"id": m["id"], "name": m["name"], "kind": "agent", "group": m["runtime"],
                    "efforts": m.get("efforts") or [], "defaultEffort": m.get("defaultEffort"), "target": _agent_target(spec)})
        seen.add(m["id"])
    provs = provider_list(s, live)
    for p in provs:
        for m in p["models"]:
            if m["id"] in seen or (m["id"].startswith(chatgpt.MODEL_PREFIX) and p["protocol"] != "chatgpt-plan"):
                continue
            seen.add(m["id"])
            out.append({"id": m["id"], "name": m["name"], "kind": "api", "group": p["key"], "base": p["base"], "efforts": [],
                        "defaultEffort": None, "target": _provider_target(p)})
    return out, provs


def _target(*values):
    """An identity for the route shown at selection time, without putting credentials in the catalog."""
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _agent_target(spec):
    return _target("agent", spec["runtime"], spec.get("protocol"), spec.get("command"))


def _provider_target(provider):
    fields = ("api", provider["protocol"], provider["base"], provider["source"], provider["key"])
    if provider["protocol"] == "chatgpt-plan":
        return _target(*fields, provider["identity"])
    return _target(*fields)


def _selected(field, s, cat):
    chosen, target = s.get(field), s.get(f"{field}_target")
    return next((m for m in cat if target and m["id"] == chosen and m.get("target") == target), None)


def default_model(s=None, cat=None):
    s = s or load_settings()
    cat = cat if cat is not None else catalog(s)[0]
    chosen = _selected("model", s, cat)
    return chosen["id"] if chosen else None


def resolve(model_id=None, s=None):
    """Resolve an explicit id or a saved choice whose destination is still available."""
    s = s or load_settings()
    if model_id:
        return _resolve(model_id, s)
    if not s.get("model_target"):
        raise RuntimeError(NO_MODEL)
    cat, provs = catalog(s)
    return _resolve_saved("model", s, cat, provs)


def _resolve_saved(field, s, cat, provs):
    chosen = _selected(field, s, cat)
    job_choice = chatgpt.job_choice.get()
    if chosen is None or (job_choice and chosen["id"] != job_choice[1]):
        raise RuntimeError(NO_MODEL)
    return _resolve(chosen["id"], s, provs, kind=chosen["kind"])


def _resolve(model_id, s, provs=None, kind=None):
    if kind != "api" and model_id in LEGACY_ALIASES:  # the command line's --model sonnet
        model_id = f"claude/claude-code/{model_id}"
    effort = None   # set per task (for_task); the per-model efforts of v0.9 and before are no longer read
    if kind != "api" and model_id.startswith(agents.CUSTOM):   # a custom agent's: custom/<key>/<its model>
        key, _, model = model_id[len(agents.CUSTOM):].partition("/")
        a = next((a for a in s.get("custom_agents") or [] if a.get("key") == key), None)
        if a and model and a.get("protocol") in agents.PROTOCOLS:
            command = local_access.approved_agent(SETTINGS.parent, a.get("command"))
            if command is None:
                raise RuntimeError("Approve this custom agent command in Settings first.")
            # (runtime "custom", not the agent it speaks as: llm.call keys its cache by the whole id, never by
            # Claude Code's bare model name, so its replies are never taken for Claude Code's own)
            return {"id": model_id, "kind": "agent", "runtime": "custom", "protocol": a["protocol"], "command": command,
                    "agent": a.get("name") or key, "model": model, "effort": effort}
        raise RuntimeError(f"Model {model_id} is not available (its custom agent is gone)")
    rt = agents.runtime_of(model_id) if kind != "api" else None
    if rt:
        rest = model_id[len(agents.PREFIX[rt]):]
        model = rest.split("/", 1)[1] if rt in ("claude-code", "codex", "codebuddy") else rest  # pi keeps provider/model
        return {"id": model_id, "kind": "agent", "runtime": rt, "model": model, "effort": effort}
    if kind == "agent":
        raise RuntimeError(f"Agent model {model_id} is not available")
    for p in provider_list(s) if provs is None else provs:
        direct = p["protocol"] == "chatgpt-plan"
        if direct != model_id.startswith(chatgpt.MODEL_PREFIX):
            continue
        row = next((m for m in p["models"] if m["id"] == model_id), None)
        if row is None:
            continue
        spec = {"id": model_id, "kind": "api", "protocol": p["protocol"], "base": p["base"], "api_key": p["api_key"],
                "provider": p["name"], "model": row["slug"] if direct else model_id, "effort": None}
        if direct:
            spec.update(connection=p["connection"], target=_provider_target(p))
        return spec
    raise RuntimeError(f"Model {model_id} is not available (its agent or provider is gone)")


def for_task(spec, task, s=None):
    """The spec a task is run with: the reasoning level for the task. A spec that names its own effort keeps it (the
    command line's, the model bench's)."""
    if spec.get("effort") is not None or spec.get("protocol") == "chatgpt-plan":
        return spec
    level = DEFAULT_THINKING[task]
    return {**spec, "effort": None if level == "default" else level}


def ask_choice(s=None, cat=None):
    """What questions are answered with, as the settings say: (a model of their own or None for the levels' model, a
    reasoning level or None for the questions' default). An unavailable separate choice has no usable effort."""
    s = s or load_settings()
    cat = cat if cat is not None else catalog(s)[0]
    own = _selected("ask_model", s, cat)
    used = own if s.get("ask_model") else _selected("model", s, cat)
    effort = s.get("ask_effort") if used and s.get("ask_effort") in (used.get("efforts") or []) else None
    return (own["id"] if own else None), effort


def ask_spec(s=None, tools=False):
    """The model a question is answered with, and how hard it reasons: the settings' own for questions (a stronger
    model, more reasoning), else the one the levels are written with, at the questions' default (DEFAULT_THINKING)."""
    s = s or load_settings()
    field = "ask_model" if s.get("ask_model") else "model"
    if not s.get(f"{field}_target"):
        raise RuntimeError(NO_MODEL)
    cat, provs = catalog(s)
    _, effort = ask_choice(s, cat)
    spec = _resolve_saved(field, s, cat, provs)
    spec = {**spec, "effort": effort} if effort else for_task(spec, "ask", s)
    # tools: an agent on this computer looks it up on the web with its own tools, as the reader asked beside the question
    # (agents.py); a service has none
    return {**spec, "tools": True} if tools and spec.get("kind") == "agent" else spec


def label(model_id, s=None):
    cat, _ = catalog(s)
    return next((m["name"] for m in cat if m["id"] == model_id), model_id or "")
