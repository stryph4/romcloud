"""ROMCloud CLI package.

Deliberately does not eagerly import :mod:`romcloud.cli.main` here. Doing so
used to cause `python -m romcloud.cli.main` (and the installed `romcloud`
console script, which `scripts/install.sh` generates as exactly that
invocation) to trigger runpy's "found in sys.modules ... prior to
execution" ``RuntimeWarning`` on every single run, because importing this
package would already fully import and execute `romcloud.cli.main` under
its real name before runpy separately re-executed it as `__main__`.
Import ``romcloud.cli.main`` directly instead:
``from romcloud.cli.main import cli``.
"""

