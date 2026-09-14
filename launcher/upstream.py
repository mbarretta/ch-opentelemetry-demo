"""The pinned upstream checkout: verification, reads at the pin, and the tools.py patch."""

import shutil

from . import core


def verify_upstream():
    if not core.UPSTREAM.exists():
        raise SystemExit(f"Missing {core.UPSTREAM}; run scripts/demo.py bootstrap first.")
    actual = core.capture("git", "-C", core.UPSTREAM, "rev-parse", "HEAD").stdout.strip()
    if actual != core.COMMIT:
        raise SystemExit(
            f"Expected upstream {core.COMMIT}, found {actual}; restore the pinned checkout."
        )


def upstream_file(path):
    """Content of an upstream file at the pinned commit, independent of the working tree."""
    return core.capture(
        "git", "-C", core.UPSTREAM, "show", f"{core.COMMIT}:{path}", text=False
    ).stdout


# The released shop tools hard-code USD and pass the cart id under the wrong query key.
# These edits let the agent forward the conversation's currency and reach the right cart.
TOOLS_PATCH = (
    (
        "async def list_products():",
        'async def list_products(currency_code: str = "USD"):',
    ),
    (
        'url = f"http://{BASE_URL}/api/products"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url)',
        'url = f"http://{BASE_URL}/api/products"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url, params={"currencyCode": currency_code})',
    ),
    (
        "async def get_product(product_id: str):",
        'async def get_product(product_id: str, currency_code: str = "USD"):',
    ),
    (
        'url = f"http://{BASE_URL}/api/products/{product_id}"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url)',
        'url = f"http://{BASE_URL}/api/products/{product_id}"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url, params={"currencyCode": currency_code})',
    ),
    (
        "async def get_cart(user_id: str):",
        'async def get_cart(user_id: str, currency_code: str = "USD"):',
    ),
    (
        'params={"user_id": user_id}',
        'params={"sessionId": user_id, "currencyCode": currency_code}',
    ),
)


def patch_tools(source):
    """Apply TOOLS_PATCH to the released src/shared/tools.py text; fail on any drift."""
    for old, new in TOOLS_PATCH:
        if source.count(old) != 1:
            raise SystemExit(
                f"Upstream tools.py changed around {old.splitlines()[0]!r}; review TOOLS_PATCH."
            )
        source = source.replace(old, new)
    return source


def corrected_tools():
    """The released src/shared/tools.py at the pinned commit with TOOLS_PATCH applied."""
    return patch_tools(upstream_file("src/shared/tools.py").decode())


def write_tools():
    """Refresh the corrected tools.py that the images copy and the host-run agent imports."""
    tools = corrected_tools()
    (core.RUNTIME / "tools.py").write_text(tools)
    host_copy = core.RUNTIME / "python/src/agents/tools.py"
    if host_copy.parent.is_dir():
        host_copy.write_text(tools)


def bootstrap():
    if not core.UPSTREAM.exists():
        core.run("git", "clone", "--depth", "1", "--branch", core.TAG, core.REPO, core.UPSTREAM)
    verify_upstream()
    changed = core.capture("git", "-C", core.UPSTREAM, "status", "--porcelain").stdout
    if changed:
        raise SystemExit("Upstream checkout has changes. Keep overlay changes in the project root.")
    core.RUNTIME.mkdir(exist_ok=True)
    (core.RUNTIME / "telemetry").mkdir(exist_ok=True)
    if not (core.RUNTIME / "flagd").exists():
        shutil.copytree(core.UPSTREAM / "src/flagd", core.RUNTIME / "flagd")
    for service in ["agent", "chatbot"]:
        shutil.copytree(
            core.UPSTREAM / f"src/{service}/src", core.RUNTIME / "python/src", dirs_exist_ok=True
        )
    write_tools()
    if not (core.ROOT / ".env").exists():
        shutil.copyfile(core.ROOT / ".env.example", core.ROOT / ".env")
        (core.ROOT / ".env").chmod(0o600)
    print(f"Upstream {core.TAG} ({core.COMMIT[:12]}) verified; overlay prepared.")
