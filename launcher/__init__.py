"""The demo launcher: `scripts/demo.py` is a shim over this package.

The laptop modules form one chain, cli -> stack -> images -> upstream -> core, with the EKS
target hanging off cli as the `launcher.eks` package. They read each other by attribute at
call time (`core.RUNTIME`, `images.read_manifest()`) rather than importing names,
so a test that patches `core.RUNTIME` or `core.run` is seen by every module above it.

The three names below are re-exported for `scripts/smoke.py`, which runs against a live stack
and patches nothing. Inside the package, never read them from here: `launcher.RUNTIME` is bound
once at import and would not follow a patched `core.RUNTIME`.
"""

from .core import RUNTIME
from .stack import environment, scenario

__all__ = ["RUNTIME", "environment", "scenario"]
