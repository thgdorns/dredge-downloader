"""Resolve the name of a public MEGA folder link.

A MEGA folder link carries an opaque id and a key; the folder's own name is in
its encrypted attributes. The website and megatools know it, but megatools uses
the id for the local folder, so dredge decrypts the name here and uses it as the
destination folder. Best effort: any API or decryption failure returns ``None``
and the caller keeps the id.
"""

from __future__ import annotations

import base64
import json
import random
import re
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_FOLDER = re.compile(
    r"(?i)^https?://(?:www\.)?mega\.(?:nz|io)/folder/([A-Za-z0-9_-]+)(?:#([A-Za-z0-9_-]+))?"
)
_API = "https://g.api.mega.co.nz/cs"


def _b64(value: str) -> bytes | None:
    text = value.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text)
    except ValueError:
        return None


def _decrypt(key: bytes, data: bytes | None) -> bytes | None:
    if not data or len(data) % 16:
        return None
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    except ValueError:
        return None


def folder_name(url: str) -> str | None:
    """The decrypted folder name, or ``None`` when it cannot be read."""
    match = _FOLDER.match(url)
    if not match or not match.group(2):
        return None
    folder_id, folder_key = match.group(1), match.group(2)
    body = json.dumps([{"a": "f", "c": 1, "r": 1, "p": folder_id}]).encode()
    request = urllib.request.Request(
        f"{_API}?id={random.randint(10**8, 10**9 - 1)}&n={folder_id}",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
        nodes = payload[0].get("f") or []
        root = next((node for node in nodes if node.get("t") == 1), None)
        if not root:
            return None
        handle = str(root.get("h"))
        entries = str(root.get("k", "")).split("/")
        segment = next(
            (entry for entry in entries if entry.startswith(handle + ":")), None
        )
        if not segment:
            return None
        node_key = _decrypt(_b64(folder_key) or b"", _b64(segment.split(":", 1)[1]))
        if not node_key:
            return None
        plain = _decrypt(node_key, _b64(str(root.get("a", ""))))
        if not plain:
            return None
        text = plain.decode("utf-8", "replace").rstrip("\0")
        text = text.removeprefix("MEGA")
        name = json.loads(text).get("n")
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
    return name if isinstance(name, str) and name.strip() else None
