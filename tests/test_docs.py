"""The READMEs as a contract, checked mechanically rather than by reading.

Three things drift silently and cost a presenter the demo: a command that no longer exists in
the parser, a configuration table that has gained or lost a key against `.env.example`, and a
name from before the merge left in a runbook. All three are decidable from the documents and the
code, so they are asserted here instead of re-read by hand after every CLI change.
"""

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


def commands(text):
    """Every `demo.py …` invocation the document shows, as argument lists.

    Two spellings are deliberately not invocations. `--help` is an instruction to ask the CLI
    itself, which `parse_args` answers by printing and exiting rather than by parsing; and an
    ellipsis (`demo.py eks …`) stands for a whole family of subcommands, so there is no one
    command line to check it against.
    """
    found = []
    for region in code_regions(text):
        for segment in re.split(r"&&|\|\||;", region):
            segment = segment.split(" #")[0]
            if "demo.py" not in segment or "…" in segment or "..." in segment:
                continue
            arguments = shlex.split(segment.split("demo.py", 1)[1])
            if arguments and "--help" not in arguments:
                found.append(arguments)
    return found


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
    assert shown, f"{name} shows no demo.py command"
    for arguments in shown:
        try:
            parsed = cli.build_parser().parse_args(arguments)
        except SystemExit as refused:
            raise AssertionError(f"{name} documents `demo.py {' '.join(arguments)}`") from refused
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


def test_the_eks_runbook_covers_the_first_run_and_the_everyday_cycle():
    shown = commands(section(document(), EKS_HEADING))
    for wanted in FIRST_RUN + EVERYDAY:
        assert wanted in shown, f"the EKS runbook never shows `demo.py {' '.join(wanted)}`"
