"""performer -- the Performer collector.

Standard library only, by design: the target machine is a locked down lab box
where installing packages is not an option.  Nothing in this package may import
a third party module.

Milestone status:
    M0  bundle format, manifest schema, inspect/validate/fake-run   [done]
    M1  preflight + oncpu collection                                [todo]
    M2  full probe set, profiles, process supervision               [todo]
    M6  localhost daemon                                            [todo]
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
