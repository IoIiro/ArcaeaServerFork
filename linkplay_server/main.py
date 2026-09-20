import logging
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from json import dumps, loads

from .aes import decrypt, encrypt
from .config import Config
from .store import Store, TCPRouter, clear_player, clear_room
from .udp_class import bi
from .udp_parser import CommandParser

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s", level=logging.INFO
)


class UDP_handler(socketserver.BaseRequestHandler):
    def handle(self):
        client_msg, server = self.request

        if len(client_msg) < 36:
            return None

        token = client_msg[:8]
        token_id = bi(token)

        # Store lookup is protected separately from the room lock so the hot UDP
        # path does not hold the global store lock while parsing a packet.
        with Store.lock:
            user = Store.link_play_data.get(token_id)

        if user is None:
            return None

        iv = client_msg[8:20]
        tag = client_msg[20:36]
        ciphertext = client_msg[36:]

        try:
            plaintext = decrypt(user["key"], b"", iv, ciphertext, tag)
        except Exception as e:
            logging.debug("Invalid UDP packet from %s: %s", self.client_address[0], e)
            return None

        room = user["room"]
        commands = []
        cleanup_player = False
        cleanup_room = False

        # A room is the unit of synchronization. Multiple UDP worker threads
        # may receive packets for the same room at the same time, so all room
        # state mutation and command generation happens under this lock.
        with room.lock:
            player_index = user["player_index"]
            if not 0 <= player_index < len(room.players):
                return None

            player = room.players[player_index]
            if player.player_id != user["player_id"]:
                cleanup_player = True
            else:
                try:
                    commands = CommandParser(room, player_index).get_commands(plaintext)
                except Exception as e:
                    logging.debug(
                        "Invalid UDP command from %s: %s",
                        self.client_address[0],
                        e,
                    )
                    return None

                if room.players[player_index].player_id == 0:
                    cleanup_player = True
                    cleanup_room = room.player_num == 0
                    # Preserve the original kick-handling behavior.
                    commands = [i for i in commands if i[2] == 0x12]

        if cleanup_player:
            clear_player(token_id)
            if cleanup_room:
                clear_room(room)

        for command in commands:
            iv, ciphertext, tag = encrypt(user["key"], command, b"")
            server.sendto(token + iv + tag + ciphertext, self.client_address)


AUTH_LEN = len(Config.AUTHENTICATION)
TCP_AES_KEY = Config.TCP_SECRET_KEY.encode("utf-8").ljust(16, b"\x00")[:16]


class TCP_handler(socketserver.StreamRequestHandler):

    def handle(self):
        try:
            if self.rfile.read(AUTH_LEN).decode("utf-8") != Config.AUTHENTICATION:
                self.wfile.write(b"No authentication")
                logging.warning(f"TCP-{self.client_address[0]}-No authentication")
                return None

            cipher_len = int.from_bytes(self.rfile.read(8), byteorder="little")
            if cipher_len > Config.TCP_MAX_LENGTH:
                self.wfile.write(b"Body too long")
                logging.warning(f"TCP-{self.client_address[0]}-Body too long")
                return None

            iv = self.rfile.read(12)
            tag = self.rfile.read(16)
            ciphertext = self.rfile.read(cipher_len)

            self.data = decrypt(TCP_AES_KEY, b"", iv, ciphertext, tag)
            message = self.data.decode("utf-8")
            data = loads(message)
        except Exception as e:
            logging.error(e)
            return None

        if Config.DEBUG:
            logging.info(f"TCP-From-{self.client_address[0]}-{message}")

        r = TCPRouter(data).handle()
        try:
            r = dumps(r)
            if Config.DEBUG:
                logging.info(f"TCP-To-{self.client_address[0]}-{r}")
            iv, ciphertext, tag = encrypt(TCP_AES_KEY, r.encode("utf-8"), b"")
            r = len(ciphertext).to_bytes(8, byteorder="little") + iv + tag + ciphertext
        except Exception as e:
            logging.error(e)
            return None
        self.wfile.write(r)


class LinkPlayUDPServer(socketserver.UDPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, max_workers=Config.UDP_WORKERS):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="linkplay-udp",
        )
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        self._executor.submit(self._process_request, request, client_address)

    def _process_request(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request, client_address)

    def server_close(self):
        super().server_close()
        self._executor.shutdown(wait=True, cancel_futures=True)


class LinkPlayTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def link_play(
    ip: str = Config.HOST,
    udp_port: int = Config.UDP_PORT,
    tcp_port: int = Config.TCP_PORT,
):
    udp_server = LinkPlayUDPServer((ip, udp_port), UDP_handler)
    tcp_server = LinkPlayTCPServer((ip, tcp_port), TCP_handler)

    threads = [
        threading.Thread(target=udp_server.serve_forever),
        threading.Thread(target=tcp_server.serve_forever),
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
