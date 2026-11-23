# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Support Interface Probe"
# description = "Read-only probe: logs what the plugin API exposes at each slicing step, to see if support interface filament can be set per region."
# author = "evilbooty"
# version = "0.2.0"
# ///
"""Read-only probe for the OrcaSlicer plugin API.

Two capabilities:
  * "Support Probe: dump model API" (Script): run from the Plugins window.
    Dumps the orca module, orca.host, and every object/volume in the scene,
    so we can see how modifiers are told apart from parts.
  * "Support Probe: slicing steps" (SlicingPipeline): select it under
    Process > Others > Slicing Pipeline Plugin, then slice. Records the order
    steps run in and dumps ctx / ctx.print / ctx.object at each step.
    At psGCodePostProcess it counts support interface sections and tool
    changes in the G-code and writes the report next to the G-code.

Everything is printed to the Python log (data_dir()/log/python_*.log).
The probe only calls zero-argument methods whose names look like getters
(allowlist) and never touches host/UI objects, so it should not alter the
slice. It does not modify the G-code.
"""

import os
import re
import threading
import time
import traceback

import orca

PROBE_TAG = "[support-probe]"
MAX_DEPTH = 3
MAX_LINES_PER_DUMP = 3000
STALE_RUN_SECONDS = 600
KEYWORDS = (
    "support", "interface", "extruder", "filament", "tool", "role",
    "layer", "volume", "region", "config", "modifier", "type", "wipe",
)
# A zero-arg method is called only if one of its name tokens is here
# (or it starts with a getter prefix) and none of its tokens is mutating.
GETTER_PREFIXES = ("get", "is", "has", "num", "count", "size", "len")
READ_NOUNS = {
    "name", "id", "idx", "index", "type", "kind", "role", "roles",
    "object", "objects", "volume", "volumes", "instance", "instances",
    "layer", "layers", "region", "regions", "fill", "fills", "entities",
    "extrusion", "extrusions", "perimeters", "support", "interface",
    "extruder", "extruders", "filament", "filaments", "tool", "tools",
    "config", "z", "height", "print", "bbox", "bounding", "box",
    "slices", "polygons", "expolygons", "area", "volume_type",
}
MUTATING_TOKENS = {
    "set", "clear", "add", "remove", "delete", "del", "invalidate", "apply",
    "process", "generate", "make", "export", "reset", "update", "append",
    "insert", "erase", "run", "slice", "simplify", "write", "save", "load",
    "push", "pop", "assign", "translate", "rotate", "scale", "mirror",
    "transform", "split", "merge", "center", "move", "drop", "cancel",
    "release", "free", "swap", "resize", "reserve", "emplace", "execute",
    "start", "stop", "send", "open", "close", "copy", "clone", "repair",
    "cleanup", "shrink", "flush", "finalize", "init", "initialize",
    "compute", "calc", "calculate", "build", "create", "fill", "sort",
    "prepare", "detect", "join", "reverse", "flip", "rebuild", "regenerate",
    "commit", "notify", "emit", "refresh", "redraw", "select", "toggle",
    "enable", "disable", "show", "hide", "attach", "detach", "bind",
    "register", "connect", "post", "undo", "redo", "lock", "unlock", "sync",
    "rename", "change", "modify", "edit", "replace", "put", "store",
    "activate", "dispatch", "invoke", "trigger", "request", "wait",
}
# Never descend into anything that looks like host/UI: calling into the GUI
# from the slicing thread can deadlock.
UI_MARKERS = ("host", "ui", "gui", "plater", "window", "app", "dialog", "frame")
CONFIG_KEYS = (
    "enable_support", "support_type", "support_style",
    "support_filament", "support_interface_filament",
    "support_interface_top_layers", "support_interface_bottom_layers",
    "enable_prime_tower", "wipe_tower_x", "single_extruder_multi_material",
)
ZERO_ARG_SIG = re.compile(r"^\s*\w+\(self(?:\s*:\s*[^,)]*)?\)")
CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_lock = threading.RLock()
_run = {}


def _reset_run():
    _run.update(steps=[], lines=[], dumped=set(), started=None)


_reset_run()


def _log(lines, text):
    lines.append(text)
    print(f"{PROBE_TAG} {text}")


def _tokens(name):
    return [t for t in CAMEL_SPLIT.sub("_", name).lower().split("_") if t]


def _safe_to_call(name):
    toks = _tokens(name)
    if not toks or any(t in MUTATING_TOKENS for t in toks):
        return False
    return toks[0] in GETTER_PREFIXES or any(t in READ_NOUNS for t in toks)


def _looks_like_ui(name, value):
    toks = set(_tokens(name))
    if toks & set(UI_MARKERS):
        return True
    cls = type(value)
    where = f"{getattr(cls, '__module__', '')}.{cls.__name__}".lower()
    return any(re.search(rf"(^|[._]){m}([._]|$)", where) for m in UI_MARKERS)


def _doc_first_line(value):
    doc = getattr(value, "__doc__", None)
    if not isinstance(doc, str) or not doc.strip():
        return ""
    return doc.strip().splitlines()[0]


def _short_repr(value, limit=160):
    try:
        text = repr(value)
    except Exception as exc:  # noqa: BLE001 - probing unknown bindings
        text = f"<repr failed: {exc}>"
    return text if len(text) <= limit else text[:limit] + "..."


def _as_sequence(value):
    """Return (length, first_item) for sequence-like values, else None."""
    if isinstance(value, (str, bytes, dict)):
        return None
    try:
        length = len(value)
    except Exception:  # noqa: BLE001
        return None
    first = None
    if length:
        try:
            first = value[0]
        except Exception:  # noqa: BLE001
            try:
                first = next(iter(value))
            except Exception:  # noqa: BLE001
                first = None
    return length, first


def dump(obj, path, lines, depth=0):
    """Dump one object, with its own line budget."""
    budget = {"limit": len(lines) + MAX_LINES_PER_DUMP, "capped": False}
    _dump(obj, path, lines, depth, set(), budget)


def _over_budget(lines, budget, indent):
    if len(lines) < budget["limit"]:
        return False
    if not budget["capped"]:
        budget["capped"] = True
        _log(lines, f"{indent}... output capped at {MAX_LINES_PER_DUMP} lines")
    return True


def _dump(obj, path, lines, depth, seen, budget):
    """Recursively list public attributes of a binding object."""
    indent = "  " * depth
    if _over_budget(lines, budget, indent):
        return
    if id(obj) in seen or obj is None or isinstance(obj, (int, float, bool, str, bytes)):
        return
    seen.add(id(obj))
    try:
        names = sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception as exc:  # noqa: BLE001
        _log(lines, f"{indent}{path}: <dir failed: {exc}>")
        return
    for name in names:
        if _over_budget(lines, budget, indent):
            return
        full = f"{path}.{name}"
        flag = " *" if any(k in name.lower() for k in KEYWORDS) else ""
        if set(_tokens(name)) & set(UI_MARKERS):
            _log(lines, f"{indent}{full}: <skipped: host/ui>")
            continue
        try:
            value = getattr(obj, name)
        except Exception as exc:  # noqa: BLE001
            _log(lines, f"{indent}{full}: <getattr failed: {exc}>{flag}")
            continue
        if _looks_like_ui(name, value):
            _log(lines, f"{indent}{full}: <skipped: host/ui {type(value).__name__}>")
            continue

        if callable(value) and not isinstance(value, type):
            sig = _doc_first_line(value)
            if not (ZERO_ARG_SIG.match(sig) and _safe_to_call(name)):
                _log(lines, f"{indent}{full}()  sig={sig!r}{flag}")
                continue
            try:
                result = value()
            except Exception as exc:  # noqa: BLE001
                _log(lines, f"{indent}{full}() -> <raised {type(exc).__name__}: {exc}>{flag}")
                continue
            _report_value(f"{full}()", result, lines, depth, seen, budget, flag)
        else:
            _report_value(full, value, lines, depth, seen, budget, flag)


def _report_value(label, value, lines, depth, seen, budget, flag):
    indent = "  " * depth
    seq = _as_sequence(value)
    if seq is not None:
        length, first = seq
        _log(lines, f"{indent}{label}: {type(value).__name__} len={length}{flag}")
        if first is not None and depth < MAX_DEPTH:
            _log(lines, f"{indent}  [0]: {type(first).__name__} {_short_repr(first)}")
            _dump(first, f"{label}[0]", lines, depth + 1, seen, budget)
        return
    _log(lines, f"{indent}{label}: {type(value).__name__} = {_short_repr(value)}{flag}")
    if depth < MAX_DEPTH and not isinstance(value, (int, float, bool, str, bytes, dict)):
        _dump(value, label, lines, depth + 1, seen, budget)


def _scan_gcode(path, lines):
    """Baseline counts: support interface sections and tool changes per file."""
    if not path or not str(path).lower().endswith(".gcode"):
        _log(lines, f"gcode scan skipped (path={path!r}, plain .gcode only)")
        return
    feature_counts = {}
    tool_changes = 0
    ams_changes = 0
    layers = 0
    interface_layers = set()
    current_layer = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith(";LAYER_CHANGE") or line.startswith("; CHANGE_LAYER"):
                    layers += 1
                    current_layer = layers
                elif line.startswith("; FEATURE:") or line.startswith(";TYPE:"):
                    feature = line.split(":", 1)[1].strip()
                    feature_counts[feature] = feature_counts.get(feature, 0) + 1
                    if "interface" in feature.lower():
                        interface_layers.add(current_layer)
                elif re.match(r"^T\d+\b", line):
                    tool_changes += 1
                elif line.startswith("M620 S"):
                    ams_changes += 1
    except OSError as exc:
        _log(lines, f"gcode scan failed: {exc}")
        return
    _log(lines, f"gcode layers={layers} T-commands={tool_changes} M620-swaps={ams_changes}")
    for feature, count in sorted(feature_counts.items()):
        _log(lines, f"gcode feature {feature!r}: {count} sections")
    if interface_layers:
        _log(lines, f"support interface on {len(interface_layers)} layers, "
                    f"first={min(interface_layers)} last={max(interface_layers)}")


def _write_report(gcode_path, lines):
    if not gcode_path:
        print(f"{PROBE_TAG} no gcode_path, report only in the log")
        return
    report_path = os.path.join(os.path.dirname(str(gcode_path)), "support_probe_report.txt")
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        print(f"{PROBE_TAG} report written to {report_path}")
    except OSError as exc:
        print(f"{PROBE_TAG} could not write report: {exc}")


def _fail_soft(where):
    """Log an unexpected error without failing the slice or the script."""
    print(f"{PROBE_TAG} error in {where}:\n{traceback.format_exc()}")
    return orca.ExecutionResult.success(f"Support probe hit an error in {where}; see Python log")


class ModelProbe(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Support Probe: dump model API"

    def execute(self):
        try:
            return self._execute()
        except Exception:  # noqa: BLE001
            return _fail_soft("model dump")

    def _execute(self):
        lines = []
        for label, mod in (("orca", orca), ("orca.host", getattr(orca, "host", None)),
                           ("orca.slicing", getattr(orca, "slicing", None))):
            _log(lines, f"=== {label} ===")
            _log(lines, ", ".join(n for n in dir(mod) if not n.startswith("_")) if mod else "<missing>")
        try:
            _log(lines, "Step members: " + ", ".join(
                n for n in dir(orca.slicing.Step) if not n.startswith("_")))
        except Exception as exc:  # noqa: BLE001
            _log(lines, f"Step enum listing failed: {exc}")

        model = orca.host.model()
        objects = list(model.objects()) if model is not None else []
        _log(lines, f"{len(objects)} objects in scene")
        for oi, obj in enumerate(objects):
            _log(lines, f"=== object {oi}: {_short_repr(getattr(obj, 'name', obj))} ===")
            if oi == 0:
                dump(obj, f"object[{oi}]", lines)
            for vi, vol in enumerate(obj.volumes() or []):
                _log(lines, f"--- object {oi} volume {vi}: name={getattr(vol, 'name', '?')!r}")
                # Full dump of every volume on the first object, so a part and
                # a modifier can be compared side by side.
                if oi == 0:
                    dump(vol, f"object[{oi}].volume[{vi}]", lines)
        return orca.ExecutionResult.success(
            f"Dumped {len(lines)} lines to the Python log (data_dir()/log/python_*.log)")


class StepProbe(orca.slicing.SlicingPipelineCapabilityBase):
    def get_name(self):
        return "Support Probe: slicing steps"

    def on_lifecycle_event(self, event, ctx):
        # Start a fresh record on every slice, so a cancelled or failed slice
        # doesn't leak into the next report.
        try:
            if event == getattr(getattr(orca, "LifecycleEvent", None), "SliceStarted", object()):
                with _lock:
                    _reset_run()
        except Exception:  # noqa: BLE001
            print(f"{PROBE_TAG} lifecycle handler error:\n{traceback.format_exc()}")

    def execute(self, ctx):
        try:
            with _lock:
                return self._execute(ctx)
        except Exception:  # noqa: BLE001
            return _fail_soft("slicing step")

    def _execute(self, ctx):
        now = time.time()
        # Fallback reset if SliceStarted never arrives.
        if _run["started"] is not None and now - _run["started"] > STALE_RUN_SECONDS:
            _reset_run()
        if _run["started"] is None:
            _run["started"] = now
        lines = _run["lines"]
        step = getattr(ctx.step, "name", str(ctx.step))
        obj_name = None
        if ctx.object is not None:
            obj_name = _short_repr(getattr(ctx.object, "name", ctx.object), 60)
        tag = step + (f"({obj_name})" if obj_name else "")
        _run["steps"].append(tag)
        _log(lines, f"=== step {len(_run['steps'])}: {tag} t+{now - _run['started']:.2f}s "
                    f"thread={threading.current_thread().name} "
                    f"print={'yes' if ctx.print is not None else 'None'} "
                    f"object={'yes' if ctx.object is not None else 'None'} ===")

        if len(_run["steps"]) == 1:
            _log(lines, "--- ctx ---")
            dump(ctx, "ctx", lines, depth=MAX_DEPTH - 1)
            for key in CONFIG_KEYS:
                try:
                    _log(lines, f"config {key} = {ctx.config_value(key)!r}")
                except Exception as exc:  # noqa: BLE001
                    _log(lines, f"config {key}: <failed: {exc}>")

        # Full dump of print/object once per step name, so we can see what
        # exists at each stage (e.g. support layers before vs after posSupportMaterial).
        for label, target in (("ctx.print", ctx.print), ("ctx.object", ctx.object)):
            if target is None or (step, label) in _run["dumped"]:
                continue
            _run["dumped"].add((step, label))
            _log(lines, f"--- {label} at {step} ---")
            dump(target, label, lines)

        if ctx.step == orca.slicing.Step.psGCodePostProcess:
            try:
                _log(lines, "=== step order ===")
                _log(lines, " > ".join(_run["steps"]))
                _scan_gcode(ctx.gcode_path, lines)
                _write_report(ctx.gcode_path, lines)
            finally:
                _reset_run()
            return orca.ExecutionResult.success("Support probe report written")

        return orca.ExecutionResult.success()


@orca.plugin
class SupportProbePlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(ModelProbe)
        orca.register_capability(StepProbe)
