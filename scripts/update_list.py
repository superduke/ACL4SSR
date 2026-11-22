#!/usr/bin/env python3
"""Download and validate rule lists before replacing the last good copy."""

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in url)
    ):
        raise ValueError("Source must be a complete http(s) URL to a plain-text list")
    # Accessing port also validates its syntax and range.
    parsed.port
    return url


def validate_proxy_list(text):
    count = 0
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        endpoint = line.split("#", 1)[0].strip()
        port = None
        if endpoint.startswith("["):
            match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", endpoint)
            if not match:
                raise ValueError(f"Invalid IP endpoint on line {number}")
            host, port = match.groups()
            if ipaddress.ip_address(host).version != 6:
                raise ValueError(f"Bracketed endpoint is not IPv6 on line {number}")
        elif endpoint.count(":") == 1:
            host, port = endpoint.rsplit(":", 1)
        else:
            host = endpoint
        if "%" in host:
            raise ValueError(f"Scoped IP address is not supported on line {number}")
        ipaddress.ip_address(host)
        if port is not None and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535):
            raise ValueError(f"Invalid port on line {number}")
        count += 1
    if not count:
        raise ValueError("Downloaded IP list has no IP addresses")
    return text


DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def convert_adobe_list(text):
    output = []
    count = 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        fields = stripped.split()
        if len(fields) != 2 or fields[0] != "0.0.0.0" or not DOMAIN.fullmatch(fields[1]):
            raise ValueError(f"Invalid Adobe hosts entry on line {number}")
        output.append(f"DOMAIN,{fields[1]}")
        count += 1
    if not count:
        raise ValueError("Downloaded Adobe list has no domain rules")
    return "\n".join(output) + "\n"


def update_list(kind, url, destination):
    validate_url(url)
    destination = Path(destination)
    # No output files are opened until the whole response has been validated.
    request = Request(url, headers={"User-Agent": "ACL4SSR-list-updater/1.0"})
    with urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise ValueError("Source returned an unsuccessful HTTP status")
        text = response.read().decode("utf-8-sig")
    if kind == "baipiao":
        result = validate_proxy_list(text)
    elif kind == "adobe":
        result = convert_adobe_list(text)
    else:
        raise ValueError(f"Unknown list kind: {kind}")
    encoded = result.encode("utf-8")
    if destination.exists() and destination.read_bytes() == encoded:
        print(f"{destination}: no changes")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"{destination}: validated and updated")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("baipiao", "adobe"))
    parser.add_argument("url")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        update_list(args.kind, args.url, args.destination)
    except Exception as error:
        # URLs may contain private query strings; do not print raw HTTP errors.
        parser.exit(1, f"List update failed ({type(error).__name__}); existing output was preserved.\n")


if __name__ == "__main__":
    main()
