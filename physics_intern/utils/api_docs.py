"""Structured API documentation for the lab's C++ library, as lookup tools.

The Python side of Analysis-Utilities is two modules; the C++ side is thirteen
headers, and nearly everything the physics work needs lives there — waveform
feature extraction, the RooFit photopeak fits, the CAEN/WaveDump/SOLARIS binary
readers. Those classes are reached through PyROOT, which is a dynamic binding:
a wrong guess is not a syntax error, it is an ``AttributeError`` a line late, or
a segfault inside a method that was never meant to be called on its own.

Guessing is what a model does when it has no better option, so this gives it
one. The headers are the ground truth and they are small, so these tools serve
them verbatim rather than paraphrasing — an index of every symbol, and the
exact declaration for any of them. A paraphrase is one more thing that drifts;
the header cannot.

The tools satisfy the same duck type as :class:`~physics_intern.utils.mcp.
MCPToolset` and plug into the same per-role lookup slot, so the sub-agent's
pre-script lookup loop and the Manager pick them up with no other change.
Nothing here touches the network: the headers ship with the runtime, and are
found by asking the configured interpreter where its own ``ROOT_INCLUDE_PATH``
points.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

INDEX_TOOL = "analysis_utilities_api_index"
READ_TOOL = "analysis_utilities_api_read"

#: Roles that get these lookups when ``physics.api_docs.roles`` is absent.
#: Both, and for the same reason: the Manager writes the brief that tells a
#: sub-agent which utility to use, and gets it wrong in exactly the same way.
DEFAULT_ROLES = ("manager", "subagent")

#: The header that identifies an Analysis-Utilities include directory.
_MARKER_HEADER = "PlottingUtils.hpp"

#: Declarations worth indexing. ``enum class`` has to precede bare ``enum``.
_DECLARATION = re.compile(
    r"^(class|struct|namespace|enum\s+class|enum)\s+([A-Za-z_]\w*)"
)

#: Free functions declared directly inside a namespace, so ``IO`` lists what it
#: actually offers instead of only naming itself.
_FREE_FUNCTION = re.compile(r"^[A-Za-z_][\w:<>,* &]*[\s*&]([A-Za-z_]\w*)\s*\(")

#: Lines that open something other than a function declaration. A typedef of a
#: function pointer otherwise parses as a function returning its own alias.
_SKIP_PREFIXES = ("//", "/*", "*", "#", "}", "typedef", "using", "template")

#: A control-flow keyword followed by "(" is not a declaration.
_NOT_A_FUNCTION_NAME = frozenset(
    {"if", "for", "while", "switch", "return", "sizeof", "catch"}
)

RESULT_LIMIT = 20_000


@dataclass(frozen=True)
class Declaration:
    """One class, struct, namespace or enum, kept as written."""

    name: str
    kind: str
    header: str
    text: str
    members: tuple[str, ...] = ()


def _config() -> dict:
    try:
        from son_of_anton_cli.config import load_config

        section = (load_config() or {}).get("physics")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _candidate_dirs(interpreter: str | None, cfg: dict) -> list[Path]:
    """Directories that might hold the headers, best guess first."""
    candidates: list[str] = []
    configured = str(cfg.get("analysis_utilities_include") or "").strip()
    if configured:
        candidates.append(os.path.expanduser(configured))

    # The runtime wrapper exports ROOT_INCLUDE_PATH for its own interpreter,
    # which is the authoritative answer and needs no configuration. The agent
    # process does not have it, so ask the interpreter that does.
    if interpreter:
        try:
            proc = subprocess.run(
                [
                    interpreter,
                    "-c",
                    "import os;print(os.environ.get('ROOT_INCLUDE_PATH',''))",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            candidates.extend(proc.stdout.strip().split(os.pathsep))
        except Exception:
            pass

    candidates.extend(os.environ.get("ROOT_INCLUDE_PATH", "").split(os.pathsep))
    return [Path(entry) for entry in candidates if entry.strip()]


def find_header_dir(interpreter: str | None = None) -> Path | None:
    """Locate the Analysis-Utilities include directory, or None."""
    cfg = _config()
    for directory in _candidate_dirs(interpreter, cfg):
        if (directory / _MARKER_HEADER).is_file():
            return directory
    return None


def _leading_comment(lines: list[str], start: int) -> list[str]:
    """The contiguous ``//`` comment block immediately above *start*."""
    first = start
    while first > 0 and lines[first - 1].lstrip().startswith("//"):
        first -= 1
    return lines[first:start]


def parse_header(path: Path) -> list[Declaration]:
    """Extract every top-level declaration from one header, verbatim."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    declarations: list[Declaration] = []
    depth = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        match = _DECLARATION.match(stripped) if depth == 0 else None
        if match is None:
            depth += line.count("{") - line.count("}")
            index += 1
            continue

        # Walk to the end of the declaration. A forward declaration closes on a
        # semicolon without ever opening a brace, and carries nothing to read.
        body: list[str] = []
        inner = 0
        opened = False
        cursor = index
        while cursor < len(lines):
            current = lines[cursor]
            body.append(current)
            inner += current.count("{") - current.count("}")
            if current.count("{"):
                opened = True
            if opened and inner <= 0:
                break
            if not opened and current.rstrip().endswith(";"):
                break
            cursor += 1

        if opened:
            kind = " ".join(match.group(1).split())
            text = "\n".join(_leading_comment(lines, index) + body)
            declarations.append(
                Declaration(
                    name=match.group(2),
                    kind=kind,
                    header=path.name,
                    text=text,
                    members=_members(body, kind),
                )
            )
        index = cursor + 1

    return declarations + _file_scope_functions(lines, path.name)


def _file_scope_functions(lines: list[str], header: str) -> list[Declaration]:
    """Functions declared at file scope, outside any class or namespace.

    RooFitPhotopeakKernels.hpp is nothing but these, so a header-by-header
    index that only looked for types would show it as empty.
    """
    declarations: list[Declaration] = []
    depth = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        eligible = (
            depth == 0
            and stripped
            and not stripped.startswith(_SKIP_PREFIXES)
            and not _DECLARATION.match(stripped)
        )
        match = _FREE_FUNCTION.match(stripped) if eligible else None
        if match is None or match.group(1) in _NOT_A_FUNCTION_NAME:
            depth += line.count("{") - line.count("}")
            index += 1
            continue

        body: list[str] = []
        cursor = index
        while cursor < len(lines):
            body.append(lines[cursor])
            if lines[cursor].rstrip().endswith((";", "{", "}")):
                break
            cursor += 1
        declarations.append(
            Declaration(
                name=match.group(1),
                kind="function",
                header=header,
                text="\n".join(_leading_comment(lines, index) + body),
            )
        )
        # An inline definition opens a brace here; counting it keeps the body
        # at depth > 0, where it is correctly ignored.
        depth += sum(l.count("{") - l.count("}") for l in body)
        index = cursor + 1
    return declarations


def _members(body: list[str], kind: str) -> tuple[str, ...]:
    """Names declared directly inside a namespace body.

    Only namespaces: a class's methods are read from its declaration, but a
    namespace's free functions are the whole of what it offers and are worth
    listing in the index so they can be found by name.

    Depth-tracked, so a nested class contributes its own name and not its
    internals — a private copy constructor otherwise reads as a free function.
    """
    if kind != "namespace":
        return ()
    names: list[str] = []
    depth = 0
    for line in body[1:]:
        stripped = line.strip()
        if depth == 0 and stripped and not stripped.startswith(("//", "#", "}")):
            nested = _DECLARATION.match(stripped)
            if nested:
                names.append(nested.group(2))
            else:
                call = _FREE_FUNCTION.match(stripped)
                if call and call.group(1) not in names:
                    names.append(call.group(1))
        depth += line.count("{") - line.count("}")
    return tuple(names)


def build_index(header_dir: Path) -> list[Declaration]:
    """Every declaration in every header, header order then file order."""
    declarations: list[Declaration] = []
    for path in sorted(header_dir.glob("*.hpp")):
        declarations.extend(parse_header(path))
    return declarations


_INDEX_CACHE: dict[str, list[Declaration]] = {}


def load_index(interpreter: str | None = None) -> list[Declaration]:
    """The parsed header index, cached per include directory."""
    header_dir = find_header_dir(interpreter)
    if header_dir is None:
        return []
    key = str(header_dir)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = build_index(header_dir)
    return _INDEX_CACHE[key]


def _truncate(text: str) -> str:
    if len(text) <= RESULT_LIMIT:
        return text
    half = RESULT_LIMIT // 2
    return (
        text[:half]
        + f"\n\n[... truncated {len(text) - RESULT_LIMIT} chars ...]\n\n"
        + text[-half:]
    )


def render_index(declarations: list[Declaration], header: str = "") -> str:
    """The symbol index, grouped by header."""
    wanted = (header or "").strip().lower()
    by_header: dict[str, list[Declaration]] = {}
    for declaration in declarations:
        if wanted and wanted not in declaration.header.lower():
            continue
        by_header.setdefault(declaration.header, []).append(declaration)

    if not by_header:
        known = sorted({d.header for d in declarations})
        return f"No header matched '{header}'. Known headers: {', '.join(known)}"

    blocks: list[str] = []
    for name, entries in by_header.items():
        lines = [name]
        for entry in entries:
            suffix = ""
            if entry.members:
                suffix = "  -> " + ", ".join(entry.members)
            lines.append(f"  {entry.kind} {entry.name}{suffix}")
        blocks.append("\n".join(lines))
    return _truncate("\n\n".join(blocks))


def render_symbols(declarations: list[Declaration], symbols: list[str]) -> str:
    """The verbatim declaration for each requested symbol."""
    blocks: list[str] = []
    for symbol in symbols:
        wanted = symbol.strip()
        if not wanted:
            continue
        matches = [d for d in declarations if d.name == wanted]
        if not matches:
            matches = [
                d
                for d in declarations
                if wanted.lower() in d.name.lower() or wanted in d.members
            ]
        if not matches:
            blocks.append(f"// No declaration named '{wanted}'.")
            continue
        for declaration in matches:
            blocks.append(
                f"// {declaration.header}\n{declaration.text}"
            )
    return _truncate("\n\n".join(blocks) or "// Nothing requested.")


class ApiDocsToolset:
    """Analysis-Utilities header lookups, served locally with no network."""

    def __init__(
        self,
        declarations: list[Declaration],
        roles: tuple[str, ...] = DEFAULT_ROLES,
    ):
        self.declarations = declarations
        self.roles = tuple(roles)

    @classmethod
    def from_config(
        cls, config: dict | None = None, interpreter: str | None = None
    ) -> "ApiDocsToolset | None":
        """Build the toolset, or None when it has nothing to serve."""
        cfg = config if config is not None else _config()
        section = cfg.get("api_docs")
        if section is False:
            return None
        roles = DEFAULT_ROLES
        if isinstance(section, dict):
            declared = section.get("roles")
            if isinstance(declared, (list, tuple)):
                roles = tuple(str(r).strip() for r in declared if str(r).strip())
        if not roles:
            return None

        if interpreter is None:
            try:
                from .sandbox import resolve_interpreter

                interpreter = resolve_interpreter()
            except Exception:
                interpreter = None
        declarations = load_index(interpreter)
        return cls(declarations, roles) if declarations else None

    def enabled_for(self, role: str) -> bool:
        return bool(self.declarations) and self._role_matches(role)

    def _role_matches(self, role: str) -> bool:
        return any(role == known or role.startswith(known) for known in self.roles)

    def tools_for(self, role: str) -> list[dict]:
        if not self.enabled_for(role):
            return []
        headers = sorted({d.header for d in self.declarations})
        return [
            {
                "type": "function",
                "function": {
                    "name": INDEX_TOOL,
                    "description": (
                        "List every class, struct, namespace and enum in the "
                        "lab's C++ Analysis-Utilities library, grouped by "
                        "header. These are reached from Python through PyROOT "
                        "after analysis_utilities.load_cpp_library(), e.g. "
                        "ROOT.WaveformProcessingUtils. Call this before "
                        "writing code against the library, then "
                        f"{READ_TOOL} for the exact declaration. Headers: "
                        + ", ".join(headers)
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "header": {
                                "type": "string",
                                "description": (
                                    "Restrict to one header, e.g. "
                                    "'WaveformProcessingUtils'. Omit for all."
                                ),
                            }
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": READ_TOOL,
                    "description": (
                        "Return the exact C++ declaration of one or more "
                        "Analysis-Utilities symbols, as written in the header "
                        "— every public method with its real signature and "
                        "return type, every struct field, every enum value. "
                        "Use it instead of guessing at a PyROOT call: a wrong "
                        "guess surfaces as an AttributeError a line late, or a "
                        "segfault."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Symbol names, e.g. "
                                    "['WaveformProcessingUtils', "
                                    "'FileProcessingConfig']."
                                ),
                            }
                        },
                        "required": ["symbols"],
                    },
                },
            },
        ]

    def handles(self, tool_name: str, role: str) -> bool:
        return self.enabled_for(role) and tool_name in (INDEX_TOOL, READ_TOOL)

    def call(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        """Answer one lookup. Returns (text, is_error) — never raises."""
        arguments = arguments or {}
        try:
            if tool_name == INDEX_TOOL:
                return render_index(
                    self.declarations, str(arguments.get("header") or "")
                ), False
            if tool_name == READ_TOOL:
                requested = arguments.get("symbols")
                if isinstance(requested, str):
                    requested = [requested]
                if not isinstance(requested, (list, tuple)) or not requested:
                    return "ERROR: 'symbols' must be a non-empty list.", True
                return render_symbols(
                    self.declarations, [str(s) for s in requested]
                ), False
        except Exception as exc:  # noqa: BLE001 — tool failures are data
            return f"ERROR in {tool_name}: {type(exc).__name__}: {exc}", True
        return f"ERROR: Unknown tool '{tool_name}'.", True
