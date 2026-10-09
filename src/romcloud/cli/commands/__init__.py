"""Side-effect-free CLI command package.

Commands are registered explicitly by :mod:`romcloud.cli.main`. Keeping this
initializer empty lets pure commands be imported without importing every
POSIX-only runtime service.
"""

__all__: list[str] = []
