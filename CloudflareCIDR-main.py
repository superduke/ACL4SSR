"""Generate the Cloudflare IPv4 lists from ipverse's ASN archive."""

from http.client import HTTPException
import io
import ipaddress
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen
import zipfile


ARCHIVE_URL = "https://github.com/ipverse/as-ip-blocks/archive/refs/heads/master.zip"
REQUEST_TIMEOUT = 120
INCLUDED_ASNS = (
    "209242", "13335", "149648", "132892", "139242", "202623", "203898", "394536"
)
SOURCE_FILENAME = "ipv4-aggregated.txt"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
OUTPUT_PATHS = (Path("Clash/CloudflareCIDR.list"), Path("CloudflareCIDR.txt"))


def download_archive(url=ARCHIVE_URL):
    """Fail before touching output files if the download is not complete."""
    request = Request(url, headers={"User-Agent": "ACL4SSR-list-updater/1.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        if response.status != 200:
            raise ValueError(f"Expected HTTP 200, received {response.status}")
        data = response.read()
    if not data:
        raise ValueError("The downloaded archive is empty")
    return data


def render_outputs(data):
    """Validate every selected source and read ZIP members without extracting."""
    clash_lines = []
    cidr_lines = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        sources = {}
        roots = set()
        for member in archive.infolist():
            parts = member.filename.split("/")
            # The repository/branch prefix changes when upstream is renamed.
            if (len(parts) != 4 or parts[1] != "as"
                    or parts[2] not in INCLUDED_ASNS or parts[3] != SOURCE_FILENAME):
                continue
            if parts[0] in ("", ".", ".."):
                raise ValueError(f"Invalid source path: {member.filename}")
            asn = parts[2]
            if asn in sources:
                raise ValueError(f"Duplicate IPv4 source for AS{asn}")
            if member.file_size > MAX_SOURCE_BYTES:
                raise ValueError(f"IPv4 source for AS{asn} is unexpectedly large")
            sources[asn] = member
            roots.add(parts[0])

        missing = [asn for asn in INCLUDED_ASNS if asn not in sources]
        if missing:
            raise ValueError("Missing IPv4 sources for " + ", ".join("AS" + asn for asn in missing))
        if len(roots) != 1:
            raise ValueError("Selected IPv4 sources must share one archive root")

        # Use the configured order so output does not depend on ZIP entry order.
        for asn in INCLUDED_ASNS:
            text = archive.read(sources[asn]).decode("utf-8")
            if not text.strip():
                raise ValueError(f"IPv4 source for AS{asn} is empty")
            lines = text.splitlines()
            if not any(line == f"# AS{asn}" or line.startswith(f"# AS{asn} ") for line in lines):
                raise ValueError(f"Missing or mismatched AS{asn} source header")
            for number, original in enumerate(lines, start=1):
                line = original.strip()
                if not line or line.startswith("#"):
                    clash_lines.append(original)
                    continue
                try:
                    if not re.fullmatch(r"[0-9.]+/[0-9]{1,2}", line):
                        raise ValueError("Expected an IPv4 CIDR")
                    network = ipaddress.IPv4Network(line)
                except ValueError as error:
                    raise ValueError(f"Invalid IPv4 CIDR in AS{asn}, line {number}: {line!r}") from error
                cidr = str(network)
                clash_lines.append(f"IP-CIDR,{cidr},no-resolve")
                cidr_lines.append(cidr)

    # Some ASNs legitimately have comment-only IPv4 files (e.g. IPv6-only ASNs).
    # Still require all sources above, and never publish an empty combined list.
    if not cidr_lines:
        raise ValueError("No IPv4 CIDRs found in the selected sources")
    return "\n".join(clash_lines) + "\n", "\n".join(cidr_lines) + "\n"


def write_outputs(output_dir, contents):
    """Stage both complete outputs before replacing any existing file."""
    output_dir = Path(output_dir)
    changed = []
    for relative_path, content in zip(OUTPUT_PATHS, contents):
        destination = output_dir / relative_path
        encoded = content.encode("utf-8")
        if not destination.exists() or destination.read_bytes() != encoded:
            changed.append((destination, encoded))
    if not changed:
        return False

    with tempfile.TemporaryDirectory(prefix=".cloudflare-cidr-", dir=output_dir) as temp_dir:
        staged = []
        for index, (destination, content) in enumerate(changed):
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(temp_dir) / str(index)
            temporary.write_bytes(content)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
    return True


def generate(output_dir=".", archive_url=ARCHIVE_URL):
    contents = render_outputs(download_archive(archive_url))
    return write_outputs(output_dir, contents)


def main():
    try:
        changed = generate()
    except (URLError, HTTPException, OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        print(f"Cloudflare CIDR update failed: {error}", file=sys.stderr)
        return 1
    print("Cloudflare CIDR lists updated." if changed else "Cloudflare CIDR lists unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
