"""Constrain the pinned AirPlay relay to the standalone Play:3."""

import os
import re
import select
import subprocess
import time
from http.server import ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from threading import Lock, Thread
from types import SimpleNamespace
from xml.etree import ElementTree

import server as relay

host = str(IPv4Address(os.environ["BRIDGE_IP"]))
target = str(IPv4Address(os.environ["SONOS_IP"]))
stream_format = os.environ.get("STREAM_FORMAT", "wav")
if stream_format not in {"wav", "flac"}:
    raise ValueError("STREAM_FORMAT must be wav or flac")
if not 1 <= relay.MAX_VOLUME <= 100:
    raise ValueError("MAX_VOLUME must be between 1 and 100")
relay.RENDERER_IP = target
relay.STREAM_URL = f"http://{host}:{relay.PORT}/stream.{stream_format}"
target_uuid = os.environ["SONOS_UUID"]
services = {
    "AVTransport": "/MediaRenderer/AVTransport/Control",
    "RenderingControl": "/MediaRenderer/RenderingControl/Control",
    "ZoneGroupTopology": "/ZoneGroupTopology/Control",
}
runtime = Path(os.environ.get("PLAY3_RUNTIME_DIR", "/run/play3-airplay"))
relay.AUDIO_PIPE = str(runtime / "audio")
relay.META_PIPE = str(runtime / "metadata")


class Play3HTTPServer(ThreadingHTTPServer):
    def __init__(self, address, handler):
        super().__init__((host, address[1]), handler)

    def verify_request(self, request, client_address):
        return client_address[0] == target


# Match the receiver PCM format: 44.1 kHz, stereo, signed 16-bit.
WAV_SAMPLE_RATE = 44100
WAV_READ_TIMEOUT = 0.25
WAV_MAX_GAP = 5.0


class Play3StreamHandler(relay.StreamHandler):
    def serve_art(self):
        self.send_error(404)

    def serve_wav(self):
        self.connection.settimeout(30)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.end_headers()

        # Sonos can close its read connection while the PCM pipe is idle.
        # Detect that promptly so the relay can reconnect when audio resumes.
        connection = select.poll()
        connection.register(self.connection, select.POLLRDHUP | select.POLLHUP | select.POLLERR)
        position = relay.RING.join_pos()
        relay.CLIENTS.add(self.connection)
        log(f"wav client {self.address_string()} connected")
        last_audio_at = None
        padding_sent = 0
        try:
            self.wfile.write(relay.wav_header())
            while not connection.poll(0):
                position, audio = relay.RING.read_from(position, timeout=WAV_READ_TIMEOUT)
                now = time.monotonic()
                if audio:
                    if padding_sent:
                        log(f"WAV audio resumed after {now - last_audio_at:.2f}s gap")
                    last_audio_at = now
                    padding_sent = 0
                    self.wfile.write(audio)
                elif last_audio_at is not None and relay.STATE["active"]:
                    # Keep an established stream fed during a short track gap.
                    # Deliver silence only to this client, without moving the ring
                    # cursor or replaying the gap to a newly connected client.
                    elapsed = min(now - last_audio_at, WAV_MAX_GAP)
                    due = int(elapsed * WAV_SAMPLE_RATE) * 4
                    if due > padding_sent:
                        self.wfile.write(bytes(due - padding_sent))
                        padding_sent = due
        except OSError:
            pass
        finally:
            relay.CLIENTS.discard(self.connection)
            log(f"wav client {self.address_string()} disconnected")


def control_url(service):
    return f"http://{target}:1400{services[service]}"


def read_sonos(service, action, arguments=""):
    urn = f"urn:schemas-upnp-org:service:{service}:1"
    body = (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action} xmlns:u="{urn}">{arguments}</u:{action}>'
        '</s:Body></s:Envelope>'
    )
    data = relay.http_req(
        control_url(service), body.encode(),
        {"SOAPACTION": f'"{urn}#{action}"', "Content-Type": "text/xml"},
        timeout=2,
    )
    return ElementTree.fromstring(data)


def is_standalone():
    state = read_sonos("ZoneGroupTopology", "GetZoneGroupState")
    group_xml = state.findtext(".//{*}ZoneGroupState")
    groups = ElementTree.fromstring(group_xml)
    return any(
        group.get("Coordinator") == target_uuid
        and [member.get("UUID") for member in group.findall("ZoneGroupMember")] == [target_uuid]
        for group in groups.findall(".//ZoneGroup")
    )


class Play3Renderer(relay.Renderer):
    def __init__(self, ip):
        super().__init__(ip)
        self.control_lock = Lock()
        self.airplay_volume = None

    def resolve(self, service):
        return control_url(service)

    def owns_stream(self):
        info = read_sonos("AVTransport", "GetMediaInfo", "<InstanceID>0</InstanceID>")
        return info.findtext(".//{*}CurrentURI") == relay.STREAM_URL

    def resume_wav(self):
        with self.control_lock:
            if not relay.STATE["active"] or relay.CLIENTS:
                return
            try:
                if not is_standalone() or not self.owns_stream():
                    return
                transport = read_sonos("AVTransport", "GetTransportInfo", "<InstanceID>0</InstanceID>")
                if transport.findtext(".//{*}CurrentTransportState") != "STOPPED":
                    return
            except (OSError, TypeError, ValueError, ElementTree.ParseError):
                return
            if relay.STATE["active"] and not relay.CLIENTS:
                log("WAV audio resumed, reconnecting stopped player")
                super().action("Play", "<Speed>1</Speed>")

    def action(self, name, args, service="AVTransport"):
        with self.control_lock:
            # AirPlay can send its volume before playback begins.
            if name == "SetVolume":
                self.airplay_volume = args
            if name != "Stop" and not relay.STATE["active"]:
                return
            try:
                if not is_standalone():
                    print("Skipped Sonos control because the Play:3 is grouped", flush=True)
                    return
                if name in {"Stop", "SetVolume", "Play"} and not self.owns_stream():
                    return
            except (OSError, TypeError, ValueError, ElementTree.ParseError):
                print("Skipped Sonos control because the target could not be verified", flush=True)
                return

            if name == "Stop":
                # An idle stream can keep Sonos's Stop request waiting for audio.
                relay.drop_clients("session ended")
            if name == "SetAVTransportURI":
                # AirPlay command metadata can still contain embedded artwork
                # even when Shairport's separate cover-art delivery is disabled.
                args = re.sub(r"&lt;upnp:albumArtURI&gt;.*?&lt;/upnp:albumArtURI&gt;",
                              "", args)
                if stream_format == "wav":
                    # The pinned renderer hardcodes the DIDL resource MIME type.
                    args = args.replace('protocolInfo="http-get:*:audio/flac:*"',
                                        'protocolInfo="http-get:*:audio/wav:*"')
            if name == "Play" and self.airplay_volume is not None:
                super().action("SetVolume", self.airplay_volume, "RenderingControl")
            super().action(name, args, service)


def log(message):
    if message.startswith(("session begin", "session end", "renderer Play failed",
                           "renderer Stop failed", "renderer SetAVTransportURI failed",
                           "renderer SetVolume failed",
                           "flac client", "wav client", "serving FLAC", "audio pipe",
                           "audio resumed, kicking player", "session ended: dropped",
                           "WAV audio resumed")):
        print(message, flush=True)


def read_wav_audio(renderer):
    retry_at = 0.0
    retry = None
    while True:
        try:
            with open(relay.AUDIO_PIPE, "rb", buffering=0) as pipe:
                log("audio pipe open")
                while data := pipe.read(8192):
                    relay.RING.write(data)
                    now = time.monotonic()
                    if relay.CLIENTS or not relay.STATE["active"] or relay.STATE["pushed"] is None:
                        retry_at = now + 0.75
                    elif now >= retry_at and (retry is None or not retry.is_alive()):
                        # The speaker may disconnect on a skip shorter than the
                        # upstream reader's two-second audio-gap threshold.
                        if data.strip(b"\0"):
                            retry_at = now + 1.5
                            retry = Thread(target=renderer.resume_wav, daemon=True)
                            retry.start()
            log("audio pipe closed (session ended)")
        except OSError as error:
            log(f"audio pipe error: {error}")
            time.sleep(1)


def start_encoder(command, **options):
    # The pinned relay supplies the pipe's PCM codec, sample rate and channels.
    # Skip the long stream probe before emitting the first FLAC frame.
    input_index = command.index("-i")
    command = [
        *command[:input_index], "-probesize", "32", "-analyzeduration", "1",
        *command[input_index:],
    ]
    return subprocess.Popen(command, **options)


def main():
    relay.ThreadingHTTPServer = Play3HTTPServer
    relay.StreamHandler = Play3StreamHandler
    relay.Renderer = Play3Renderer
    # This dependency uses subprocess only to start its FFmpeg encoder.
    relay.subprocess = SimpleNamespace(Popen=start_encoder, PIPE=subprocess.PIPE)
    relay.log = log
    # Track refreshes are disabled. The first Play need not wait for tags.
    relay.PUSH_SETTLE = 0
    if stream_format == "wav":
        relay.audio_reader = read_wav_audio
    relay.main()


if __name__ == "__main__":
    main()
