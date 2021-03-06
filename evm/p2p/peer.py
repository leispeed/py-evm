import asyncio
import logging
import operator
import sha3
import struct
import traceback
from typing import (Callable, Dict, List, Optional, Tuple, Type)  # noqa: F401

from cytoolz import reduceby

import rlp
from rlp import sedes

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.constant_time import bytes_eq

from eth_utils import (
    decode_hex,
)

from eth_keys import (
    datatypes,
    keys,
)

from evm.constants import GENESIS_BLOCK_NUMBER
from evm.db.chain import BaseChainDB
from evm.rlp.headers import BlockHeader
from evm.p2p import auth
from evm.p2p import ecies
from evm.p2p.kademlia import Address, Node
from evm.p2p import protocol  # noqa: F401
from evm.p2p.constants import (
    CONN_IDLE_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    HEADER_LEN,
    MAC_LEN,
    MAX_HEADERS_FETCH,
    REPLY_TIMEOUT,
)
from evm.p2p.exceptions import (
    AuthenticationError,
    EmptyGetBlockHeadersReply,
    HandshakeFailure,
    PeerConnectionLost,
    PeerDisconnected,
    UnknownProtocolCommand,
    UnreachablePeer,
    UselessPeer,
)
from evm.p2p.utils import (
    gen_request_id,
    get_devp2p_cmd_id,
    roundup_16,
    sxor,
)
from evm.p2p.les import (  # noqa: F401
    Announce,
    HeadInfo,
    LESProtocol,
    Status,
)
from evm.p2p.p2p_proto import (
    Disconnect,
    DisconnectReason,
    P2PProtocol,
)


_ReceivedMsgCallbackType = Callable[
    ['BasePeer', protocol.Command, protocol._DecodedMsgType], None]


async def handshake(remote: Node,
                    privkey: datatypes.PrivateKey,
                    peer_class: 'Type[BasePeer]',
                    chaindb: BaseChainDB,
                    network_id: int,
                    received_msg_callback: Optional[_ReceivedMsgCallbackType] = None
                    ) -> 'BasePeer':
    """Perform the auth and P2P handshakes with the given remote.

    Return an instance of the given peer_class (must be a subclass of BasePeer) connected to that
    remote in case both handshakes are successful and at least one of the sub-protocols supported
    by peer_class is also supported by the remote.

    Raises UnreachablePeer if we cannot connect to the peer or UselessPeer if none of the
    sub-protocols supported by us is also supported by the remote.
    """
    try:
        (aes_secret,
         mac_secret,
         egress_mac,
         ingress_mac,
         reader,
         writer
         ) = await auth.handshake(remote, privkey)
    except (ConnectionRefusedError, OSError) as e:
        raise UnreachablePeer(e)
    peer = peer_class(
        remote=remote, privkey=privkey, reader=reader, writer=writer,
        aes_secret=aes_secret, mac_secret=mac_secret, egress_mac=egress_mac,
        ingress_mac=ingress_mac, chaindb=chaindb, network_id=network_id,
        received_msg_callback=received_msg_callback)
    peer.base_protocol.send_handshake()
    msg = await peer.read_msg()
    cmd, decoded = peer.process_msg(msg)
    if isinstance(cmd, Disconnect):
        # Peers may send a disconnect msg before they send the initial P2P handshake (e.g. when
        # they're not accepting more peers), so we special case that here because it's important
        # to distinguish this from a failed handshake (e.g. no matching protocols, etc).
        raise PeerDisconnected("Peer disconnected before completing handshake: {}".format(
            decoded['reason_name']))
    if len(peer.enabled_sub_protocols) == 0:
        raise UselessPeer("No matching sub-protocols")
    for proto in peer.enabled_sub_protocols:
        proto.send_handshake(peer._local_chain_info)
    return peer


class BasePeer:
    logger = logging.getLogger("evm.p2p.peer.Peer")
    conn_idle_timeout = CONN_IDLE_TIMEOUT
    reply_timeout = REPLY_TIMEOUT
    max_headers_fetch = MAX_HEADERS_FETCH
    # Must be defined in subclasses.
    _supported_sub_protocols = []  # type: List[Type[protocol.Protocol]]
    # FIXME: Must be configurable.
    listen_port = 30303

    def __init__(self,
                 remote: Node,
                 privkey: datatypes.PrivateKey,
                 reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter,
                 aes_secret: bytes,
                 mac_secret: bytes,
                 egress_mac: sha3.keccak_256,
                 ingress_mac: sha3.keccak_256,
                 chaindb: BaseChainDB,
                 network_id: int,
                 received_msg_callback: Optional[_ReceivedMsgCallbackType] = None
                 ) -> None:
        self._finished = asyncio.Event()
        self._pending_replies = {}  # type: Dict[int, Callable[[protocol._DecodedMsgType], None]]
        self.remote = remote
        self.privkey = privkey
        self.reader = reader
        self.writer = writer
        self.base_protocol = P2PProtocol(self)
        self.chaindb = chaindb
        self.network_id = network_id
        self.received_msg_callback = received_msg_callback
        # The sub protocols that have been enabled for this peer; will be populated when
        # we receive the initial hello msg.
        self.enabled_sub_protocols = []  # type: List[protocol.Protocol]

        self.egress_mac = egress_mac
        self.ingress_mac = ingress_mac
        # FIXME: Yes, the encryption is insecure, see: https://github.com/ethereum/devp2p/issues/32
        iv = b"\x00" * 16
        aes_cipher = Cipher(algorithms.AES(aes_secret), modes.CTR(iv), default_backend())
        self.aes_enc = aes_cipher.encryptor()
        self.aes_dec = aes_cipher.decryptor()
        mac_cipher = Cipher(algorithms.AES(mac_secret), modes.ECB(), default_backend())
        self.mac_enc = mac_cipher.encryptor().update

    @property
    def genesis(self) -> BlockHeader:
        genesis_hash = self.chaindb.lookup_block_hash(GENESIS_BLOCK_NUMBER)
        return self.chaindb.get_block_header_by_hash(genesis_hash)

    @property
    def _local_chain_info(self) -> 'ChainInfo':
        genesis = self.genesis
        head = self.chaindb.get_canonical_head()
        return ChainInfo(
            block_number=head.block_number,
            block_hash=head.hash,
            total_difficulty=self.chaindb.get_score(head.hash),
            genesis_hash=genesis.hash,
        )

    @property
    def capabilities(self) -> List[Tuple[bytes, int]]:
        return [(klass.name, klass.version) for klass in self._supported_sub_protocols]

    async def wait_for_reply(self, request_id):
        reply = None
        got_reply = asyncio.Event()

        def callback(r):
            nonlocal reply
            reply = r
            got_reply.set()

        self._pending_replies[request_id] = callback
        await asyncio.wait_for(got_reply.wait(), self.reply_timeout)
        return reply

    def get_protocol_for(self, cmd_id: int) -> protocol.Protocol:
        """Return the protocol to which the cmd_id belongs.

        Every sub-protocol enabled for a peer defines a cmd ID offset, which is agreed on by both
        sides during the base protocol's handshake. Here we use that to look up the protocol to
        which cmd_id belongs. See the match_protocols() method for the details on how the peers
        agree on which sub protocols to enable and what cmd ID offsets to use for them.
        """
        if cmd_id < self.base_protocol.cmd_length:
            return self.base_protocol
        for proto in self.enabled_sub_protocols:
            if cmd_id >= proto.cmd_id_offset and cmd_id < (proto.cmd_id_offset + proto.cmd_length):
                return proto
        return None

    async def read(self, n: int) -> bytes:
        self.logger.debug("Waiting for %s bytes from %s", n, self.remote)
        try:
            data = await asyncio.wait_for(self.reader.readexactly(n), self.conn_idle_timeout)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            raise PeerConnectionLost("EOF reading from stream")
        return data

    async def start(self, finished_callback: Optional[Callable[['BasePeer'], None]] = None) -> None:
        try:
            await self.read_loop()
        except Exception as e:
            self.logger.error(
                "Unexpected error when handling remote msg: %s", traceback.format_exc())
        finally:
            self._finished.set()
            if finished_callback is not None:
                finished_callback(self)

    def close(self):
        """Close this peer's reader/writer streams.

        This will cause the peer to stop in case it is running.
        """
        self.reader.feed_eof()
        self.writer.close()

    async def stop(self):
        self.close()
        await self._finished.wait()

    async def read_loop(self):
        while True:
            try:
                msg = await self.read_msg()
            except (PeerConnectionLost, asyncio.TimeoutError) as e:
                self.logger.info(
                    "%s stopped responding (%s), disconnecting", self.remote, repr(e))
                return

            try:
                self.process_msg(msg)
            except HandshakeFailure as e:
                self.disconnect(e.reason)
                return

    async def read_msg(self) -> bytes:
        header_data = await self.read(HEADER_LEN + MAC_LEN)
        header = self.decrypt_header(header_data)
        frame_size = self.get_frame_size(header)
        # The frame_size specified in the header does not include the padding to 16-byte boundary,
        # so need to do this here to ensure we read all the frame's data.
        read_size = roundup_16(frame_size)
        frame_data = await self.read(read_size + MAC_LEN)
        return self.decrypt_body(frame_data, frame_size)

    def process_msg(self, msg: bytes) -> Tuple[protocol.Command, protocol._DecodedMsgType]:
        cmd_id = get_devp2p_cmd_id(msg)
        self.logger.debug("Got msg with cmd_id: %s", cmd_id)
        proto = self.get_protocol_for(cmd_id)
        if proto is None:
            raise UnknownProtocolCommand("No protocol found for cmd_id {}".format(cmd_id))

        cmd, decoded = proto.process(cmd_id, msg)
        if decoded is not None:
            # Check if this is a reply we're waiting for and, if so, call the callback for this
            # request_id.
            request_id = decoded.get('request_id')
            if request_id is not None and request_id in self._pending_replies:
                callback = self._pending_replies.pop(request_id)
                callback(decoded)

        if self.received_msg_callback is not None:
            self.received_msg_callback(self, cmd, decoded)
        return cmd, decoded

    def process_p2p_handshake(self, decoded_msg: protocol._DecodedMsgType) -> None:
        remote_capabilities = decoded_msg['capabilities']
        self.match_protocols(remote_capabilities)
        if len(self.enabled_sub_protocols) == 0:
            self.logger.debug(
                "No matching capabilities between us (%s) and %s (%s), disconnecting",
                self.capabilities, self.remote, remote_capabilities)
            raise HandshakeFailure(DisconnectReason.useless_peer)
        else:
            self.logger.debug(
                "Finished P2P handshake with %s; matching protocols: %s",
                self.remote, [(p.name, p.version) for p in self.enabled_sub_protocols])

    def encrypt(self, header: bytes, frame: bytes) -> bytes:
        if len(header) != HEADER_LEN:
            raise ValueError("Unexpected header length: {}".format(len(header)))

        header_ciphertext = self.aes_enc.update(header)
        mac_secret = self.egress_mac.digest()[:HEADER_LEN]
        self.egress_mac.update(sxor(self.mac_enc(mac_secret), header_ciphertext))
        header_mac = self.egress_mac.digest()[:HEADER_LEN]

        frame_ciphertext = self.aes_enc.update(frame)
        self.egress_mac.update(frame_ciphertext)
        fmac_seed = self.egress_mac.digest()[:HEADER_LEN]

        mac_secret = self.egress_mac.digest()[:HEADER_LEN]
        self.egress_mac.update(sxor(self.mac_enc(mac_secret), fmac_seed))
        frame_mac = self.egress_mac.digest()[:HEADER_LEN]

        return header_ciphertext + header_mac + frame_ciphertext + frame_mac

    def decrypt_header(self, data: bytes) -> bytes:
        if len(data) != HEADER_LEN + MAC_LEN:
            raise ValueError("Unexpected header length: {}".format(len(data)))

        header_ciphertext = data[:HEADER_LEN]
        header_mac = data[HEADER_LEN:]
        mac_secret = self.ingress_mac.digest()[:HEADER_LEN]
        aes = self.mac_enc(mac_secret)[:HEADER_LEN]
        self.ingress_mac.update(sxor(aes, header_ciphertext))
        expected_header_mac = self.ingress_mac.digest()[:HEADER_LEN]
        if not bytes_eq(expected_header_mac, header_mac):
            raise AuthenticationError('Invalid header mac')
        return self.aes_dec.update(header_ciphertext)

    def decrypt_body(self, data: bytes, body_size: int) -> bytes:
        read_size = roundup_16(body_size)
        if len(data) < read_size + MAC_LEN:
            raise ValueError('Insufficient body length; Got {}, wanted {}'.format(
                len(data), (read_size + MAC_LEN)))

        frame_ciphertext = data[:read_size]
        frame_mac = data[read_size:read_size + MAC_LEN]

        self.ingress_mac.update(frame_ciphertext)
        fmac_seed = self.ingress_mac.digest()[:MAC_LEN]
        self.ingress_mac.update(sxor(self.mac_enc(fmac_seed), fmac_seed))
        expected_frame_mac = self.ingress_mac.digest()[:MAC_LEN]
        if not bytes_eq(expected_frame_mac, frame_mac):
            raise AuthenticationError('Invalid frame mac')
        return self.aes_dec.update(frame_ciphertext)[:body_size]

    def get_frame_size(self, header: bytes) -> int:
        # The frame size is encoded in the header as a 3-byte int, so before we unpack we need
        # to prefix it with an extra byte.
        encoded_size = b'\x00' + header[:3]
        (size,) = struct.unpack(b'>I', encoded_size)
        return size

    def send(self, header: bytes, body: bytes) -> None:
        cmd_id = rlp.decode(body[:1], sedes=sedes.big_endian_int)
        self.logger.debug("Sending msg with cmd_id: {}".format(cmd_id))
        self.writer.write(self.encrypt(header, body))

    def disconnect(self, reason: DisconnectReason) -> None:
        """Send a disconnect msg to the remote node and stop this Peer.

        :param reason: An item from the DisconnectReason enum.
        """
        if not isinstance(reason, DisconnectReason):
            raise ValueError(
                "Reason must be an item of DisconnectReason, got {}".format(reason))
            self.logger.debug("Disconnecting from remote peer; reason: %s", reason.value)
        self.base_protocol.send_disconnect(reason.value)
        self.close()

    def match_protocols(self, remote_capabilities: List[Tuple[bytes, int]]):
        """Match the sub-protocols supported by this Peer with the given remote capabilities.

        Every sub-protocol and remote-capability are defined by a protocol name and version. This
        method will get the match with the highest version for every protocol, sort them
        in ascending alphabetical order and add a Protocol instance for the protocol with that
        name/version to this peer's list of enabled sub protocols. Each Protocol instance will
        also have a cmd ID offset, defined as the offset of the previous item (0 for the base
        protocol) plus the protocol's cmd length (i.e. number of commands).
        """
        matching_capabilities = set(self.capabilities).intersection(remote_capabilities)
        higher_matching = reduceby(
            key=operator.itemgetter(0),
            binop=lambda a, b: a if a[1] > b[1] else b,
            seq=matching_capabilities)
        sub_protocols_by_name_and_version = dict(
            ((klass.name, klass.version), klass) for klass in self._supported_sub_protocols)
        offset = self.base_protocol.cmd_length
        for name, version in sorted(higher_matching.values()):
            proto_klass = sub_protocols_by_name_and_version[(name, version)]
            self.enabled_sub_protocols.append(proto_klass(self, offset))
            offset += proto_klass.cmd_length

    def __str__(self):
        return "{} {}".format(self.__class__.__name__, self.remote)


class LESPeer(BasePeer):
    _les_proto = None
    _supported_sub_protocols = [LESProtocol]

    async def fetch_headers_starting_at(self, start_block: int) -> List[BlockHeader]:
        """Fetches up to self.max_headers_fetch starting at start_block.

        Returns a list containing those headers in ascending order of block number.
        """
        request_id = gen_request_id()
        self.les_proto.send_get_block_headers(
            start_block, self.max_headers_fetch, request_id, reverse=False)
        reply = await self.wait_for_reply(request_id)
        if len(reply['headers']) == 0:
            raise EmptyGetBlockHeadersReply(
                "No headers in reply. start_block=={}".format(start_block))
        self.logger.info(
            "fetched headers from %s to %s", reply['headers'][0].block_number,
            reply['headers'][-1].block_number)
        return reply['headers']

    @property
    def les_proto(self):
        """Return the LESProtocol available for this peer.

        The available LESProtocol will be the highest version supported by this class and the
        remote peer. It is available only after the initial P2P handshake.
        """
        # Here we assume this class supports one version of the LESProtocol that is also
        # supported by the remote peer, which should be ok since LES is the only sub-protocol
        # supported by this class and the handshake will ensure we have a matching sub-protocol
        # with the remote peer.
        assert len(self.enabled_sub_protocols) > 0
        if self._les_proto is None:
            for proto in self.enabled_sub_protocols:
                if proto.name == LESProtocol.name:
                    self._les_proto = proto
            if self._les_proto is None:
                raise Exception(
                    "We assumed LESProtocol was supported, but it isn't. See comment above")
        return self._les_proto


class ChainInfo:
    def __init__(self, block_number, block_hash, total_difficulty, genesis_hash):
        self.block_number = block_number
        self.block_hash = block_hash
        self.total_difficulty = total_difficulty
        self.genesis_hash = genesis_hash


if __name__ == "__main__":
    """
    Create a Peer instance connected to a local geth instance and log messages exchanged with it.

    Use the following command line to run geth:

        ./build/bin/geth -vmodule p2p=4,p2p/discv5=0,eth/*=0 \
          -nodekeyhex 45a915e4d060149eb4365960e6a7a45f334393093061116b197e3240065ff2d8 \
          -testnet -lightserv 90
    """
    import argparse
    from evm.chains.ropsten import RopstenChain, ROPSTEN_GENESIS_HEADER
    from evm.db.backends.memory import MemoryDB

    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

    # The default remoteid can be used if you pass nodekeyhex as above to geth.
    nodekey = keys.PrivateKey(decode_hex(
        "45a915e4d060149eb4365960e6a7a45f334393093061116b197e3240065ff2d8"))
    remoteid = nodekey.public_key.to_hex()
    parser = argparse.ArgumentParser()
    parser.add_argument('-remoteid', type=str, default=remoteid)
    args = parser.parse_args()

    remote = Node(
        keys.PublicKey(decode_hex(args.remoteid)),
        Address('127.0.0.1', 30303, 30303))
    chaindb = BaseChainDB(MemoryDB())
    chaindb.persist_header_to_db(ROPSTEN_GENESIS_HEADER)
    network_id = RopstenChain.network_id
    loop = asyncio.get_event_loop()
    try:
        peer = loop.run_until_complete(
            asyncio.wait_for(
                handshake(remote, ecies.generate_privkey(), LESPeer, chaindb, network_id),
                HANDSHAKE_TIMEOUT))
        loop.run_until_complete(peer.start())
    except KeyboardInterrupt:
        pass

    loop.run_until_complete(peer.stop())
    loop.close()
