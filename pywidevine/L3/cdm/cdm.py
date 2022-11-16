import base64

import os
import time
import binascii

from google.protobuf.message import DecodeError
from google.protobuf import text_format

from pywidevine.L3.cdm.formats import wv_proto2_pb2 as wv_proto2
from pywidevine.L3.cdm.session import Session
from pywidevine.L3.cdm.key import Key
from Cryptodome.Random import get_random_bytes
from Cryptodome.Random import random
from Cryptodome.Cipher import PKCS1_OAEP, AES
from Cryptodome.Hash import CMAC, SHA256, HMAC, SHA1
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pss
from Cryptodome.Util import Padding
import logging

class Cdm:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.sessions = {}

    def open_session(self, init_data_b64, device, raw_init_data = None, offline=False):
        self.logger.debug("open_session(init_data_b64={}, device={}".format(init_data_b64, device))
        self.logger.info("opening new cdm session")
        if device.session_id_type == 'android':
            # format: 16 random hexdigits, 2 digit counter, 14 0s
            rand_ascii = ''.join(random.choice('ABCDEF0123456789') for _ in range(16))
            counter = '01' # this resets regularly so its fine to use 01
            rest = '00000000000000'
            session_id = rand_ascii + counter + rest
            session_id = session_id.encode('ascii')
        elif device.session_id_type == 'chrome':
            rand_bytes = get_random_bytes(16)
            session_id = rand_bytes
        else:
            # other formats NYI
            self.logger.error("device type is unusable")
            return 1
        if raw_init_data and isinstance(raw_init_data, (bytes, bytearray)):
            # used for NF key exchange, where they don't provide a valid PSSH
            init_data = raw_init_data
            self.raw_pssh = True
        else:
            init_data = self._parse_init_data(init_data_b64)
            self.raw_pssh = False

        if init_data:
            new_session = Session(session_id, init_data, device, offline)
        else:
            self.logger.error("unable to parse init data")
            return 1
        self.sessions[session_id] = new_session
        self.logger.info("session opened and init data parsed successfully")
        return session_id

    def _parse_init_data(self, init_data_b64):
        parsed_init_data = wv_proto2.WidevineCencHeader()
        try:
            self.logger.debug("trying to parse init_data directly")
            parsed_init_data.ParseFromString(base64.b64decode(init_data_b64)[32:])
        except DecodeError:
            self.logger.debug("unable to parse as-is, trying with removed pssh box header")
            try:
                id_bytes = parsed_init_data.ParseFromString(base64.b64decode(init_data_b64)[32:])
            except DecodeError:
                self.logger.error("unable to parse, unsupported init data format")
                return None
        self.logger.debug("init_data:")
        for line in text_format.MessageToString(parsed_init_data).splitlines():
            self.logger.debug(line)
        return parsed_init_data

    def close_session(self, session_id):
        self.logger.debug("close_session(session_id={})".format(session_id))
        self.logger.info("closing cdm session")
        if session_id in self.sessions:
            self.sessions.pop(session_id)
            self.logger.info("cdm session closed")
            return 0
        else:
            self.logger.info("session {} not found".format(session_id))
            return 1

    def set_service_certificate(self, session_id, cert_b64):
        self.logger.debug("set_service_certificate(session_id={}, cert={})".format(session_id, cert_b64))
        self.logger.info("setting service certificate")

        if session_id not in self.sessions:
            self.logger.error("session id doesn't exist")
            return 1

        session = self.sessions[session_id]

        message = wv_proto2.SignedMessage()

        try:
            message.ParseFromString(base64.b64decode(cert_b64))
        except DecodeError:
            self.logger.error("failed to parse cert as SignedMessage")

        service_certificate = wv_proto2.SignedDeviceCertificate()

        if message.Type:
            self.logger.debug("service cert provided as signedmessage")
            try:
                service_certificate.ParseFromString(message.Msg)
            except DecodeError:
                self.logger.error("failed to parse service certificate")
                return 1
        else:
            self.logger.debug("service cert provided as signeddevicecertificate")
            try:
                service_certificate.ParseFromString(base64.b64decode(cert_b64))
            except DecodeError:
                self.logger.error("failed to parse service certificate")
                return 1

        self.logger.debug("service certificate:")
        for line in text_format.MessageToString(service_certificate).splitlines():
            self.logger.debug(line)

        session.service_certificate = service_certificate
        session.privacy_mode = True

        return 0

    def get_license_request(self, session_id):
        self.logger.debug("get_license_request(session_id={})".format(session_id))
        self.logger.info("getting license request")

        if session_id not in self.sessions:
            self.logger.error("session ID does not exist")
            return 1

        session = self.sessions[session_id]

        # raw pssh will be treated as bytes and not parsed
        if self.raw_pssh:
            license_request = wv_proto2.SignedLicenseRequestRaw()
        else:
            license_request = wv_proto2.SignedLicenseRequest()
        client_id = wv_proto2.ClientIdentification()

        if not os.path.exists(session.device_config.device_client_id_blob_filename):
            self.logger.error("no client ID blob available for this device")
            return 1

        with open(session.device_config.device_client_id_blob_filename, "rb") as f:
            try:
                cid_bytes = client_id.ParseFromString(f.read())
            except DecodeError:
                self.logger.error("client id failed to parse as protobuf")
                return 1

        self.logger.debug("building license request")
        if not self.raw_pssh:
            license_request.Type = wv_proto2.SignedLicenseRequest.MessageType.Value('LICENSE_REQUEST')
            license_request.Msg.ContentId.CencId.Pssh.CopyFrom(session.init_data)
        else:
            license_request.Type = wv_proto2.SignedLicenseRequestRaw.MessageType.Value('LICENSE_REQUEST')
            license_request.Msg.ContentId.CencId.Pssh = session.init_data # bytes

        if session.offline:
           license_type = wv_proto2.LicenseType.Value('OFFLINE')
        else:
           license_type = wv_proto2.LicenseType.Value('DEFAULT')
        license_request.Msg.ContentId.CencId.LicenseType = license_type
        license_request.Msg.ContentId.CencId.RequestId = session_id
        license_request.Msg.Type = wv_proto2.LicenseRequest.RequestType.Value('NEW')
        license_request.Msg.RequestTime = int(time.time())
        license_request.Msg.ProtocolVersion = wv_proto2.ProtocolVersion.Value('CURRENT')
        if session.device_config.send_key_control_nonce:
            license_request.Msg.KeyControlNonce = random.randrange(1, 2**31)

        if session.privacy_mode:
            if session.device_config.vmp:
                self.logger.debug("vmp required, adding to client_id")
                self.logger.debug("reading vmp hashes")
                vmp_hashes = wv_proto2.FileHashes()
                with open(session.device_config.device_vmp_blob_filename, "rb") as f:
                    try:
                        vmp_bytes = vmp_hashes.ParseFromString(f.read())
                    except DecodeError:
                        self.logger.error("vmp hashes failed to parse as protobuf")
                        return 1
                client_id._FileHashes.CopyFrom(vmp_hashes)
            self.logger.debug("privacy mode & service certificate loaded, encrypting client id")
            self.logger.debug("unencrypted client id:")
            for line in text_format.MessageToString(client_id).splitlines():
                self.logger.debug(line)
            cid_aes_key = get_random_bytes(16)
            cid_iv = get_random_bytes(16)

            cid_cipher = AES.new(cid_aes_key, AES.MODE_CBC, cid_iv)

            encrypted_client_id = cid_cipher.encrypt(Padding.pad(client_id.SerializeToString(), 16))

            service_public_key = RSA.importKey(session.service_certificate._DeviceCertificate.PublicKey)

            service_cipher = PKCS1_OAEP.new(service_public_key)

            encrypted_cid_key = service_cipher.encrypt(cid_aes_key)

            encrypted_client_id_proto = wv_proto2.EncryptedClientIdentification()

            encrypted_client_id_proto.ServiceId = session.service_certificate._DeviceCertificate.ServiceId
            encrypted_client_id_proto.ServiceCertificateSerialNumber = session.service_certificate._DeviceCertificate.SerialNumber
            encrypted_client_id_proto.EncryptedClientId = encrypted_client_id
            encrypted_client_id_proto.EncryptedClientIdIv = cid_iv
            encrypted_client_id_proto.EncryptedPrivacyKey = encrypted_cid_key

            license_request.Msg.EncryptedClientId.CopyFrom(encrypted_client_id_proto)
        else:
            license_request.Msg.ClientId.CopyFrom(client_id)

        if session.device_config.private_key_available:
             key = RSA.importKey(open(session.device_config.device_private_key_filename).read())
             session.device_key = key
        else:
             self.logger.error("need device private key, other methods unimplemented")
             return 1

        self.logger.debug("signing license request")

        hash = SHA1.new(license_request.Msg.SerializeToString())
        signature = pss.new(key).sign(hash)

        license_request.Signature = signature

        session.license_request = license_request

        self.logger.debug("license request:")
        for line in text_format.MessageToString(session.license_request).splitlines():
            self.logger.debug(line)
        self.logger.info("license request created")
        self.logger.debug("license request b64: {}".format(base64.b64encode(license_request.SerializeToString())))
        return license_request.SerializeToString()

    def provide_license(self, session_id, license_b64):
        self.logger.debug("provide_license(session_id={}, license_b64={})".format(session_id, license_b64))
        self.logger.info("decrypting provided license")

        if session_id not in self.sessions:
            self.logger.error("session does not exist")
            return 1

        session = self.sessions[session_id]

        if not session.license_request:
            self.logger.error("generate a license request first!")
            return 1

        license = wv_proto2.SignedLicense()
        try:
            license.ParseFromString(base64.b64decode(license_b64))
        except DecodeError:
            self.logger.error("unable to parse license - check protobufs")
            return 1

        session.license = license

        self.logger.debug("license:")
        for line in text_format.MessageToString(license).splitlines():
            self.logger.debug(line)

        self.logger.debug("deriving keys from session key")

        oaep_cipher = PKCS1_OAEP.new(session.device_key)

        session.session_key = oaep_cipher.decrypt(license.SessionKey)

        lic_req_msg = session.license_request.Msg.SerializeToString()

        enc_key_base = b"ENCRYPTION\000" + lic_req_msg + b"\0\0\0\x80"
        auth_key_base = b"AUTHENTICATION\0" + lic_req_msg + b"\0\0\2\0"

        enc_key = b"\x01" + enc_key_base
        auth_key_1 = b"\x01" + auth_key_base
        auth_key_2 = b"\x02" + auth_key_base
        auth_key_3 = b"\x03" + auth_key_base
        auth_key_4 = b"\x04" + auth_key_base

        cmac_obj = CMAC.new(session.session_key, ciphermod=AES)
        cmac_obj.update(enc_key)

        enc_cmac_key = cmac_obj.digest()

        cmac_obj = CMAC.new(session.session_key, ciphermod=AES)
        cmac_obj.update(auth_key_1)
        auth_cmac_key_1 = cmac_obj.digest()

        cmac_obj = CMAC.new(session.session_key, ciphermod=AES)
        cmac_obj.update(auth_key_2)
        auth_cmac_key_2 = cmac_obj.digest()

        cmac_obj = CMAC.new(session.session_key, ciphermod=AES)
        cmac_obj.update(auth_key_3)
        auth_cmac_key_3 = cmac_obj.digest()

        cmac_obj = CMAC.new(session.session_key, ciphermod=AES)
        cmac_obj.update(auth_key_4)
        auth_cmac_key_4 = cmac_obj.digest()

        auth_cmac_combined_1 = auth_cmac_key_1 + auth_cmac_key_2
        auth_cmac_combined_2 = auth_cmac_key_3 + auth_cmac_key_4

        session.derived_keys['enc'] = enc_cmac_key
        session.derived_keys['auth_1'] = auth_cmac_combined_1
        session.derived_keys['auth_2'] = auth_cmac_combined_2

        self.logger.debug('verifying license signature')

        lic_hmac = HMAC.new(session.derived_keys['auth_1'], digestmod=SHA256)
        lic_hmac.update(license.Msg.SerializeToString())

        self.logger.debug("calculated sig: {} actual sig: {}".format(lic_hmac.hexdigest(), binascii.hexlify(license.Signature)))

        if lic_hmac.digest() != license.Signature:
            self.logger.info("license signature doesn't match - writing bin so they can be debugged")
            with open("original_lic.bin", "wb") as f:
                f.write(base64.b64decode(license_b64))
            with open("parsed_lic.bin", "wb") as f:
                f.write(license.SerializeToString())
            self.logger.info("continuing anyway")

        self.logger.debug("key count: {}".format(len(license.Msg.Key)))
        for key in license.Msg.Key:
            if key.Id:
                key_id = key.Id
            else:
                key_id = wv_proto2.License.KeyContainer.KeyType.Name(key.Type).encode('utf-8')
            encrypted_key = key.Key
            iv = key.Iv
            type = wv_proto2.License.KeyContainer.KeyType.Name(key.Type)

            cipher = AES.new(session.derived_keys['enc'], AES.MODE_CBC, iv=iv)
            decrypted_key = cipher.decrypt(encrypted_key)
            if type == "OPERATOR_SESSION":
                permissions = []
                perms = key._OperatorSessionKeyPermissions
                for (descriptor, value) in perms.ListFields():
                    if value == 1:
                        permissions.append(descriptor.name)
                print(permissions)
            else:
                permissions = []
            session.keys.append(Key(key_id, type, Padding.unpad(decrypted_key, 16), permissions))

        self.logger.info("decrypted all keys")
        return 0

    def get_keys(self, session_id):
        if session_id in self.sessions:
            return self.sessions[session_id].keys
        else:
            self.logger.error("session not found")
            return 1
