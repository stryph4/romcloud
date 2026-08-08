"""Shared Click context helper — the container factory.

Deliberately a standalone leaf module: it does not import
:mod:`romcloud.cli.main` or any ``romcloud.cli.commands.*`` module. Command
modules import :func:`get_container` from here instead of from
``romcloud.cli.main`` (which used to define it directly). That old
arrangement was circular — every command module imported
``romcloud.cli.main`` at the top, while ``romcloud.cli.main`` imports every
command module at the bottom — which only worked because
``romcloud/cli/__init__.py`` happened to fully import and execute
``romcloud.cli.main`` first as a side effect. That, in turn, caused
``python -m romcloud.cli.main`` (the exact invocation ``scripts/install.sh``
generates as the ``romcloud`` wrapper) to import and execute
``romcloud.cli.main`` *twice*, triggering runpy's "found in sys.modules ...
prior to execution" ``RuntimeWarning`` on every single CLI invocation.

Breaking the cycle here removes the need for that side-effect import, and
the warning along with it.
"""

from __future__ import annotations

import click


def get_container(ctx: click.Context):
    """Return the Container from Click context, building it on first access."""
    if "container" not in ctx.obj:
        from romcloud.bootstrap.container import Container

        config = ctx.obj["config"]
        ctx.obj["container"] = Container(config)
    return ctx.obj["container"]
