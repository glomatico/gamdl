import os

device_android_generic = {
    'name': 'android_generic',
    'description': 'android studio cdm',
    'security_level': 3,
    'session_id_type': 'android',
    'private_key_available': True,
    'vmp': False,
    'send_key_control_nonce': True
}

devices_available = [device_android_generic]

FILES_FOLDER = 'devices'

class DeviceConfig:
    def __init__(self, device):
        self.device_name = device['name']
        self.description = device['description']
        self.security_level = device['security_level']
        self.session_id_type = device['session_id_type']
        self.private_key_available = device['private_key_available']
        self.vmp = device['vmp']
        self.send_key_control_nonce = device['send_key_control_nonce']

        if 'keybox_filename' in device:
            self.keybox_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], device['keybox_filename'])
        else:
            self.keybox_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], 'keybox')

        if 'device_cert_filename' in device:
            self.device_cert_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], device['device_cert_filename'])
        else:
            self.device_cert_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], 'device_cert')

        if 'device_private_key_filename' in device:
            self.device_private_key_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], device['device_private_key_filename'])
        else:
            self.device_private_key_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], 'device_private_key')

        if 'device_client_id_blob_filename' in device:
            self.device_client_id_blob_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], device['device_client_id_blob_filename'])
        else:
            self.device_client_id_blob_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], 'device_client_id_blob')

        if 'device_vmp_blob_filename' in device:
            self.device_vmp_blob_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], device['device_vmp_blob_filename'])
        else:
            self.device_vmp_blob_filename = os.path.join(os.path.dirname(__file__), FILES_FOLDER, device['name'], 'device_vmp_blob')

    def __repr__(self):
        return "DeviceConfig(name={}, description={}, security_level={}, session_id_type={}, private_key_available={}, vmp={})".format(self.device_name, self.description, self.security_level, self.session_id_type, self.private_key_available, self.vmp)
