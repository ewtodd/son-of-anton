"""Shell completion scripts for the son-of-anton CLI.

The scripts are generated from the live, fully built argparse tree (what
``main.build_parser()`` returns), so they cannot drift from the commands they
complete. They are static: pressing Tab never starts Python.

Handled at every depth: subcommands, option strings, ``choices`` (positional
or the value after an option), and a filename fallback for any other
positional or option value. Aliases are left out to keep the lists clean.

Supports bash, zsh and fish.
"""

from __future__ import annotations

import argparse
import re
import shlex
from dataclasses import dataclass, field

SHELLS = ("bash", "zsh", "fish")

# nargs values that mean "a value may follow, but how many words is unknown".
_UNCOUNTED = {"?", "*", "+", argparse.REMAINDER, argparse.PARSER}


@dataclass
class Option:
    strings: list[str]
    help: str
    takes: int  # words consumed after the option (0 for flags)
    value_expected: bool  # a value follows, even if we cannot count it
    choices: list[str] | None
    metavar: str


@dataclass
class Positional:
    name: str
    choices: list[str] | None
    repeat: bool


@dataclass
class Node:
    path: tuple[str, ...]
    help: str = ""
    options: list[Option] = field(default_factory=list)
    positionals: list[Positional] = field(default_factory=list)
    subs: dict[str, "Node"] = field(default_factory=dict)


def _clean(text: str, maxlen: int = 60) -> str:
    """Make help text safe inside any of the three shells' quoting."""
    text = re.sub(r"[\x00-\x1f]", " ", text or "")
    text = re.sub(r"""['"`\\\[\]:$%]""", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maxlen]


def _choices(action: argparse.Action) -> list[str] | None:
    if action.choices is None:
        return None
    return [str(c) for c in action.choices]


def _walk(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = (), help_text: str = ""
) -> Node:
    node = Node(path=path, help=_clean(help_text))
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help or "" for a in action._choices_actions}
            seen: set[int] = set()
            for name, sub in action.choices.items():
                if id(sub) in seen:  # an alias of one already taken
                    continue
                seen.add(id(sub))
                node.subs[name] = _walk(sub, path + (name,), helps.get(name, ""))
        elif action.option_strings:
            if action.help == argparse.SUPPRESS:
                continue
            nargs = action.nargs
            if nargs == 0:
                takes, expected = 0, False
            elif nargs is None:
                takes, expected = 1, True
            elif isinstance(nargs, int):
                takes, expected = nargs, True
            else:
                takes, expected = 0, nargs in _UNCOUNTED
            node.options.append(
                Option(
                    strings=[o for o in action.option_strings if o.startswith("-")],
                    help=_clean(action.help or ""),
                    takes=takes,
                    value_expected=expected,
                    choices=_choices(action),
                    metavar=_clean(str(action.metavar or action.dest or "value"), 20)
                    or "value",
                )
            )
        else:
            node.positionals.append(
                Positional(
                    name=_clean(str(action.metavar or action.dest), 20) or "arg",
                    choices=_choices(action),
                    repeat=action.nargs in ("*", "+", argparse.REMAINDER),
                )
            )
    return node


def _all_nodes(node: Node):
    yield node
    for sub in node.subs.values():
        yield from _all_nodes(sub)


def _key(node: Node) -> str:
    return "/".join(node.path)


def _flags(node: Node) -> list[str]:
    return sorted({s for o in node.options for s in o.strings})


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------

_BASH_FILES = (
    'compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -f -- "$cur")'
)


def _bash_words(words: list[str]) -> str:
    return shlex.quote(" ".join(words))


def _bash_choices(choices: list[str] | None) -> str:
    if choices:
        return f'mapfile -t COMPREPLY < <(compgen -W {_bash_words(choices)} -- "$cur")'
    return _BASH_FILES


def generate_bash(parser: argparse.ArgumentParser) -> str:
    nodes = list(_all_nodes(_walk(parser)))

    # Words an option consumes, so they are not mistaken for subcommands.
    skip_cases: dict[int, list[str]] = {}
    for n in nodes:
        for o in n.options:
            if o.takes:
                for s in o.strings:
                    skip_cases.setdefault(o.takes, []).append(
                        shlex.quote(f"{_key(n)}|{s}")
                    )
    skip_block = "".join(
        f"            {'|'.join(pats)}) skip={count} ;;\n"
        for count, pats in sorted(skip_cases.items())
    )

    descend = "".join(
        f"            {shlex.quote(_key(n) + '|' + name)}) "
        f"node={shlex.quote(_key(child))}; npos=0 ;;\n"
        for n in nodes
        for name, child in n.subs.items()
    )

    value_cases = ""
    for n in nodes:
        for o in n.options:
            if o.value_expected:
                pats = "|".join(shlex.quote(f"{_key(n)}|{s}") for s in o.strings)
                value_cases += (
                    f"            {pats})\n                {_bash_choices(o.choices)}\n"
                    f"                ;;\n"
                )

    flag_cases = "".join(
        f"            {shlex.quote(_key(n))}) words={_bash_words(_flags(n))} ;;\n"
        for n in nodes
        if _flags(n)
    )

    node_cases = []
    for n in nodes:
        if n.subs:
            body = (
                "mapfile -t COMPREPLY < <(compgen -W "
                f'{_bash_words(sorted(n.subs))} -- "$cur")'
            )
        elif n.positionals:
            arms = [
                f"                {i}) {_bash_choices(p.choices)} ;;"
                for i, p in enumerate(n.positionals)
            ]
            last = n.positionals[-1]
            tail = _bash_choices(last.choices) if last.repeat else _BASH_FILES
            arms.append(f"                *) {tail} ;;")
            body = "case $npos in\n" + "\n".join(arms) + "\n            esac"
        else:
            continue
        node_cases.append(
            f"        {shlex.quote(_key(n))})\n            {body}\n            ;;"
        )
    node_block = "\n".join(node_cases)

    return f"""# Son of Anton Agent bash completion (from `son-of-anton completion bash`)
# Static: nothing runs when you press Tab.

_son_of_anton_completion() {{
    local cur i w node="" skip=0 npos=0 lastopt="" words
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    [[ $cur == "=" ]] && cur=""

    for ((i = 1; i < COMP_CWORD; i++)); do
        w="${{COMP_WORDS[i]}}"
        [[ $w == "=" ]] && continue
        if ((skip > 0)); then
            ((skip--))
            continue
        fi
        case "$node|$w" in
{skip_block}            *) ;;
        esac
        if ((skip > 0)); then
            lastopt="$w"
            continue
        fi
        [[ $w == -* ]] && continue
        case "$node|$w" in
{descend}            *) ((npos++)) ;;
        esac
    done

    if ((skip > 0)); then
        case "$node|$lastopt" in
{value_cases}            *) ;;
        esac
        return 0
    fi

    if [[ $cur == -* ]]; then
        words=""
        case "$node" in
{flag_cases}            *) ;;
        esac
        mapfile -t COMPREPLY < <(compgen -W "$words" -- "$cur")
        return 0
    fi

    case "$node" in
{node_block}
        *) ;;
    esac
    return 0
}}

complete -F _son_of_anton_completion son-of-anton
"""


# ---------------------------------------------------------------------------
# Zsh
# ---------------------------------------------------------------------------


def _zsh_id(node: Node) -> str:
    return "_".join(
        ("_son_of_anton",) + tuple(re.sub(r"\W", "_", p) for p in node.path)
    )


def _zsh_action(choices: list[str] | None) -> str:
    if choices:
        return "(" + " ".join(shlex.quote(c) for c in choices) + ")"
    return "_files"


def _zsh_option_specs(node: Node) -> list[str]:
    specs = []
    for o in node.options:
        for s in o.strings:
            spec = f"{s}[{o.help}]" if o.help else s
            if o.value_expected:
                spec += f":{o.metavar}:{_zsh_action(o.choices)}"
            specs.append(shlex.quote(spec))
    return specs


def _zsh_function(node: Node) -> str:
    fn = _zsh_id(node)
    specs = _zsh_option_specs(node)
    if node.subs:
        lines = [
            f"{fn}() {{",
            '    local curcontext="$curcontext" state line',
            "    typeset -A opt_args",
            "    _arguments -C \\",
        ]
        lines += [f"        {s} \\" for s in specs]
        lines += [
            "        '1:command:->cmds' \\",
            "        '*::arg:->args'",
            "    case $state in",
            "        cmds)",
            "            local -a subcmds",
            "            subcmds=(",
        ]
        for name, child in sorted(node.subs.items()):
            lines.append(f"                {shlex.quote(name + ':' + child.help)}")
        lines += [
            "            )",
            "            _describe 'command' subcmds",
            "            ;;",
            "        args)",
            "            case $line[1] in",
        ]
        for name, child in sorted(node.subs.items()):
            lines.append(f"                {shlex.quote(name)}) {_zsh_id(child)} ;;")
        lines += ["            esac", "            ;;", "    esac", "}"]
        return "\n".join(lines)

    lines = [f"{fn}() {{", "    _arguments \\"]
    lines += [f"        {s} \\" for s in specs]
    for p in node.positionals:
        star = "*" if p.repeat else ""
        spec = shlex.quote(f"{star}:{p.name}:{_zsh_action(p.choices)}")
        lines.append(f"        {spec} \\")
    lines.append("        '*:: :_files'")
    lines.append("}")
    return "\n".join(lines)


def generate_zsh(parser: argparse.ArgumentParser) -> str:
    funcs = "\n\n".join(_zsh_function(n) for n in _all_nodes(_walk(parser)))
    return f"""#compdef son-of-anton
# Son of Anton Agent zsh completion (from `son-of-anton completion zsh`)
# Static: nothing runs when you press Tab.

{funcs}

if [[ $funcstack[1] == _son-of-anton ]]; then
    _son_of_anton "$@"
else
    compdef _son_of_anton son-of-anton
fi
"""


# ---------------------------------------------------------------------------
# Fish
# ---------------------------------------------------------------------------


def _fish_q(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish_option_flags(strings: list[str]) -> str:
    parts = []
    for s in strings:
        if s.startswith("--"):
            parts.append(f"-l {s[2:]}")
        elif len(s) == 2:
            parts.append(f"-s {s[1]}")
        else:
            parts.append(f"-o {s[1:]}")
    return " ".join(parts)


def generate_fish(parser: argparse.ArgumentParser) -> str:
    nodes = list(_all_nodes(_walk(parser)))

    skip_cases: dict[int, list[str]] = {}
    for n in nodes:
        for o in n.options:
            if o.takes:
                for s in o.strings:
                    skip_cases.setdefault(o.takes, []).append(
                        _fish_q(f"{_key(n)}|{s}")
                    )
    skip_block = "".join(
        f"            case {' '.join(pats)}\n                set skip {count}\n"
        f"                continue\n"
        for count, pats in sorted(skip_cases.items())
    )
    descend = "".join(
        f"            case {_fish_q(_key(n) + '|' + name)}\n"
        f"                set node {_fish_q(_key(child))}\n"
        for n in nodes
        for name, child in n.subs.items()
    )

    lines = [
        "# Son of Anton Agent fish completion (from `son-of-anton completion fish`)",
        "# Static: nothing runs when you press Tab.",
        "",
        "# Prints the subcommand path of the command line so far, e.g. sessions/list.",
        "function __son_of_anton_node",
        '    set -l node ""',
        "    set -l skip 0",
        "    for w in (commandline -opc)[2..-1]",
        "        if test $skip -gt 0",
        "            set skip (math $skip - 1)",
        "            continue",
        "        end",
        '        switch "$node|$w"',
        skip_block.rstrip("\n") or "            case ''",
        "        end",
        "        string match -q -- '-*' $w; and continue",
        '        switch "$node|$w"',
        descend.rstrip("\n") or "            case ''",
        "        end",
        "    end",
        "    echo $node",
        "end",
        "",
        "function __son_of_anton_at",
        '    test "$(__son_of_anton_node)" = "$argv[1]"',
        "end",
        "",
    ]

    for n in nodes:
        cond = f"-n '__son_of_anton_at {_key(n)}'" if _key(n) else "-n '__son_of_anton_at \"\"'"
        lines.append(f"# {_key(n) or 'top level'}")
        for name, child in sorted(n.subs.items()):
            lines.append(
                f"complete -c son-of-anton -f {cond} -a {name} -d {_fish_q(child.help)}"
            )
        for o in n.options:
            line = f"complete -c son-of-anton {cond} {_fish_option_flags(o.strings)}"
            if o.value_expected:
                line += " -r"
                if o.choices:
                    line += " -f -a " + _fish_q(" ".join(o.choices))
            if o.help:
                line += f" -d {_fish_q(o.help)}"
            lines.append(line)
        if not n.subs:
            for p in n.positionals:
                if p.choices:
                    lines.append(
                        f"complete -c son-of-anton -f {cond} -a "
                        + _fish_q(" ".join(p.choices))
                    )
        lines.append("")
    return "\n".join(lines)


_GENERATORS = {"bash": generate_bash, "zsh": generate_zsh, "fish": generate_fish}


def generate(parser: argparse.ArgumentParser, shell: str = "bash") -> str:
    """Return the completion script for *shell* from the built *parser*."""
    try:
        gen = _GENERATORS[shell]
    except KeyError:
        raise ValueError(f"unsupported shell {shell!r}; choose from {SHELLS}") from None
    return gen(parser)
