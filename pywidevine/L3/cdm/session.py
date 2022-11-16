class Session:
    def __init__(self, session_id, init_data, device_config, offline):
        self.session_id = session_id
        self.init_data = init_data
        self.offline = offline
        self.device_config = device_config
        self.device_key = None
        self.session_key = None
        self.derived_keys = {
            'enc': None,
            'auth_1': None,
            'auth_2': None
        }
        self.license_request = None
        self.license = None
        self.service_certificate = None
        self.privacy_mode = False
        self.keys = []
