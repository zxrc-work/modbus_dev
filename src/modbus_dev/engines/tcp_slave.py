"""Bounded TCP transport using the pinned PyModbus ADU and PDU codecs.

One event loop owns clients and snapshots. No datastore write path exists.
"""
from dataclasses import dataclass, field
import selectors
import socket

from pymodbus.framer.socket import FramerSocket
from pymodbus.pdu.decoders import DecodePDU

from .read_only import ReadOnlyRegisters


@dataclass
class Client:
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    pending_since: float | None = None


class TcpSlaveEngine(ReadOnlyRegisters):
    MAX_CLIENTS = 16
    MAX_BUFFER = 4096

    def __init__(self, config, control, emit):
        super().__init__(config, control, emit)
        self.selector = selectors.DefaultSelector()
        self.listener = None
        self.clients = {}
        self.framer = FramerSocket(DecodePDU(True))
        self.bound_address = None

    def connect(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener = listener
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((self.config.host, self.config.port))
        listener.listen(self.MAX_CLIENTS)
        listener.setblocking(False)
        self.bound_address = listener.getsockname()
        self.selector.register(listener, selectors.EVENT_READ)

    def _drop(self, sock):
        self.selector.unregister(sock)
        self.clients.pop(sock, None)
        sock.close()

    def poll(self, deadline):
        self.control.check()
        for key, mask in self.selector.select(max(0, min(.02, deadline - self.control.now()))):
            self.control.check()
            sock = key.fileobj
            if sock is self.listener:
                client, _ = sock.accept()
                if len(self.clients) >= self.MAX_CLIENTS:
                    client.close()
                    self._event("client_limit")
                    continue
                client.setblocking(False)
                self.clients[client] = Client()
                self.selector.register(client, selectors.EVENT_READ)
                continue
            state = self.clients[sock]
            try:
                if mask & selectors.EVENT_READ:
                    data = sock.recv(self.MAX_BUFFER)
                    if not data:
                        self._drop(sock)
                        continue
                    self._event("rx", peer=sock.getpeername(), data=data.hex())
                    if not state.incoming:
                        state.pending_since = self.control.now()
                    state.incoming.extend(data)
                    if len(state.incoming) > self.MAX_BUFFER:
                        raise ValueError("TCP input buffer limit")
                    while len(state.incoming) >= 7:
                        length = int.from_bytes(state.incoming[4:6], "big")
                        if state.incoming[2:4] != b"\x00\x00" or not 2 <= length <= 254:
                            raise ValueError("Invalid MBAP protocol or length")
                        size = 6 + length
                        if len(state.incoming) < size:
                            break
                        frame = bytes(state.incoming[:size])
                        del state.incoming[:size]
                        _, unit, tid, pdu = self.framer.decode(frame)
                        response = self.response(unit, pdu)
                        if response is not None:
                            state.outgoing.extend(self.framer.encode(response, unit, tid))
                            if len(state.outgoing) > self.MAX_BUFFER:
                                raise ValueError("TCP output buffer limit")
                    if not state.incoming:
                        state.pending_since = None
                if mask & selectors.EVENT_WRITE and state.outgoing:
                    sent = sock.send(state.outgoing)
                    if not sent:
                        raise OSError("TCP connection closed during write")
                    self._event("tx", peer=sock.getpeername(), data=bytes(state.outgoing[:sent]).hex())
                    del state.outgoing[:sent]
                self.selector.modify(sock, selectors.EVENT_READ |
                                     (selectors.EVENT_WRITE if state.outgoing else 0))
            except BlockingIOError:
                pass
            except (OSError, ValueError) as exc:
                self._event("client_closed", reason=str(exc))
                self._drop(sock)
        # Slow/partial requests have a fixed deadline, never extended by trickle bytes.
        for sock, state in list(self.clients.items()):
            if state.pending_since is not None and self.control.now() - state.pending_since > max(1, self.config.timeout_ms / 1000):
                self._event("client_closed", reason="Incomplete request deadline")
                self._drop(sock)

    def close(self):
        for sock in list(self.clients):
            self._drop(sock)
        if self.listener is not None:
            self.listener.close()
        self.selector.close()
