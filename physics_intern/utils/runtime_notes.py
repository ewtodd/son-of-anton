"""House-library guidance for the code the physics agents write.

Knowing a library is importable is not the same as knowing to use it. A model
handed `numpy, scipy, matplotlib, analysis_utilities` will reach for the first
three, because it has seen a million scripts that use them and none that use
the fourth — and it will then reimplement, worse and slower, work the lab has
already done: waveform feature extraction, a cached TTree loader, a plotting
style that is publication-ready without being argued about.

So each library that carries house conventions gets a short brief, injected
into the tool description and the sub-agent code-execution instructions, and
only when that library actually imports in the configured runtime. The briefs
are deliberately terse: the entry points, the idiom, and what NOT to hand-roll.
Anything longer competes with the task for the model's attention.

``physics.runtime_notes`` in config.yaml appends deployment-specific text — a
second lab, another internal package, a local convention.
"""

from __future__ import annotations

#: Importable module name -> guidance shown when that module is present.
LIBRARY_NOTES: dict[str, str] = {
    "ROOT": """\
PyROOT is a binding to a C++ API, and guessing at it fails in ways that look
like Python. Every one of these was a real failed script:

    tree.GetBranch(0)          # WRONG - takes a NAME, not an index
    branch.GetLeaf(0)          # WRONG - same
    branch.GetLeaves()         # WRONG - the method is GetListOfLeaves()
    leaf.GetBufferSize()       # WRONG - not on TLeafS

Iterate; do not index:

    for branch in tree.GetListOfBranches():
        for leaf in branch.GetListOfLeaves():
            print(branch.GetName(), leaf.GetTypeName())

A method returning a null pointer raises `ReferenceError: attempt to access a
null-pointer` on the NEXT call, so the traceback points one line past the
mistake. If you get one, check what the previous line returned.

Better still: DO NOT INTROSPECT THE FILE AT ALL. The task statement already
lists every tree, its entry count, and every branch with its type. Reading that
costs nothing and cannot fail; rediscovering it has already cost several
attempts. Go straight to the analysis.""",
    "analysis_utilities": """\
`analysis_utilities` is the lab's own library. PREFER IT over hand-written
equivalents — it is faster, it caches, and its plots are the publication style.

Reading ROOT data — use these loaders. Do NOT write your own TTree loop, and
there is no uproot here: waveforms live in TArray object branches and
fixed-size array leaves, which these read straight into 2-D numpy and cache to
df_cache/, so a second pass costs nothing. A hand-rolled per-event loop over a
multi-GB file will spend your whole time budget and time out.

    from analysis_utilities.io import load_tree_data, load_leaf_array_data

    # scalar branches -> DataFrame
    df = load_tree_data("data.root", tree_name="features")

    # scalars + a TArrayF/TArrayS branch -> (DataFrame, (n_events, n_samples))
    df, waveforms = load_tree_data("data.root", tree_name="features",
                                   array_branch="Samples")

    # fixed-size array leaves, e.g. "Samples[1024]/S" -> {name: 2-D array}
    arrays = load_leaf_array_data("data.root", tree_name="Data_R",
                                  array_branches=["Samples"])

    # PASS max_events. Your script is killed after {timeout} seconds, and a
    # full read of a multi-GB file does not finish in that — it dies partway,
    # having written gigabytes of cache for nothing. A timeout here does NOT
    # mean your approach was wrong; it means you asked for too many events.
    # Start at 50_000, confirm the shape and the physics, scale up only when
    # you know what you are scaling.
    df = load_tree_data("data.root", tree_name="Data_R", max_events=50_000)

Plots — every figure you produce should go through PlottingUtils, not
matplotlib. It is an all-static class; no instantiation:

    from analysis_utilities import set_root_preferences
    ROOT = set_root_preferences(plots_dir="plots", root_files_dir="root_files")
    ROOT.PlottingUtils.SetStylePreferences(ROOT.PlotSaveFormat.kPDF)
    canvas = ROOT.PlottingUtils.GetConfiguredCanvas(False)     # True = log y
    ROOT.PlottingUtils.ConfigureAndDrawHistogram(hist, ...)    # or ...Graph, ...2DHistogram
    legend = ROOT.PlottingUtils.AddLegend(x1, x2, y1, y2)      # note: x1,x2,y1,y2
    ROOT.PlottingUtils.AddText("(a)", x, y)
    ROOT.PlottingUtils.SaveFigure(canvas, "name", "subdir")    # writes plots/subdir/name.pdf

`set_root_preferences` also pins the plot and ROOT-file output directories to
absolute paths, so the same script writes to the same place from any working
directory. Call it once at the top.

RAW WAVEFORMS. If the data card says a trace is a raw digitizer waveform, it
sits on a DC baseline and the pulse goes NEGATIVE. Integrating those samples as
they come off disk gives you a number dominated by the baseline offset, a
meaningless tail-to-total ratio, and results that look entirely plausible and
are wrong. Do not hand-roll the correction; WaveformProcessingUtils does it,
and it is what the lab's published numbers were produced with:

    from analysis_utilities import load_cpp_library
    ROOT = load_cpp_library()

    cfg = ROOT.FileProcessingConfig()
    cfg.polarity = -1            # negative-going pulses; +1 if positive
    cfg.num_samples_baseline = 10   # pre-trigger samples averaged for baseline
    cfg.pre_gate, cfg.short_gate, cfg.long_gate = 10, 10, 200
    cfg.max_events = 50000
    proc = ROOT.WaveformProcessingUtils(cfg)

    proc.ProcessWaveform(samples)      # one TArrayS -> features
    proc.ProcessFile(in_path, out_name)  # whole file -> a features TTree

Each processed waveform yields a `WaveformFeatures`: raw_pulse_height,
pulse_height, peak_position, trigger_position, short_integral, long_integral,
negative_fraction, passes_cuts, timestamp. short_integral / long_integral is
the charge-comparison PSD ratio, computed off the baseline-subtracted,
polarity-corrected trace. `ProcessingStats` reports why events were rejected
(no trigger, clipped, negative integral, bad baseline) — read it, because a
cut that silently removes most of your data will otherwise look like a clean
result.

FittingUtils / RooFitUtils do RooFit photopeak fits, for the same reason: they
are what the published fits used.""",
}


def notes_for(packages, extra: str = "", timeout: int = 60) -> str:
    """Return the guidance for whichever known libraries *packages* contains.

    *packages* is the probe result from :func:`physics_intern.utils.sandbox.
    runtime_summary` — a mapping of importable module name to version. *extra*
    is appended verbatim (``physics.runtime_notes``). *timeout* is substituted
    into the notes: "the timeout is short" was ignored, and a real number with
    the consequence spelled out is harder to skim past.
    """
    names = set(packages or ())
    blocks = [
        note.replace("{timeout}", str(timeout))
        for name, note in LIBRARY_NOTES.items()
        if name in names
    ]
    extra = (extra or "").strip()
    if extra:
        blocks.append(extra)
    return "\n\n".join(blocks)
