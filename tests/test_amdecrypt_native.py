import socket
import struct
import threading

import pytest

from gamdl import _amdecrypt

MAGIC = b"WV2D"
VERSION = 1
KIND_BATCH = 1
KIND_OK = 2
KIND_ERROR = 3
KIND_CLOSE = 9


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        chunk = conn.recv(size - len(out))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _frame(kind: int, request_id: int, payload: bytes) -> bytes:
    return (
        MAGIC
        + struct.pack(">HHII", VERSION, kind, request_id, len(payload))
        + payload
    )


def _parse_batch_payload(payload: bytes):
    adam_len, uri_len, sample_count = struct.unpack_from(">HHI", payload, 0)
    off = 8
    lengths = [
        struct.unpack_from(">I", payload, off + i * 4)[0]
        for i in range(sample_count)
    ]
    off += sample_count * 4
    adam = payload[off : off + adam_len]
    off += adam_len
    uri = payload[off : off + uri_len]
    off += uri_len
    samples = []
    for length in lengths:
        samples.append(payload[off : off + length])
        off += length
    assert off == len(payload)
    return adam, uri, samples


def _ok_payload(plaintexts: list[bytes]) -> bytes:
    out = bytearray()
    out += struct.pack(">I", len(plaintexts))
    for plain in plaintexts:
        out += struct.pack(">I", len(plain))
    for plain in plaintexts:
        out += plain
    return bytes(out)


def _start_fake_wrapper(responses: list[list[bytes]]):
    ready = threading.Event()
    seen: list[tuple[int, int, bytes, bytes, list[bytes]]] = []
    close_seen = threading.Event()
    errors: list[BaseException] = []

    def run(server: socket.socket):
        try:
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                for plaintexts in responses:
                    header = _recv_exact(conn, 16)
                    magic = header[:4]
                    version, kind, request_id, payload_len = struct.unpack(
                        ">HHII", header[4:]
                    )
                    payload = _recv_exact(conn, payload_len)
                    assert magic == MAGIC
                    assert version == VERSION
                    assert kind == KIND_BATCH
                    adam, uri, samples = _parse_batch_payload(payload)
                    seen.append((kind, request_id, adam, uri, samples))
                    conn.sendall(_frame(KIND_OK, request_id, _ok_payload(plaintexts)))

                header = _recv_exact(conn, 16)
                if header:
                    magic = header[:4]
                    version, kind, request_id, payload_len = struct.unpack(
                        ">HHII", header[4:]
                    )
                    payload = _recv_exact(conn, payload_len)
                    assert magic == MAGIC
                    assert version == VERSION
                    assert kind == KIND_CLOSE
                    assert request_id == 0
                    assert payload == b""
                    close_seen.set()
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
    return port, seen, close_seen, errors, thread


def test_wrapper_decrypt_session_wire_reassemble_and_persist():
    port, seen, close_seen, errors, thread = _start_fake_wrapper(
        [[b"AAAA", b"BBBBCCCC"], [b"ZZZZ"]]
    )

    session = _amdecrypt.WrapperDecryptSession("127.0.0.1", port)
    out1 = session.decrypt_reassemble(
        "12345",
        "skd://example",
        [
            (b"xxxx", b"1111", b"", []),
            (b"aa22222222zz", b"22222222", b"", [(2, 8)]),
        ],
    )
    out2 = session.decrypt_reassemble(
        "12345",
        "skd://example",
        [(b"yyyy", b"3333", b"", [])],
    )
    session.close()

    thread.join(5)
    assert not errors
    assert close_seen.is_set()
    assert out1 == [b"AAAA", b"aaBBBBCCCCzz"]
    assert out2 == [b"ZZZZ"]
    assert seen == [
        (KIND_BATCH, 1, b"12345", b"skd://example", [b"1111", b"22222222"]),
        (KIND_BATCH, 2, b"12345", b"skd://example", [b"3333"]),
    ]


@pytest.mark.parametrize(
    ("adam", "uri", "items"),
    [
        ("", "skd://x", [(b"x" * 16, b"x" * 16, b"", [])]),
        ("1", "", [(b"x" * 16, b"x" * 16, b"", [])]),
        ("1", "skd://x", []),
        ("1", "skd://x", [(b"x", b"", b"", [])]),
    ],
)
def test_wrapper_decrypt_session_rejects_bad_inputs(adam, uri, items):
    port, _, _, _, thread = _start_fake_wrapper([])
    session = _amdecrypt.WrapperDecryptSession("127.0.0.1", port)
    with pytest.raises((OSError, ValueError)):
        session.decrypt_reassemble(adam, uri, items)
    session.close()
    thread.join(5)


def test_wrapper_decrypt_session_rejects_error_frame():
    ready = threading.Event()

    def run(server: socket.socket):
        server.listen(1)
        ready.set()
        conn, _ = server.accept()
        with conn:
            header = _recv_exact(conn, 16)
            request_id = struct.unpack(">I", header[8:12])[0]
            payload_len = struct.unpack(">I", header[12:16])[0]
            _recv_exact(conn, payload_len)
            conn.sendall(_frame(KIND_ERROR, request_id, b"nope"))
        server.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    thread = threading.Thread(target=run, args=(server,), daemon=True)
    thread.start()
    assert ready.wait(5)

    session = _amdecrypt.WrapperDecryptSession("127.0.0.1", port)
    with pytest.raises(OSError, match="nope"):
        session.decrypt_reassemble("1", "skd://x", [(b"x" * 16, b"x" * 16, b"", [])])
    session.close()
    thread.join(5)
