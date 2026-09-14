"""Offline validation of the whole EKS surface: no AWS credentials, no cluster.

Ports `check.sh`. What it catches is the class of mistake the unit tests cannot: values the
chart rejects, a collector pipeline naming a processor nobody defines, a manifest Kubernetes
will not parse. It may download pinned OpenTofu providers and the demo chart, which is why it
is a subcommand and not part of the default pytest run.

The body lands with the check task.
"""


def register(subparsers):
    """Declare `eks check`."""
    parser = subparsers.add_parser(
        "check", help="validate the OpenTofu, the collector manifest and the rendered chart"
    )
    parser.set_defaults(handler=lambda args: check())


def check():
    """Run `tofu fmt/init/validate`, parse the collector manifest, render the chart twice.

    Twice because the Langfuse exporter is conditional: both the configured and the
    unconfigured generated values have to render, and each rendering is asserted against -- the
    four image overrides landed, every processor and exporter a pipeline names is defined, the
    agent Deployment carries the optional `langfuse-credentials` references, and there is no
    chatbot Deployment. Missing tools are named rather than silently skipped.
    """
    raise SystemExit("not implemented yet")
