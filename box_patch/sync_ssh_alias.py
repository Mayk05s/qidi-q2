#!/usr/bin/env python3
"""Point the `qidi` alias in an SSH config at the address from .env."""
import re
import sys


def sync(text, host):
    """Return text with HostName changed inside the `Host qidi` block only."""
    match = re.search(r"^Host[ \t]+qidi[ \t]*\n(?:(?!Host[ \t]).*\n?)*", text, re.M)
    if match is None:
        return text
    block = re.sub(r"^([ \t]*HostName[ \t]+).*$", lambda m: m.group(1) + host,
                   match.group(0), flags=re.M)
    return text[:match.start()] + block + text[match.end():]


def main():
    path, host = sys.argv[1:3]
    with open(path) as f:
        text = f.read()
    updated = sync(text, host)
    if updated != text:
        with open(path, "w") as f:
            f.write(updated)
        print("ssh qidi теперь указывает на " + host)


if __name__ == "__main__":
    main()
