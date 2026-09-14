"""The EKS target: the same demo on a cluster instead of on a laptop.

`config` is the shared vocabulary (chart, names, state location, the keys read from `.env`);
`aws`, `k8s` and `values` are the libraries the commands are built from; `infra`, `lifecycle`,
`tunnel`, `flags`, `ops` and `check` each own a piece of the `demo.py eks ...` tree and declare
it themselves in a `register()` function, so a task can fill one module in without touching
`launcher/cli.py`.
"""

from . import check, flags, infra, lifecycle, ops, tunnel

# Subcommand order under `eks --help` follows this tuple, which groups by module: create the
# infrastructure, run the demo on it, then the day-to-day tools.
COMMAND_MODULES = (infra, lifecycle, tunnel, flags, ops, check)


def register(subparsers):
    """Declare every `demo.py eks` subcommand on the given subparser set."""
    for module in COMMAND_MODULES:
        module.register(subparsers)
