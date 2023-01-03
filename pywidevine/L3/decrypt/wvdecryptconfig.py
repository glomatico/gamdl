class WvDecryptConfig(object):
    def __init__(self, decrypted_location, encrypted_location, init_data_b64):
        self.decrypted_location = str(decrypted_location)
        self.encrypted_location = str(encrypted_location)
        self.init_data_b64 = init_data_b64

    def build_commandline_list(self, keys):
        commandline = ['mp4decrypt']
        commandline.append('--show-progress')
        for key in keys:
            if key.type == 'CONTENT':
                commandline.append('--key')
                default_KID = 1
                commandline.append('{}:{}'.format(str(default_KID), key.key.hex()))
        commandline.append(self.encrypted_location)
        commandline.append(self.decrypted_location)
        return commandline
