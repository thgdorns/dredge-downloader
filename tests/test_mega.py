import base64
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from dredge import mega


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _encrypt(key: bytes, data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def _serve(monkeypatch, payload) -> None:
    body = json.dumps(payload).encode()
    monkeypatch.setattr(
        mega.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse(body)
    )


def test_the_online_folder_name_is_decrypted(monkeypatch):
    share_key = b"0123456789abcdef"
    node_key = b"0123456789abcdef" + b"fedcba9876543210"
    attributes = b'MEGA{"n":"VR"}'
    attributes += b"\0" * (-len(attributes) % 16)
    payload = [{
        "f": [{
            "t": 1,
            "h": "ROOT",
            "k": f"OTHER:ignored/ROOT:{_b64(_encrypt(share_key, node_key))}",
            "a": _b64(_encrypt(node_key, attributes)),
        }]
    }]
    _serve(monkeypatch, payload)

    url = f"https://mega.nz/folder/FOLDER#{_b64(share_key)}"

    assert mega.folder_name(url) == "VR"


def test_a_folder_link_without_a_key_falls_back(monkeypatch):
    assert mega.folder_name("https://mega.nz/folder/FOLDER") is None
    assert mega.folder_name("https://example.com/folder/FOLDER#KEY") is None


def test_an_api_error_falls_back(monkeypatch):
    _serve(monkeypatch, [-16])

    assert mega.folder_name("https://mega.nz/folder/FOLDER#KEY") is None
