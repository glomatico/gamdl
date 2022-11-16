import base64
import logging
import subprocess
from pywidevine.L3.cdm import cdm, deviceconfig

class WvDecrypt(object):

    WV_SYSTEM_ID = [237, 239, 139, 169, 121, 214, 74, 206, 163, 200, 39, 220, 213, 29, 33, 237]

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.wvdecrypt_process = None

        self.logger.debug(self.log_message("wvdecrypt object created"))
        self.cdm = cdm.Cdm()

        def check_pssh(pssh_b64):
            pssh = base64.b64decode(pssh_b64)
            if not pssh[12:28] == bytes(self.WV_SYSTEM_ID):
                new_pssh = bytearray([0,0,0])
                new_pssh.append(32+len(pssh))
                new_pssh[4:] = bytearray(b'pssh')
                new_pssh[8:] = [0,0,0,0]
                new_pssh[13:] = self.WV_SYSTEM_ID
                new_pssh[29:] = [0,0,0,0]
                new_pssh[31] = len(pssh)
                new_pssh[32:] = pssh
                return base64.b64encode(new_pssh)
            else:
                return pssh_b64

        self.session = self.cdm.open_session(check_pssh(config.init_data_b64),
                                deviceconfig.DeviceConfig(deviceconfig.device_android_generic))

        self.logger.debug(self.log_message("widevine session opened"))

            
    def log_message(self, msg):
        return "{}_{} : {}".format('audio', '0', msg)

    def start_process(self):
        decryption_keys = self.cdm.get_keys(self.session)
        self.logger.debug(self.log_message("starting process"))
        self.logger.debug(self.config.build_commandline_list(decryption_keys))
        self.wvdecrypt_process = subprocess.run(
            self.config.build_commandline_list(decryption_keys),
            check=True
        )
        self.logger.debug(self.log_message("decrypted successfully"))

    def get_challenge(self):
        return self.cdm.get_license_request(self.session)

    def update_license(self, license_b64):
        self.cdm.provide_license(self.session, license_b64)
        return True
