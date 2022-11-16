class WvDecryptConfig(object):
    def __init__(self, decrypted_download_location, encrypted_download_location, init_data_b64):
        self.decrypted_download_location = str(decrypted_download_location)
        self.encrypted_download_location = str(encrypted_download_location)
        self.init_data_b64 = init_data_b64

    def build_commandline_list(self, keys):
        commandline = ['mp4decrypt']
        commandline.append('--show-progress')
        for key in keys:
            if key.type == 'CONTENT':
                commandline.append('--key')
                default_KID = 1
                commandline.append('{}:{}'.format(str(default_KID), key.key.hex()))
        commandline.append(self.encrypted_download_location)
        commandline.append(self.decrypted_download_location)
        return commandline
