"""The house API docs the physics agents look things up in.

The bug behind these: ``analysis_utilities`` is two Python modules over a
thirteen-header C++ library, and the agents reached the C++ side through
PyROOT by guessing. The injected brief told them to call
``proc.ProcessWaveform(samples)`` and read features off the result; the real
declaration is ``Bool_t ProcessWaveform(const TArrayS &)``, which fills a TTree
only ``ProcessFile`` creates and segfaults when called on its own. Nothing
caught it because nothing served the agent the actual declarations.

These cover the parser that turns the headers into an index, and the toolset
that serves it into the same per-role lookup slot the remote MCP endpoint uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_intern.utils.api_docs import (
    INDEX_TOOL,
    READ_TOOL,
    ApiDocsToolset,
    build_index,
    parse_header,
    render_index,
    render_symbols,
)

HEADER = """\
#ifndef SAMPLE_H
#define SAMPLE_H

#include <TArrayS.h>

class Forward;

// A gate configuration.
struct GateConfig {
  Int_t polarity = -1;
  Int_t long_gate = 200;
};

enum class SaveFormat { kPNG, kPDF };

namespace Space {
void SetBase(const TString &dir);
TFile *OpenForReading(const TString &subpath);

class ScopedLock {
public:
  ScopedLock();

private:
  ScopedLock(const ScopedLock &);
};
} // namespace Space

typedef int (*ErrorHandler)(void *display);

inline int IgnoreError(void *, void *) { return 0; }

void LaunchEditor(TH1 *hist, Double_t low);

class Processor {
public:
  Processor(const GateConfig &config);
  Bool_t ProcessWaveform(const TArrayS &samples);
};

#endif
"""


@pytest.fixture()
def header_dir(tmp_path: Path) -> Path:
    (tmp_path / "Sample.hpp").write_text(HEADER, encoding="utf-8")
    return tmp_path


def test_every_kind_of_declaration_is_found(header_dir: Path) -> None:
    found = {d.name: d.kind for d in parse_header(header_dir / "Sample.hpp")}
    assert found["GateConfig"] == "struct"
    assert found["SaveFormat"] == "enum class"
    assert found["Space"] == "namespace"
    assert found["Processor"] == "class"


def test_a_forward_declaration_carries_nothing_to_read(header_dir: Path) -> None:
    """``class Forward;`` has no body, so indexing it would promise a lookup
    that returns an empty block."""
    names = {d.name for d in parse_header(header_dir / "Sample.hpp")}
    assert "Forward" not in names


def test_a_declaration_is_served_exactly_as_written(header_dir: Path) -> None:
    """The point of the tool is that it does not paraphrase."""
    declarations = build_index(header_dir)
    text = render_symbols(declarations, ["Processor"])
    assert "Bool_t ProcessWaveform(const TArrayS &samples);" in text
    assert "Processor(const GateConfig &config);" in text


def test_a_namespace_lists_what_it_offers(header_dir: Path) -> None:
    space = next(d for d in build_index(header_dir) if d.name == "Space")
    assert "SetBase" in space.members
    assert "OpenForReading" in space.members
    assert "ScopedLock" in space.members


def test_a_private_copy_constructor_is_not_a_namespace_member(
    header_dir: Path,
) -> None:
    """It sits inside a nested class, and reads as a function to a scanner
    that does not track depth — which produced a phantom 'copedRootLock'."""
    space = next(d for d in build_index(header_dir) if d.name == "Space")
    assert not [m for m in space.members if m.endswith("copedLock") and m != "ScopedLock"]


def test_file_scope_functions_are_indexed(header_dir: Path) -> None:
    """A header can be nothing but free functions; RooFitPhotopeakKernels.hpp
    is, and an index that only looked for types showed it as empty."""
    functions = {d.name for d in build_index(header_dir) if d.kind == "function"}
    assert "LaunchEditor" in functions
    assert "IgnoreError" in functions


def test_a_function_pointer_typedef_is_not_a_function(header_dir: Path) -> None:
    """``typedef int (*ErrorHandler)(...)`` parses as a function named ``int``
    unless typedefs are skipped."""
    functions = {d.name for d in build_index(header_dir) if d.kind == "function"}
    assert "int" not in functions
    assert "ErrorHandler" not in functions


def test_an_inline_body_does_not_leak_declarations(header_dir: Path) -> None:
    """The brace an inline definition opens has to be counted, or everything
    after it is scanned as though it were at file scope."""
    names = [d.name for d in build_index(header_dir)]
    assert names.count("IgnoreError") == 1
    assert "return" not in names


def test_the_index_groups_by_header_and_can_be_filtered(header_dir: Path) -> None:
    declarations = build_index(header_dir)
    assert "Sample.hpp" in render_index(declarations)
    assert "No header matched" in render_index(declarations, "Nonexistent")


def test_an_unknown_symbol_says_so_rather_than_returning_nothing(
    header_dir: Path,
) -> None:
    text = render_symbols(build_index(header_dir), ["Nonexistent"])
    assert "Nonexistent" in text


def test_a_near_miss_still_finds_the_symbol(header_dir: Path) -> None:
    """A model that asks for the wrong case should get the declaration, not a
    lecture — the whole point is to stop it guessing."""
    text = render_symbols(build_index(header_dir), ["processor"])
    assert "class Processor" in text


def _toolset(header_dir: Path) -> ApiDocsToolset:
    return ApiDocsToolset(build_index(header_dir))


def test_both_roles_that_write_code_get_the_lookups(header_dir: Path) -> None:
    toolset = _toolset(header_dir)
    assert len(toolset.tools_for("manager")) == 2
    assert len(toolset.tools_for("subagent")) == 2


def test_a_numbered_subagent_matches_by_prefix(header_dir: Path) -> None:
    """Sub-agents are named subagent_iter3_2, like the max-token overrides."""
    toolset = _toolset(header_dir)
    assert toolset.tools_for("subagent_iter3_2")
    assert toolset.handles(INDEX_TOOL, "subagent_iter3_2")


def test_a_role_outside_the_allowlist_gets_nothing(header_dir: Path) -> None:
    toolset = _toolset(header_dir)
    assert toolset.tools_for("critic") == []
    assert not toolset.handles(INDEX_TOOL, "critic")


def test_a_bad_call_is_reported_as_data_not_raised(header_dir: Path) -> None:
    """The lookup loop treats tool failure as content; an exception would kill
    the sub-agent turn instead."""
    toolset = _toolset(header_dir)
    _, is_error = toolset.call(READ_TOOL, {"symbols": []})
    assert is_error
    _, is_error = toolset.call("no_such_tool", {})
    assert is_error


def test_a_single_symbol_string_is_accepted(header_dir: Path) -> None:
    """Models pass a bare string where an array was asked for."""
    toolset = _toolset(header_dir)
    text, is_error = toolset.call(READ_TOOL, {"symbols": "Processor"})
    assert not is_error
    assert "class Processor" in text


def test_the_toolset_declines_to_exist_with_nothing_to_serve(tmp_path) -> None:
    """No headers means no tools, rather than two that answer every question
    with 'not found'."""
    assert ApiDocsToolset.from_config(config={}, interpreter=str(tmp_path)) is None


def test_the_composite_merges_sources_without_duplicating_names(
    header_dir: Path,
) -> None:
    from physics_intern.utils.mcp import CompositeToolset

    docs = _toolset(header_dir)
    composite = CompositeToolset([docs, docs])
    names = [t["function"]["name"] for t in composite.tools_for("subagent")]
    assert sorted(names) == [INDEX_TOOL, READ_TOOL]
    assert composite.handles(READ_TOOL, "subagent")
    _, is_error = composite.call(READ_TOOL, {"symbols": ["Processor"]})
    assert not is_error
