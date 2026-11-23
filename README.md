# Support Interface Probe (OrcaSlicer plugin)

A read-only plugin that answers one question: can an OrcaSlicer plugin give
support interface a different filament in different regions of a print?
It doesn't change the slice. It logs what the plugin API exposes so we can tell.

Requires OrcaSlicer 2.5 alpha, or a nightly newer than 2.4.2.

## Install

Pick one:

- In OrcaSlicer, open the Plugins window, choose **Browse plugins ▸ Install local plugin**,
  and pick `support_probe.py`.
- Or copy this folder to `%APPDATA%\OrcaSlicer\orca_plugins\support_probe\`, then
  reload it from the Plugins window (right-click ▸ Reload) or restart OrcaSlicer.

## Test scene

1. Use one object with supports turned on and the AMS loaded with at least 2 filaments.
2. Add a **modifier** (right-click the object ▸ Add modifier ▸ Box) over part of the
   support area, and rename it `SI:2`.
3. Set Support filament / Support interface filament to something that isn't the
   default, so the AMS swaps show up in the G-code.

## Run

1. **Model dump.** In the Plugins window, run **Support Probe: dump model API**.
   This shows whether a plugin can tell a modifier from a part, and read its name.
2. **Slicing dump.** Switch to Advanced mode, go to **Process ▸ Others ▸ Slicing Pipeline
   Plugin**, and select **Support Probe: slicing steps**. Then slice the plate.

## Where the output goes

- Everything is printed to the Python log:
  `%APPDATA%\OrcaSlicer\log\python_<date>_<pid>.log`. Search for `[support-probe]`.
- The slicing dump also writes `support_probe_report.txt` next to the G-code
  OrcaSlicer writes out (usually a temp folder, so the log is easier to find).
  The G-code itself is left as is.
- OrcaSlicer re-runs only the steps a change affects. Before the probe slice,
  change any setting (layer height, say) so every step runs and gets recorded.

Send back the log (or the `[support-probe]` lines from it).

## What I'm looking for

| Question | Where it shows |
|---|---|
| Can a plugin tell a modifier from a part, and read its name? | Model dump: volume attributes (type, `is_modifier`, name) |
| Does support generation run before tool ordering and the wipe tower? | Step order: `posSupportMaterial` before `psWipeTower` |
| Are support layers and their extrusions exposed? | Lines marked `*` under `ctx.object` at `posSupportMaterial` |
| Can an extrusion or region carry its own extruder or filament? | `extruder` / `filament` / `role` attributes on support fills |
| Can the plugin edit those, or only read them? | Signatures of setter methods, which are listed but never called |

The probe calls only methods that take no arguments and whose names look like
getters (`get_*`, `is_*`, `support_layers`, `extruder` and similar). Anything
else, including `set_*`, `clear_*` and `generate_*`, is listed with its
signature but never run. It never touches `ctx.host` or anything that looks
like UI on the slicing thread, and if it hits an error it logs it rather than
failing the slice.
