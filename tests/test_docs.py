"""The READMEs as a contract, checked mechanically rather than by reading.

Three things drift silently and cost a presenter the demo: a command that no longer exists in
the parser, a configuration table that has gained or lost a key against `.env.example`, and a
name from before the merge left in a runbook. All three are decidable from the documents and the
code, so they are asserted here instead of re-read by hand after every CLI change.
"""

import argparse
import re
import shlex

import pytest

from launcher import cli, core

# Every document that tells someone how to run this repository.
READMES = ("README.md", "deploy/eks/README.md", "docker/README.md", "frontend/overlay/README.md")

# Names the merge retired: the two old repositories, the bash CLI and its env files, and the
# upstream pin the EKS repository used. They survive only in the root README's History note,
# which is the one place whose subject is what this repository used to be.
RETIRED = (
    "demo.sh",
    "envvars.",
    "build-frontend",
    "DEMO_REF",
    "d6fd782e",
    "langfuse-sample-agent-app",
    "ch-otel-demo-eks",
)
HISTORY_HEADING = "## History"

# The outline the root README has to keep: the merge's point is one document that gets someone
# from a clone to a running demo on either target, and each of these answers one of those
# questions.
SECTIONS = (
    "# ch-opentelemetry-demo",
    "## What is added",
    "## Architecture",
    "## Run on a laptop",
    "## Run on EKS",
    "## Configuration",
    "## Use the assistant",
    "## Connect ClickStack and Langfuse",
    "## Demo walkthrough",
    "## Use a live model",
    "## Optional MCP transport",
    "## Test and develop",
    "## Source and integration notes",
    HISTORY_HEADING,
)

CONFIGURATION_HEADING = "## Configuration"
EKS_HEADING = "## Run on EKS"

# The EKS runbook's two halves: what a first run does once, and what every demo after it does.
FIRST_RUN = (["eks", "init"], ["eks", "apply"], ["build"], ["publish"], ["eks", "deploy"])
EVERYDAY = (["eks", "up"], ["eks", "down"], ["eks", "tunnel"], ["eks", "status"], ["eks", "verify"])

# Every bare command span the four documents show -- the spans that spell `eks up` rather than
# `demo.py eks up`, which is how the prose refers to the CLI once the reader knows its name.
# This is the surface the extractor gained: 39 spans that no gate read before, of which the root
# README's 24 had been verified once by hand and had nothing keeping them correct afterwards.
#
# Pinned rather than merely counted, for the two failures a widened extractor can have. Losing a
# span is a coverage hole that reads as green, and gaining one is either a new documented command
# or the extractor having started to read prose as a command line -- and telling those apart is a
# judgement, so it is asked of a reader here instead of guessed at. The failure prints the whole
# set either way.
BARE_SPANS = {
    "README.md": (
        "bootstrap",
        "build",
        "down",
        "eks apply",
        "eks check",
        "eks deploy",
        "eks down",
        "eks flag",
        "eks flag --reset",
        "eks init",
        "eks status",
        "eks tunnel",
        "eks tunnel status",
        "eks tunnel stop",
        "eks up",
        "eks verify",
        "publish",
        "publish --service frontend",
        "scenario",
        "scenario backend-failure --target eks",
        "scenario shopping --target eks",
        "up",
        "up --debug-chatbot",
        "up --dev",
    ),
    "deploy/eks/README.md": (
        "down",
        "eks apply",
        "eks check",
        "eks deploy",
        "eks down",
        "eks init",
        "eks up",
        "up",
    ),
    "docker/README.md": (
        "bootstrap",
        "build",
        "build --platform linux/amd64",
        "publish",
        "stage",
        "up",
    ),
    "frontend/overlay/README.md": ("stage",),
}

# Spans these documents really contain that name a piece of the CLI instead of showing a line to
# run. Each one would be refused by the parser, so each is a false positive the widened extractor
# has to keep classifying as prose. `eks` is the Configuration table's "every `eks` subcommand"
# and the History note's "an `eks` half"; `build --platform` is docker/README.md's platform
# section naming the flag whose value the next clause supplies.
MENTIONS = (["eks"], ["build", "--platform"], ["publish", "--service"], ["up", "--help"])

KEY = re.compile(r"`([A-Z][A-Z0-9_]*)`")


def document(name="README.md"):
    return (core.ROOT / name).read_text()


def split_fences(text):
    """A Markdown document as (lines inside code fences, lines outside them).

    Both callers need the distinction and neither can guess it: a `#` inside a fence is a shell
    comment rather than a heading, and a command is as likely to be shown in a fence as in an
    inline span.
    """
    fenced, prose = [], []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        (fenced if in_fence else prose).append(line)
    return fenced, prose


def code_regions(text):
    """Every fenced code line and every inline code span in a Markdown document.

    Prose lines are rejoined before the inline spans are matched, so a span that Markdown wrapped
    across two lines is still read as one command rather than dropped as unterminated.
    """
    fenced, prose = split_fences(text)
    spans = re.findall(r"`([^`]+)`", "\n".join(prose))
    return fenced + [" ".join(span.split()) for span in spans]


def subparsers_action(parser):
    """The subparser action a parser declares, or None when it declares no subcommands."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def names_rather_than_runs(root, arguments):
    """Whether an argument list names part of the CLI instead of showing a line to run.

    The READMEs talk about the CLI as well as showing how to drive it, and the two spellings
    look alike once the backticks are gone. Three of them are talk:

    - `--help`, an instruction to ask the CLI itself, which `parse_args` answers by printing
      and exiting rather than by parsing.
    - a command group with no subcommand under it: `eks` is the whole EKS half, as in "every
      `eks` subcommand", not something anyone types.
    - a trailing option that takes a value: `build --platform`, whose value the sentence
      around it supplies.

    The last two are decided by walking the argument list into the real parser rather than by
    a list of exceptions kept here, so a group or an option added later is classified without
    an edit to this file. That matters more than the four characters it saves: a hand-kept
    exception list is what let the previous version of this gate cover one span shape.
    """
    if "--help" in arguments:
        return True

    parser = root
    for word in arguments:
        action = subparsers_action(parser)
        if action is None or word not in action.choices:
            break
        parser = action.choices[word]

    group = subparsers_action(parser)
    if group is not None and group.required:
        return True

    last = arguments[-1]
    return last.startswith("-") and any(
        last in action.option_strings and action.nargs != 0 for action in parser._actions
    )


def invocations(text):
    """Every command line the document shows, as (argument list, the text it was read from).

    Both spellings count: the full `demo.py eks up` a fenced block shows, and the bare `eks
    up` prose uses once the reader knows what the CLI is called. The bare form is most of the
    surface -- the root README alone shows two dozen of them and no fenced block spells one --
    so a gate that reads only the `demo.py` form checks the runbook and leaves the prose
    around it to be re-read by hand, which is the thing this file exists not to do.

    An ellipsis (`demo.py eks …`, `eks …`) stands for a family of subcommands, so there is no
    one command line to check it against; `names_rather_than_runs` decides the rest.
    """
    root = cli.build_parser()
    top_level = subparsers_action(root).choices
    found = []
    for region in code_regions(text):
        for segment in re.split(r"&&|\|\||;", region):
            segment = segment.split(" #")[0].strip()
            words = segment.split()
            if not words or "…" in segment or "..." in segment:
                continue
            if "demo.py" in segment:
                shown = segment.split("demo.py", 1)[1]
            elif words[0] in top_level:
                shown = segment
            else:
                continue
            arguments = shlex.split(shown)
            if arguments and not names_rather_than_runs(root, arguments):
                found.append((arguments, segment))
    return found


def commands(text):
    """Every command line the document shows, as argument lists. See `invocations`."""
    return [arguments for arguments, _ in invocations(text)]


def section(text, heading):
    """One `##` section of a document, heading included, up to the next one."""
    assert heading in text, heading
    return heading + text.split(heading, 1)[1].split("\n## ", 1)[0]


def table_keys(text):
    """The configuration keys named in the first column of that section's table."""
    keys = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line) <= set("| -"):
            continue
        keys.extend(KEY.findall(line.split("|")[1]))
    return keys


def env_example_keys():
    lines = (core.ROOT / ".env.example").read_text().splitlines()
    return [line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")]


@pytest.mark.parametrize("name", READMES)
def test_every_documented_command_exists_in_the_parser(name):
    """A README that documents a command the CLI does not have is worse than no README."""
    shown = commands(document(name))
    assert shown, f"{name} shows no command at all"
    for arguments in shown:
        try:
            parsed = cli.build_parser().parse_args(arguments)
        except SystemExit as refused:
            raise AssertionError(
                f"{name} documents `{' '.join(arguments)}`, which the parser refuses"
            ) from refused
        assert callable(parsed.handler), arguments


def test_the_configuration_table_and_env_example_hold_the_same_keys():
    documented = table_keys(section(document(), CONFIGURATION_HEADING))
    assert len(documented) == len(set(documented)), "a key is in the table twice"

    example = env_example_keys()
    assert not set(example) - set(documented), (
        f"in .env.example, missing from the configuration table: "
        f"{sorted(set(example) - set(documented))}"
    )
    assert not set(documented) - set(example), (
        f"in the configuration table, missing from .env.example: "
        f"{sorted(set(documented) - set(example))}"
    )


@pytest.mark.parametrize("name", READMES)
def test_no_retired_name_survives_outside_the_history_note(name):
    text = document(name).split(HISTORY_HEADING, 1)[0]
    for retired in RETIRED:
        assert retired not in text, f"{name} still refers to {retired}"


def test_the_readme_keeps_its_outline():
    text = document()
    for heading in SECTIONS:
        assert f"\n{heading}\n" in f"\n{text}", heading


def slug(heading):
    """A Markdown heading as the anchor GitHub gives it."""
    text = heading.lstrip("#").strip().lower()
    return re.sub(r"[^a-z0-9 -]", "", text).replace(" ", "-")


def anchors(text):
    """Every anchor a document's headings offer. Fenced lines are excluded: a `#` in a shell
    block is a comment, and counting it would make this a check that accepts a dead link."""
    _, prose = split_fences(text)
    return {slug(line) for line in prose if line.startswith("#")}


@pytest.mark.parametrize("name", READMES)
def test_every_cross_link_resolves(name):
    """The two halves of the documentation only work if they point at each other correctly."""
    here = (core.ROOT / name).parent
    for target in re.findall(r"]\((?!https?:|mailto:)([^)]+)\)", document(name)):
        path, _, anchor = target.partition("#")
        linked = (here / path).resolve() if path else (core.ROOT / name)
        assert linked.is_file(), f"{name} links to {target}, which does not exist"
        if anchor:
            assert anchor in anchors(linked.read_text()), (
                f"{name} links to {target}, but that document has no such heading"
            )


def bare_spans(name):
    """The distinct bare command lines one document shows, sorted."""
    shown = invocations(document(name))
    return sorted({" ".join(arguments) for arguments, text in shown if "demo.py" not in text})


@pytest.mark.parametrize("name", READMES)
def test_the_bare_command_spans_are_the_reviewed_set(name):
    """The full list of bare spans the extractor reads, printed and checked against BARE_SPANS.

    Printed because the answer to "is this extractor reading prose as commands?" is not a
    boolean anyone can assert -- it is a set someone has to look at. `pytest -s
    tests/test_docs.py` shows it on a passing run; a failure shows it either way, along with
    what moved.
    """
    spans = bare_spans(name)
    print(f"\n{name}: bare command spans ({len(spans)})")
    for span in spans:
        print(f"  {span}")

    gained = sorted(set(spans) - set(BARE_SPANS[name]))
    lost = sorted(set(BARE_SPANS[name]) - set(spans))
    assert tuple(spans) == BARE_SPANS[name], (
        f"the bare command spans of {name} moved. Newly read as commands: {gained}. "
        f"No longer read: {lost}. If the gained ones are commands and the lost ones are gone "
        f"from the document, update BARE_SPANS; if not, the extractor has started reading "
        f"prose, or stopped reading a span it used to cover."
    )


def test_a_span_that_names_the_cli_is_not_read_as_a_command():
    """A gate widened until it reads prose as commands is worse than the narrow one it replaced.

    The mentions and the invocations below differ by one word each, which is the whole
    difficulty: `build --platform` against `build --platform linux/amd64`, `eks` against `eks
    up`, `publish --service` against `publish --service frontend`. Asserted against the
    classifier directly rather than through the documents, so it keeps holding for the spellings
    a later edit introduces and not only for the ones present today.
    """
    root = cli.build_parser()
    for mention in MENTIONS:
        assert names_rather_than_runs(root, mention), f"`{' '.join(mention)}` is prose"

    for invocation in (
        ["eks", "up"],
        ["eks", "flag", "--reset"],
        ["eks", "tunnel", "stop"],
        ["build", "--platform", "linux/amd64"],
        ["publish", "--service", "frontend"],
        ["up", "--dev"],
        ["scenario"],
    ):
        assert not names_rather_than_runs(root, invocation), (
            f"`{' '.join(invocation)}` is a command line, not a mention"
        )


def test_the_eks_runbook_covers_the_first_run_and_the_everyday_cycle():
    shown = commands(section(document(), EKS_HEADING))
    for wanted in FIRST_RUN + EVERYDAY:
        assert wanted in shown, f"the EKS runbook never shows `demo.py {' '.join(wanted)}`"
