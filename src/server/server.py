import asyncio
import cbor2
import logging
from asyncio import IncompleteReadError
from src.util.streamable import transform_to_streamable


log = logging.getLogger(__name__)
logging.basicConfig(format='%(name)s - %(message)s', level=logging.INFO)

LENGTH_BYTES: int = 5


server_connections = []


# A new ChiaConnection object is created every time a connection is opened
class ChiaConnection:
    def __init__(self, api):
        self.api_ = api
        self.open_ = False
        self.open_lock_ = asyncio.Lock()
        self.write_lock_ = asyncio.Lock()
        self.client_opened_ = False

    # Handles an open connection, infinite loop, until EOF
    async def new_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async with self.write_lock_:
            self.reader_ = reader
            self.writer_ = writer
        log.info("Calling new_connection")
        if not self.client_opened_:
            # Prevents up from opening two connections on the same object
            async with self.open_lock_:
                if self.open_:
                    writer.close()
                    raise RuntimeError("This object already has open")
                self.open_ = True

        peername = writer.get_extra_info('peername')
        log.info(f'Connected to {peername}')

        try:
            while not reader.at_eof():
                size = await reader.readexactly(LENGTH_BYTES)
                full_message_length = int.from_bytes(size, "big")
                full_message = await reader.readexactly(full_message_length)

                decoded = cbor2.loads(full_message)
                function: str = decoded["function"]
                function_data: bytes = decoded["data"]
                log.info(f"will call {function} {function_data}")
                f = getattr(self.api_, function)
                if f is not None:
                    await f(function_data, self, server_connections)
                else:
                    log.error(f'Invalid message: {function} from {peername}')
        except IncompleteReadError:
            log.error("Received EOF, closing connection")
        finally:
            writer.close()

    # Opens up a connection with a server
    async def open_connection(self, url: str, port: int):
        log.info("Calling open_connection")
        self.client_opened_ = True
        async with self.open_lock_:
            if self.open_:
                raise RuntimeError("Already open")
            self.open_ = True
        reader, writer = await asyncio.open_connection(url, port)
        self.open_ = True
        self.reader_ = reader
        self.writer_ = writer
        return asyncio.create_task(self.new_connection(reader, writer))

    async def send(self, function_name: str, data: bytes):
        async with self.write_lock_:
            transformed = transform_to_streamable(data)
            encoded = cbor2.dumps({"function": function_name, "data": transformed})
            log.info(f"Sending message {function_name}: {transformed[:100]}...")
            self.writer_.write(len(encoded).to_bytes(LENGTH_BYTES, "big") + encoded)
            await self.writer_.drain()


async def start_server(api, host: str, port: int):
    async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        connection = ChiaConnection(api)
        server_connections.append(connection)
        await connection.new_connection(reader, writer)

    server = await asyncio.start_server(
        callback, host, port)

    addr = server.sockets[0].getsockname()
    log.info(f'Serving {type(api).__name__} on {addr}')

    async with server:
        await server.serve_forever()
