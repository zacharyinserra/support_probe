## Support Interface Probe

A developer tool, not a print feature. It doesn't change your slice or your G-code.

I want a plugin that lets you print support interface in a different filament in
different regions of a model. Imagine PETG supports with a PLA interface under one
overhang and a dedicated support material under another, with a modifier marking
which is which. Before building it, I need to know how much of the slicer the
plugin API can reach. This probe finds out.

### What it does

**Support Probe: dump model API** (script, run from the Plugins window)
Lists every object and volume in the scene and what the API exposes about them,
so you can see whether a plugin can tell a modifier from a model part.

**Support Probe: slicing steps** (slicing pipeline)
Records the order the slicing steps run in. At each step it lists what the plugin
can see on the print and the current object. Anything to do with support,
interface, extruder, filament or layers is marked with `*`. At the end it counts support
interface sections and filament swaps in the G-code, as a baseline.

Output goes to the Python log in `data_dir()/log/python_*.log`. Search for
`[support-probe]`.

### How to use it

1. Set up a model with supports on and at least two filaments loaded.
2. Add a box modifier over part of the supported area and rename it `SI:2`.
3. Run **Support Probe: dump model API** from the Plugins window.
4. In Advanced mode, go to Process ▸ Others ▸ Slicing Pipeline Plugin and pick
   **Support Probe: slicing steps**.
5. Change any setting so every step re-runs, then slice.

### Safety

- It only calls methods that look like getters (`get_*`, `is_*`,
  `support_layers` and similar). Setters and anything that might change the
  slice are listed by signature but never called.
- It never touches the UI from the slicing thread.
- If it hits an error, it logs it and lets the slice finish.
- It asks for no network, process or extra file permissions. It writes only a
  `support_probe_report.txt` next to the exported G-code.

### Status

Experimental, version 0.2.0. Built against the 2.5 alpha plugin API, which may
still change. If you run it, I'd like to see your log output, especially from
AMS and toolchanger setups.
