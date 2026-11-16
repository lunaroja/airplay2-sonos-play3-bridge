"""Exercise two separate relay processes against simulated Sonos HTTP speakers."""

import base64
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
manager = runpy.run_path(str(ROOT / "manage-player.py"))
FIRST = "RINCON_00000000000101400"
SECOND = "RINCON_00000000000201400"
PCM_A = b"\x03\x00\x04\x00" * 128
PCM_B = b"\x08\x00\x09\x00" * 128


def wait_for(probe, message):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.02)
    raise AssertionError(message)


class SimulatedSpeaker(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = self.rfile.read(int(self.headers["Content-Length"])).decode()
        action = self.headers["SOAPACTION"].split("#")[-1].strip('"')
        device = self.server
        payload = ""
        if action == "GetZoneGroupState":
            payload = (
                '<ZoneGroupState>&lt;ZoneGroupState&gt;'
                f'&lt;ZoneGroup Coordinator="{device.uuid}"&gt;'
                f'&lt;ZoneGroupMember UUID="{device.uuid}"/&gt;'
                '&lt;/ZoneGroup&gt;&lt;/ZoneGroupState&gt;</ZoneGroupState>'
            )
        elif action == "GetMediaInfo":
            payload = f"<CurrentURI>{device.uri}</CurrentURI>"
        elif action == "GetTransportInfo":
            payload = f"<CurrentTransportState>{device.state}</CurrentTransportState>"
        elif action == "SetAVTransportURI":
            device.uri = request.split("<CurrentURI>", 1)[1].split("</CurrentURI>", 1)[0]
        elif action == "SetVolume":
            volume = request.split("<DesiredVolume>", 1)[1].split("</DesiredVolume>", 1)[0]
            device.volume = int(volume)
        elif action in {"Play", "Stop"}:
            device.state = "PLAYING" if action == "Play" else "STOPPED"
        device.actions.append(action)
        body = f"<Response>{payload}</Response>".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def event(out, code, content=b""):
    data = b""
    if content:
        data = b'\n<data encoding="base64">' + base64.b64encode(content) + b"</data>"
    out.write(
        b"<item><type>73736e63</type><code>" + code.encode().hex().encode()
        + b"</code><length>" + str(len(content)).encode() + b"</length>" + data + b"</item>"
    )


def available_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@unittest.skipUnless(sys.platform.startswith("linux"), "WAV socket handling requires Linux")
class TwoPlayersTest(unittest.TestCase):
    def test_streams_controls_and_reconnect_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            paths = [Path(temporary) / slug for slug in ("office", "study")]
            devices, ports, audio, metadata = [], [], [], []
            for path, address, uuid in zip(paths, ("127.0.0.2", "127.0.0.3"), (FIRST, SECOND)):
                path.mkdir()
                device = stack.enter_context(ThreadingHTTPServer((address, 1400), SimulatedSpeaker))
                device.uuid, device.uri, device.state, device.volume = uuid, "", "STOPPED", None
                device.actions = []
                thread = threading.Thread(target=device.serve_forever, daemon=True)
                thread.start()
                stack.callback(device.shutdown)
                devices.append(device)

                port = available_port()
                ports.append(port)
                env = {
                    **os.environ, "BRIDGE_IP": "127.0.0.1", "SONOS_IP": address,
                    "SONOS_UUID": uuid, "MAX_VOLUME": "100", "DIDL_PUSH": "0",
                    "FLUSH_RESYNC": "0", "STREAM_FORMAT": "wav",
                    "STREAM_PORT": str(port), "PLAY3_RUNTIME_DIR": str(path),
                }
                diagnostics = stack.enter_context((path / "log").open("wb"))
                relay = stack.enter_context(subprocess.Popen(
                    [sys.executable, "-B", str(ROOT / "play3-relay.py")], env=env,
                    stdout=diagnostics, stderr=diagnostics,
                ))
                stack.callback(relay.terminate)
                wait_for(lambda: (path / "audio").exists() and (path / "metadata").exists(),
                         "relay did not create its FIFOs")
                # The read ends open on separate workers. Bound writer opens so
                # a failing relay cannot hang the suite.
                for filename, target in (("audio", audio), ("metadata", metadata)):
                    opened = []
                    def writer():
                        try:
                            opened.append(os.open(path / filename, os.O_WRONLY | os.O_NONBLOCK))
                        except OSError:
                            return False
                        return True

                    wait_for(writer, f"relay did not open {filename}")
                    target.append(stack.enter_context(os.fdopen(opened[0], "wb", buffering=0)))

            first, second = devices
            for out in metadata:
                event(out, "pbeg")
            wait_for(lambda: all(device.state == "PLAYING" for device in devices),
                     "the relays did not start both speakers")

            def open_stream(port, address):
                client = HTTPConnection("127.0.0.1", port, timeout=3, source_address=(address, 0))
                stack.callback(client.close)
                client.request("GET", "/stream.wav")
                response = client.getresponse()
                stack.callback(response.close)
                self.assertEqual(response.read(4), b"RIFF")
                self.assertEqual(len(response.read(40)), 40)
                return response

            self.assertEqual(first.uri, f"http://127.0.0.1:{ports[0]}/stream.wav")
            self.assertEqual(second.uri, f"http://127.0.0.1:{ports[1]}/stream.wav")
            with self.assertRaises(HTTPException):
                open_stream(ports[0], "127.0.0.3")
            stream_a = open_stream(ports[0], "127.0.0.2")
            stream_b = open_stream(ports[1], "127.0.0.3")
            audio[0].write(PCM_A)
            audio[1].write(PCM_B)
            self.assertEqual(stream_a.read(len(PCM_A)), PCM_A)
            self.assertEqual(stream_b.read(len(PCM_B)), PCM_B)

            # A track boundary keeps the AirPlay session active but starves the PCM pipe.
            # The existing speaker connection must get silence followed by new audio.
            self.assertEqual(stream_a.read(len(PCM_A)), bytes(len(PCM_A)))
            audio[0].write(PCM_B)
            def read_until_pcm(stream, pcm, failure):
                recent = bytearray()

                def arrived():
                    recent.extend(stream.read1(64 * 1024))
                    return pcm in recent

                wait_for(arrived, failure)

            read_until_pcm(stream_a, PCM_B, "new track did not reach the existing WAV connection")
            self.assertEqual(first.actions.count("Play"), 1)
            self.assertEqual(first.actions.count("SetAVTransportURI"), 1)

            event(metadata[0], "pvol", b"-18.0")
            wait_for(lambda: first.volume == 40, "volume did not reach the selected player")
            self.assertIsNone(second.volume)

            event(metadata[0], "pend")
            wait_for(lambda: first.state == "STOPPED", "first player did not stop")
            self.assertEqual(stream_a.read().strip(b"\0"), b"")
            self.assertEqual(second.state, "PLAYING")
            audio[1].write(PCM_B)
            read_until_pcm(stream_b, PCM_B, "second player lost audio when the first stopped")

            event(metadata[0], "pbeg")
            wait_for(lambda: first.state == "PLAYING", "first player did not start again")
            stream_a = open_stream(ports[0], "127.0.0.2")
            audio[0].write(PCM_A)
            self.assertEqual(stream_a.read(len(PCM_A)), PCM_A)
            self.assertEqual(first.volume, 40)
            self.assertNotIn("Stop", second.actions)
            self.assertEqual(second.actions.count("Play"), 1)

    def test_add_remove_and_failed_enable_keep_the_other_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "players").mkdir()
            source = root / "source.env"
            source.write_text(f"SONOS_UUID={FIRST}\nBRIDGE_IP=127.0.0.1\nSONOS_IP=127.0.0.2\n")
            fake_run = manager["add_player"].__globals__["subprocess"]
            result = subprocess.CompletedProcess([], 0)
            with patch.object(fake_run, "run", return_value=result) as systemctl:
                manager["add_player"](root, "office", source, "Office Sonos", 0)
                with self.assertRaisesRegex(ValueError, "already configured"):
                    manager["add_player"](root, "duplicate", source, "Duplicate", 0)
                source.write_text(f"SONOS_UUID={SECOND}\nBRIDGE_IP=127.0.0.1\nSONOS_IP=127.0.0.3\n")
                systemctl.side_effect = [subprocess.CalledProcessError(1, "systemctl"), result, result]
                with self.assertRaises(subprocess.CalledProcessError):
                    manager["add_player"](root, "study", source, "Study Sonos", 0)
                self.assertFalse((root / "players/study").exists())

                systemctl.side_effect = None
                manager["add_player"](root, "study", source, "Study Sonos", 0)
                configs = [path.read_text() for path in sorted((root / "players").glob("*/shairport-sync.conf"))]
                self.assertEqual(len(configs), 2)
                for key in ("port", "airplay_device_id", "nqptp_shared_memory_interface_name"):
                    entries = [next(line for line in text.splitlines() if line.strip().startswith(key + " =")) for text in configs]
                    self.assertEqual(len(set(entries)), 2)
                manager["remove_player"](root, "study")
                self.assertTrue((root / "players/office").is_dir())
                self.assertFalse((root / "players/study").exists())


if __name__ == "__main__":
    unittest.main()
