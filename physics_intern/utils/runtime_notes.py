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
    "analysis_utilities": """\
`analysis_utilities` is the lab's own library. PREFER IT over hand-written
equivalents — it is faster, it caches, and its plots are the publication style.

Reading ROOT data (do not write your own TTree loop, and do not reach for
uproot when this will do — this caches to df_cache/ and skips the ROOT I/O
entirely on every later call):

    from analysis_utilities.io import load_tree_data, load_leaf_array_data
    df = load_tree_data("data.root", tree_name="features")
    df, waveforms = load_tree_data("data.root", tree_name="features",
                                   array_branch="Samples")   # (n_events, n_samples)
    arrays = load_leaf_array_data("data.root", tree_name="Data_R",
                                  array_branches=["Trace0"])
    # max_events=N caps the read. cache_dir=None disables caching.

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

Also available through PyROOT after `load_cpp_library()`: WaveformProcessingUtils
(baseline subtraction, trigger finding, pulse height, short/long integrals, PSD
ratio, quality cuts) and FittingUtils/RooFitUtils (RooFit photopeak fits). If a
task needs waveform features or a peak fit, use these rather than writing the
arithmetic yourself — they are what the lab's published results were produced
with, and matching them is usually the point.""",
}


def notes_for(packages, extra: str = "") -> str:
    """Return the guidance for whichever known libraries *packages* contains.

    *packages* is the probe result from :func:`physics_intern.utils.sandbox.
    runtime_summary` — a mapping of importable module name to version. *extra*
    is appended verbatim (``physics.runtime_notes``).
    """
    names = set(packages or ())
    blocks = [note for name, note in LIBRARY_NOTES.items() if name in names]
    extra = (extra or "").strip()
    if extra:
        blocks.append(extra)
    return "\n\n".join(blocks)
