from p2pfs.core.server import MessageServer, MessageType
import socket
import logging
import os.path
import threading
from queue import Queue
import math
import pybase64
import json
import hashlib
import asyncio
logger = logging.getLogger(__name__)


class Peer(MessageServer):
    _CHUNK_SIZE = 512 * 1024
    _HASH_FUNC = hashlib.sha256

    def __init__(self, host, port, server, server_port, loop=None):
        super().__init__(host, port, loop=loop)
        self._server_config = (server, server_port)
        self._server_reader, self._server_writer = None, None

        # (remote filename) <-> (local filename)
        self._file_map = {}

        self._pending_publish = set()

    async def start(self):
        # connect to server
        try:
            self._server_reader, self._server_writer = \
                await asyncio.open_connection(*self._server_config, loop=self._loop)
        except ConnectionRefusedError:
            logger.error('Server connection refused!')
            return False
        # start the internal server
        await super().start()
        # send out register message
        logger.info('Requesting to register')
        await self._write_message(self._server_writer, {
            'type': MessageType.REQUEST_REGISTER,
            'address': self._server_config
        })
        message = await self._read_message(self._server_reader)
        assert MessageType(message['type']) == MessageType.REPLY_REGISTER
        logger.info('Successfully registered.')
        return True

    async def publish(self, local_file, remote_name=None):
        if not os.path.exists(local_file):
            return False, 'File {} doesn\'t exist'.format(local_file)

        _, remote_name = os.path.split(local_file) if remote_name is None else remote_name

        if remote_name in self._pending_publish:
            return False, 'Publish file {} already in progress.'.format(local_file)

        self._pending_publish.add(remote_name)

        # send out the request packet
        await self._write_message(self._server_writer, {
            'type': MessageType.REQUEST_PUBLISH,
            'filename': remote_name,
            'fileinfo': {'size': os.stat(local_file).st_size},
            'chunknum': math.ceil(os.stat(local_file).st_size / Peer._CHUNK_SIZE)
        })

        message = await self._read_message(self._server_reader)
        assert MessageType(message['type']) == MessageType.REPLY_PUBLISH
        is_success, message = message['result'], message['message']

        if is_success:
            self._file_map[remote_name] = local_file
            logger.info('File {} published on server with name {}'.format(local_file, remote_name))
        else:
            logger.info('File {} failed to publish, {}'.format(local_file, message))

        self._pending_publish.remove(remote_name)
        return is_success, message

    async def list_file(self):
        await self._write_message(self._server_writer, {
            'type': MessageType.REQUEST_FILE_LIST,
        })
        message = await self._read_message(self._server_reader)
        assert MessageType(message['type']) == MessageType.REPLY_FILE_LIST
        return message['file_list']

    async def download(self, file, destination, reporthook=None):
        # request for file list
        file_list = await self.list_file()
        if file not in file_list:
            return False, 'Requested file {} does not exist, try list_file?'.format(file)

        await self._write_message(self._server_writer, {
            'type': MessageType.REQUEST_FILE_LOCATION,
            'filename': file
        })

        message = await self._read_message(self._server_reader)
        assert MessageType(message['type']) == MessageType.REPLY_FILE_LOCATION
        fileinfo, chunkinfo = message['fileinfo'], message['chunkinfo']
        logger.debug('{}: {} ==> {}'.format(file, fileinfo, chunkinfo))

        totalchunknum = math.ceil(fileinfo['size'] / Peer._CHUNK_SIZE)

        # TODO: decide which peer to request chunk
        peers = {}
        # TODO: make it parallel
        for chunknum in range(totalchunknum):
            for peer_address, possessed_chunks in chunkinfo.items():
                if chunknum in possessed_chunks:
                    if peer_address not in peers:
                        # peer_address is a string, since JSON requires keys being strings
                        peers[peer_address] = await asyncio.open_connection(*json.loads(peer_address), loop=self._loop)
                    # write the message to ask the chunk
                    await self._write_message(peers[peer_address][1], {
                        'type': MessageType.PEER_REQUEST_CHUNK,
                        'filename': file,
                        'chunknum': chunknum
                    })
                    break

            # TODO: update chunkinfo after receiving each chunk
            with open(destination + '.temp', 'wb') as dest_file:
                self._file_map[file] = destination
                for i in range(totalchunknum):
                    for address, (reader, _) in peers:
                        assert isinstance(reader, asyncio.StreamReader)
                        while not reader.at_eof():
                            message = await self._read_message(reader)
                            number, data, digest = message['chunknum'], message['data'], message['digest']
                            raw_data = pybase64.b64decode(data.encode('utf-8'), validate=True)
                            # TODO: handle if corrupted
                            if Peer._HASH_FUNC(raw_data).hexdigest() != digest:
                                assert False
                            dest_file.seek(number * Peer._CHUNK_SIZE, 0)
                            dest_file.write(raw_data)
                            dest_file.flush()
                            # send request chunk register to server
                            await self._write_message(self._server_writer, {
                                'type': MessageType.REQUEST_CHUNK_REGISTER,
                                'filename': file,
                                'chunknum': number
                            })
                            if reporthook:
                                reporthook(i + 1, Peer._CHUNK_SIZE, fileinfo['size'])
                            logger.debug('Got {}\'s chunk # {}'.format(file, number))

            # change the temp file into the actual file
            os.rename(destination + '.temp', destination)

            # close the connections
            for _, (_, writer) in peers:
                writer.close()
                await writer.wait_closed()

        return True, 'File {} dowloaded to {}'.format(file, destination)

    async def _process_connection(self, reader, writer):
        assert isinstance(reader, asyncio.StreamReader) and isinstance(writer, asyncio.StreamWriter)
        while not reader.at_eof():
            message = await self._read_message(reader)
            message_type = MessageType(message['type'])
            if message_type == MessageType.PEER_REQUEST_CHUNK:
                assert message['filename'] in self._file_map, 'File {} requested does not exist'.format(message['filename'])
                local_file = self._file_map[message['filename']]
                with open(local_file, 'rb') as f:
                    f.seek(message['chunknum'] * Peer._CHUNK_SIZE, 0)
                    raw_data = f.read(Peer._CHUNK_SIZE)
                await self._write_message(writer, {
                    'type': MessageType.PEER_REPLY_CHUNK,
                    'filename': message['filename'],
                    'chunknum': message['chunknum'],
                    'data': pybase64.b64encode(raw_data).decode('utf-8'),
                    'digest': Peer._HASH_FUNC(raw_data).hexdigest()
                })
            else:
                logger.error('Undefined message with type {}, full message: {}'.format(message['type'], message))