#!/usr/bin/env python3
"""
Recover Boring UX recordings saved by extension versions before 3e29fae.

Those versions built the download data-URL from the blob's MIME type
("video/webm;codecs=vp9,opus"). A data URL's media-type ends at the FIRST comma,
so Chrome wrote the literal text `opus;base64,<base64>` to disk instead of the
video. The bytes are all still there — base64-encoded. This script decodes them.

Usage:
    python3 tools/recover-webm.py ~/Downloads/boring-ux            # every session folder
    python3 tools/recover-webm.py path/to/session-folder            # one session
    python3 tools/recover-webm.py --in-place ~/Downloads/boring-ux  # replace originals (keeps .b64.bak)

Valid files (already starting with the EBML magic 1A 45 DF A3) are left alone.
"""
import base64
import glob
import os
import sys

EBML = b"\x1a\x45\xdf\xa3"


def recover(path: str, in_place: bool) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] == EBML:
        return "valid"
    marker = raw.find(b";base64,")
    if marker < 0:
        return "unknown-format"
    try:
        data = base64.b64decode(raw[marker + 8:].strip(), validate=False)
    except Exception as e:  # noqa: BLE001
        return f"decode-failed: {e}"
    if data[:4] != EBML:
        return f"decoded-not-webm ({data[:4].hex()})"
    if in_place:
        os.replace(path, path + ".b64.bak")
        out = path
    else:
        out = path[:-5] + ".recovered.webm"
    with open(out, "wb") as f:
        f.write(data)
    return f"recovered -> {os.path.basename(out)} ({len(data) / 1048576:.1f} MB)"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    in_place = "--in-place" in sys.argv
    if not args:
        print(__doc__)
        return 2
    root = os.path.expanduser(args[0])
    files = sorted(glob.glob(os.path.join(root, "**", "*.webm"), recursive=True))
    files = [f for f in files if not f.endswith(".recovered.webm")]
    if not files:
        print(f"no .webm files under {root}")
        return 1
    for f in files:
        print(f"{recover(f, in_place):<48} {os.path.relpath(f, root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
