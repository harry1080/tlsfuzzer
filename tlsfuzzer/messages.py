# Author: Hubert Kario, (c) 2015
# Released under Gnu GPL v2.0, see LICENSE file for details

"""Set of object for generating TLS messages to send"""

from tlslite.messages import ClientHello, ClientKeyExchange, ChangeCipherSpec,\
        Finished, Alert, ApplicationData
from tlslite.constants import AlertLevel, AlertDescription, ContentType, \
        ExtensionType
from tlslite.extensions import TLSExtension
from tlslite.messagesocket import MessageSocket
from tlslite.defragmenter import Defragmenter
from tlslite.mathtls import calcMasterSecret, calcFinished
from tlslite.handshakehashes import HandshakeHashes
from tlslite.utils.codec import Writer
from .tree import TreeNode
import socket

# TODO move the following to tlslite proper
class RenegotiationInfoExtension(TLSExtension):

    """Implementation of the Renegotiation Info extension

    Handling of the Secure Renegotiation extension from RFC 5746
    """

    def __init__(self):
        self.renegotiated_connection = None
        self.serverType = False

    @property
    def extType(self):
        """Return the extension type, 0xff01"""
        return ExtensionType.renegotiation_info

    @property
    def extData(self):
        """Return the extension payload"""
        if self.renegotiated_connection is None:
            return bytearray(0)

        writer = Writer()
        writer.addVarSeq(self.renegotiated_connection, 1, 1)

        return writer.bytes

    def create(self, renegotiated_connection=None):
        """Set the payload of the extension"""
        self.renegotiated_connection = renegotiated_connection
        return self

    def parse(self, parser):
        """Parse the extension from on the wire data"""
        self.renegotiated_connection = parser.getVarBytes(1)
        return self

class Command(TreeNode):

    """Command objects"""

    def __init__(self):
        super(Command, self).__init__()

    def is_command(self):
        """Define object as a command node"""
        return True

    def is_expect(self):
        """Define object as a command node"""
        return False

    def is_generator(self):
        """Define object as a command node"""
        return False

    def process(self, state):
        """Change the state of the connection"""
        raise NotImplementedError("Subclasses need to implement this!")

class Connect(Command):

    """Object used to connect to a TCP server"""

    def __init__(self, hostname, port):
        """Provide minimal settings needed to connect to other peer"""
        super(Connect, self).__init__()
        self.hostname = hostname
        self.port = port
        # note that this is just the default record layer message,
        # changed to version from server hello as soon as it is received
        self.version = (3, 0)

    def process(self, state):
        """Connect to a server"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((self.hostname, self.port))

        defragmenter = Defragmenter()
        defragmenter.addStaticSize(ContentType.alert, 2)
        defragmenter.addStaticSize(ContentType.change_cipher_spec, 1)
        defragmenter.addDynamicSize(ContentType.handshake, 1, 3)

        state.msg_sock = MessageSocket(sock, defragmenter)

        state.msg_sock.version = self.version

class Close(Command):

    """Object used to close a TCP connection"""

    def __init__(self):
        super(Close, self).__init__()

    def process(self, state):
        """Close currently open connection"""
        state.msg_sock.close()

class ResetHandshakeHashes(Command):

    """Object used to reset current state of handshake hashes to zero"""

    def __init__(self):
        super(ResetHandshakeHashes, self).__init__()

    def process(self, state):
        """Reset current running handshake protocol hashes"""
        state.handshake_hashes = HandshakeHashes()

class SetMaxRecordSize(Command):

    """Change the Record Layer to send records of non standard size"""

    def __init__(self, max_size=None):
        """Set the maximum record layer message size, no option for default"""
        super(SetMaxRecordSize, self).__init__()
        self.max_size = max_size

    def process(self, state):
        """Change the size of messages in record layer"""
        if self.max_size is None:
            # the maximum for SSLv3, TLS1.0, TLS1.1 and TLS1.2
            state.msg_sock.recordSize = 2**14
        else:
            state.msg_sock.recordSize = self.max_size

class MessageGenerator(TreeNode):

    """Message generator objects"""

    def __init__(self):
        super(MessageGenerator, self).__init__()
        self.msg = None

    def is_command(self):
        """Define object as a generator node"""
        return False

    def is_expect(self):
        """Define object as a generator node"""
        return False

    def is_generator(self):
        """Define object as a generator node"""
        return True

    def generate(self, state):
        """Return a message ready to write to socket"""
        raise NotImplementedError("Subclasses need to implement this!")

    def post_send(self, state):
        """Modify the state after sending the message"""
        # since most messages don't require any post-send modifications
        # create a no-op default action
        pass

class HandshakeProtocolMessageGenerator(MessageGenerator):

    """Message generator for TLS Handshake protocol messages"""

    def post_send(self, state):
        """Update handshake hashes after sending"""
        super(HandshakeProtocolMessageGenerator, self).post_send(state)

        state.handshake_hashes.update(self.msg.write())
        state.handshake_messages.append(self.msg)

class ClientHelloGenerator(HandshakeProtocolMessageGenerator):

    """Generator for TLS handshake protocol Client Hello messages"""

    def __init__(self, ciphers=None, extensions=None):
        super(ClientHelloGenerator, self).__init__()
        if ciphers is None:
            ciphers = []
        self.ciphers = ciphers
        self.version = (3, 3)
        self.extensions = extensions

    def _generate_extensions(self, state):
        """Convert extension generators to extension objects"""
        extensions = []
        for ext_id in self.extensions:
            if self.extensions[ext_id] is not None:
                extensions.append(self.extensions[ext_id](state))
                continue

            if ext_id == ExtensionType.renegotiation_info:
                ext = RenegotiationInfoExtension().create(state.client_verify_data)
                extensions.append(ext)
            else:
                extensions.append(TLSExtension().create(ext_id, bytearray(0)))

        return extensions

    def generate(self, state):
        if not state.client_random:
            state.client_random = bytearray(32)

        extensions = None
        if self.extensions is not None:
            extensions = self._generate_extensions(state)

        clnt_hello = ClientHello().create(self.version,
                                          state.client_random,
                                          bytearray(0),
                                          self.ciphers,
                                          extensions=extensions)

        self.msg = clnt_hello

        return clnt_hello

class ClientKeyExchangeGenerator(HandshakeProtocolMessageGenerator):

    """Generator for TLS handshake protocol Client Key Exchange messages"""

    def __init__(self, cipher=None, version=None):
        super(ClientKeyExchangeGenerator, self).__init__()
        self.cipher = cipher
        self.version = version
        self.premaster_secret = bytearray(48)

    def generate(self, status):
        if self.version is None:
            self.version = status.version

        if self.cipher is None:
            self.cipher = status.cipher

        cke = ClientKeyExchange(self.cipher, self.version)
        premaster_secret = self.premaster_secret
        assert len(premaster_secret) > 1

        premaster_secret[0] = self.version[0]
        premaster_secret[1] = self.version[1]

        status.premaster_secret = premaster_secret

        public_key = status.get_server_public_key()

        premaster_secret = public_key.encrypt(premaster_secret)

        cke.createRSA(premaster_secret)

        self.msg = cke

        return cke

class ChangeCipherSpecGenerator(MessageGenerator):

    """Generator for TLS Change Cipher Spec messages"""

    def generate(self, status):
        ccs = ChangeCipherSpec()
        return ccs

    def post_send(self, status):
        cipher_suite = status.cipher

        master_secret = calcMasterSecret(status.version,
                                         cipher_suite,
                                         status.premaster_secret,
                                         status.client_random,
                                         status.server_random)

        status.master_secret = master_secret

        status.msg_sock.calcPendingStates(cipher_suite,
                                          master_secret,
                                          status.client_random,
                                          status.server_random,
                                          None)

        status.msg_sock.changeWriteState()

class FinishedGenerator(HandshakeProtocolMessageGenerator):

    """Generator for TLS handshake protocol Finished messages"""

    def __init__(self, protocol=None):
        super(FinishedGenerator, self).__init__()
        self.protocol = protocol

    def generate(self, status):
        finished = Finished(self.protocol)

        verify_data = calcFinished(status.version,
                                   status.master_secret,
                                   status.cipher,
                                   status.handshake_hashes,
                                   status.client)

        status.client_verify_data = verify_data

        finished.create(verify_data)

        self.msg = finished

        return finished

class AlertGenerator(MessageGenerator):

    """Generator for TLS Alert messages"""

    def __init__(self, level=AlertLevel.warning,
                 description=AlertDescription.close_notify):
        super(AlertGenerator, self).__init__()
        self.level = level
        self.description = description

    def generate(self, status):
        alert = Alert().create(self.description, self.level)
        return alert

class ApplicationDataGenerator(MessageGenerator):

    """Generator for TLS Application Data messages"""

    def __init__(self, payload):
        super(ApplicationDataGenerator, self).__init__()
        self.payload = payload

    def generate(self, status):
        app_data = ApplicationData().create(self.payload)
        return app_data
