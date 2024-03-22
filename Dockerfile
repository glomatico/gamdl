FROM python:3.12-bullseye as PYTHON

ARG USERNAME=gamdl
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -r -g $USER_GID $USER_UID && useradd -r -g $USER_GID $USER_UID

ENV PROJ_NAME=gamdl
ENV MP4DECRYPT="Bento4-SDK-1-6-0-641.x86_64-unknown-linux"
ENV M3U8DL="N_m3u8DL-RE_Beta_linux-x64_20230628"
ENV M3U8DL_URL="https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.0-beta/$M3U8DL.tar.gz"
ENV MP4BOX="gpac_2.2.1-rev0-gb34e3851-release-2.2_amd64"
ENV MP4BOX_URL="https://download.tsi.telecom-paristech.fr/gpac/release/2.2.1/$MP4BOX.deb"

# Install ffmpeg
RUN apt-get update && apt-get install -y \
	ffmpeg \
	libmad0 \
	libglu1 \
	libglu1-mesa \
	libfaad2 \
	liba52-0.7.4 \
	libavcodec58

# Install MP4Decrypt
RUN wget "https://www.bok.net/Bento4/binaries/$MP4DECRYPT.zip" -O /bento.zip
RUN unzip -d / /bento.zip
RUN mv /$MP4DECRYPT/bin/* /usr/bin/
RUN rm -rf /$MP4DECRYPT
RUN rm /bento.zip

# Install pywidevine
RUN pip3 install pywidevine pyyaml

# Install MP4Box
RUN wget "$MP4BOX_URL"
RUN dpkg --install "$MP4BOX".deb
RUN rm "$MP4BOX".deb

# Install N_m3u8DL-RE
RUN wget $M3U8DL_URL -O $M3U8DL.tar.gz
RUN tar -xf $M3U8DL.tar.gz -C /
RUN rm $M3U8DL.tar.gz
RUN mv /N_m3u8DL-RE*/N_m3u8DL-RE /bin/N_m3u8DL-RE
RUN chmod +x /bin/N_m3u8DL-RE

# Install gamdl
ADD ./ /gamdl
RUN pip3 install /gamdl/

#USER $USER_UID

# No Entrypoint, should be run with specific commands.
CMD ["gamdl"]

