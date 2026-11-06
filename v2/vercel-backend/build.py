"""Bundle a pinned Deno runtime for yt-dlp's YouTube EJS challenge solver."""
import hashlib
import io
import os
import platform
import urllib.request
import zipfile
from pathlib import Path

DENO_VERSION = "2.9.8"
ASSETS = {
    "x86_64": ("deno-x86_64-unknown-linux-gnu.zip", "e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609"),
    "aarch64": ("deno-aarch64-unknown-linux-gnu.zip", "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905"),
}

def main():
    arch = platform.machine().lower()
    if arch not in ASSETS or platform.system() != "Linux":
        raise RuntimeError(f"Vercel Deno build expects Linux x64/aarch64, got {platform.system()} {arch}")
    asset, expected_sha = ASSETS[arch]
    url = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/{asset}"
    print(f"Bundling Deno {DENO_VERSION} ({arch})")
    request = urllib.request.Request(url, headers={"User-Agent": "vercel-python-yt-dlp-build"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(90 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise RuntimeError("Deno archive checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        binary = package.read("deno")
    destination = Path(__file__).parent / "vendor" / "deno"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(binary)
    destination.chmod(0o755)
    print(f"Deno bundled at {destination}")

if __name__ == "__main__":
    main()
