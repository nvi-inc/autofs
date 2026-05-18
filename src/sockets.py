import socket
import functools
import selectors
import time
import types
import json
from threading import Event

from utils import exec_app

BUFFER_SIZE = 4096
EOT = b"\0"


class Server:

    def __init__(self, host, port):
        self.host, self.port = host, port

        """
        
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.socket.listen(1)

        """
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.socket.listen()
        self.socket.setblocking(False)

        self.selector = selectors.DefaultSelector()
        self.selector.register(self.socket, selectors.EVENT_READ, data=None)

        self.stopped = Event()

    def _accept(self, sock):
        conn, addr = sock.accept()  # Should be ready to read
        conn.setblocking(False)
        data = types.SimpleNamespace(addr=addr, buffer=b"", records=[])
        events = selectors.EVENT_READ | selectors.EVENT_WRITE
        self.selector.register(conn, events, data=data)

    @staticmethod
    def _get_data(sock, data):
        try:
            while EOT not in data.buffer:
                if not (received := sock.recv(BUFFER_SIZE)):
                    print('Nothing', )
                    return None
                print(len(received))
                data.buffer += received
            while EOT in data.buffer:
                record, _, data.buffer = data.buffer.partition(EOT)
                data.records.append(record)
            return True
        except ConnectionResetError as err:
            print(str(err))
            return None

    def monit(self):
        try:
            while not self.stopped.is_set():
                for key, mask in self.selector.select(timeout=None):
                    if key.data is None:
                        self._accept(key.fileobj)
                    elif mask & selectors.EVENT_READ:
                        if self._get_data(key.fileobj, key.data):
                            for record in key.data.records:
                                if reply := self.process(key.data.addr, record):
                                    key.fileobj.send(reply + EOT)
                            key.data.records = []
                        else:
                            print(f"Closing connection to {key.data.addr}")
                            self.selector.unregister(key.fileobj)
                            key.fileobj.close()
                            self.connection_closed(key.data.addr)

        except KeyboardInterrupt:
            print("Caught keyboard interrupt, exiting")
        finally:
            self.selector.unregister(self.socket)
            self.selector.close()
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()

    def connection_closed(self, addr):
        pass

    def stop(self):
        self.stopped.set()

    def process(self, addr, data):
        print(f"Received from {addr}", len(data), data[:25])
        return None


class Client:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(1)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self):
        self.socket.connect((self.host, self.port))

    def close(self):
        self.socket.close()

    def getsockname(self):
        return self.socket.getsockname()

    def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.socket.sendall(data)  # + EOT)

    def recv(self):
        buffer = b""
        while received := self.socket.recv(BUFFER_SIZE):
            buffer += received
            if len(received) < BUFFER_SIZE:
                return buffer

    def query(self, data):
        self.send(json.dumps(data))
        ans = self.recv()
        try:
            return json.loads(ans) if ans else {}
        except json.decoder.JSONDecodeError:
            return ans.decode('utf-8')





