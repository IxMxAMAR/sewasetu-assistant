"""Entry point for both servers.

    python -m sewasetu_mcp citizen
    python -m sewasetu_mcp officer

Same image, same package, two roles. The role decides the port, the token
audience the server will accept, and which tools exist.
"""

import sys

USAGE = "usage: python -m sewasetu_mcp {citizen|officer}"


def main() -> None:
    which = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if which == "citizen":
        from . import citizen
        citizen.main()
    elif which == "officer":
        from . import officer
        officer.main()
    else:
        sys.exit(USAGE)


if __name__ == "__main__":
    main()
