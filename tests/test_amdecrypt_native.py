import socket
import struct
import threading

import pytest

from gamdl import _amdecrypt


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        chunk = conn.recv(size - len(out))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _start_fake_wrapper(
    expected_prefix: bytes,
    plaintexts: list[bytes],
    *,
    close_after_plaintexts: bool = False,
):
    ready = threading.Event()
    seen = bytearray()
    errors: list[BaseException] = []

    def run(server: socket.socket):
        try:
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                seen.extend(_recv_exact(conn, len(expected_prefix)))
                for plain in plaintexts:
                    size = struct.unpack("=I", _recv_exact(conn, 4))[0]
                    seen.extend(struct.pack("=I", size))
                    ciphertext = _recv_exact(conn, size)
                    seen.extend(ciphertext)
                    conn.sendall(plain)
                if close_after_plaintexts:
                    conn.shutdown(socket.SHUT_WR)
                terminator = _recv_exact(conn, 4)
                seen.extend(terminator)
        except BaseException as exc:
            errors.append(exc)
        finally:
            server.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    thread = threading.Thread(target=run, args=(server,), daemon=True)
    thread.start()
    assert ready.wait(5)
    return port, seen, errors, thread


def test_wrapper_decrypt_reassemble_wire_and_order():
    adam = "12345"
    uri = "skd://example"
    expected_prefix = bytes([len(adam)]) + adam.encode() + bytes([len(uri)]) + uri.encode()
    plaintexts = [b"AAAA", b"BBBBCCCC"]
    port, seen, errors, thread = _start_fake_wrapper(expected_prefix, plaintexts)

    out = _amdecrypt.wrapper_decrypt_reassemble(
        "127.0.0.1",
        port,
        adam,
        uri,
        [
            (b"xxxx", b"1111", b"", []),
            (b"aa22222222zz", b"22222222", b"", [(2, 8)]),
        ],
    )

    thread.join(5)
    assert not errors
    assert out == [b"AAAA", b"aaBBBBCCCCzz"]
    assert bytes(seen) == (
        expected_prefix
        + struct.pack("=I", 4)
        + b"1111"
        + struct.pack("=I", 8)
        + b"22222222"
        + struct.pack("=I", 0)
    )


@pytest.mark.parametrize(
    ("adam", "uri", "items"),
    [
        ("", "skd://x", [(b"x" * 16, b"x" * 16, b"", [])]),
        ("1", "", [(b"x" * 16, b"x" * 16, b"", [])]),
        ("1" * 256, "skd://x", [(b"x" * 16, b"x" * 16, b"", [])]),
        ("1", "skd://x", []),
        ("1", "skd://x", [(b"x", b"", b"", [])]),
    ],
)
def test_wrapper_decrypt_reassemble_rejects_bad_inputs(adam, uri, items):
    with pytest.raises((OSError, ValueError)):
        _amdecrypt.wrapper_decrypt_reassemble(
            "127.0.0.1",
            9,
            adam,
            uri,
            items,
        )


def test_wrapper_decrypt_reassemble_rejects_truncated_plaintext():
    adam = "1"
    uri = "skd://x"
    expected_prefix = bytes([len(adam)]) + adam.encode() + bytes([len(uri)]) + uri.encode()
    port, _, _, thread = _start_fake_wrapper(
        expected_prefix,
        [b"short"],
        close_after_plaintexts=True,
    )

    with pytest.raises(OSError, match="truncated plaintext"):
        _amdecrypt.wrapper_decrypt_reassemble(
            "127.0.0.1",
            port,
            adam,
            uri,
            [(b"x" * 16, b"x" * 16, b"", [])],
        )
    thread.join(5)
