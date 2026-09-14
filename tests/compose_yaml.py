"""Read the repository's Compose override files the way `docker compose config` would.

Shared by the build and integration-config tests so the `!reset`/`!override` handling and the
list-form environment parsing live in one place.
"""

import re

import yaml

from scripts import demo


class ComposeLoader(yaml.SafeLoader):
    """Accepts Compose's override tags: `!reset` reads as None, `!override` as its plain value."""


ComposeLoader.add_constructor("!reset", lambda loader, node: None)
ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: (
        loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    ),
)

# `${NAME}`, `${NAME:-default}` (default when unset or empty), `${NAME-default}` (unset only):
# the interpolation forms the override files use.
_SUBSTITUTION = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<sep>:-|-)(?P<default>[^}]*))?\}"
)


def environment_of(service):
    """Compose list-form environment (KEY=VALUE) as a mapping."""
    return dict(entry.split("=", 1) for entry in service["environment"])


def compose_override(name):
    return yaml.load((demo.ROOT / name).read_text(), Loader=ComposeLoader)["services"]


def interpolate(template, variables):
    """Render one Compose value against `variables` using the substitution forms listed above."""

    def substitute(match):
        value = variables.get(match["name"])
        if match["sep"] == ":-":
            return value if value else match["default"]
        if match["sep"] == "-":
            return match["default"] if value is None else value
        return value or ""

    return _SUBSTITUTION.sub(substitute, template)


def rendered_environment(name, service, variables):
    """The service's environment as `docker compose config` would render it under `variables`."""
    return {
        key: interpolate(value, variables)
        for key, value in environment_of(compose_override(name)[service]).items()
    }
