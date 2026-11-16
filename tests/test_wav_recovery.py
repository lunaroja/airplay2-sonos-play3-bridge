"""Run with the pinned server.py from the README on PYTHONPATH."""

import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

with patch.dict(os.environ, {
    "BRIDGE_IP": "192.0.2.10",
    "SONOS_IP": "192.0.2.20",
    "SONOS_UUID": "RINCON_00000000000001400",
    "STREAM_FORMAT": "wav",
}):
    app = runpy.run_path(str(Path(__file__).resolve().parents[1] / "play3-relay.py"))

relay = app["relay"]
renderer_class = app["Play3Renderer"]
reader = app["read_wav_audio"]


class WavRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(relay.CLIENTS.clear)
        state = patch.dict(relay.STATE, {"active": True, "pushed": ("track",)})
        state.start()
        self.addCleanup(state.stop)

    def test_short_gap_then_audio_schedules_a_retry(self):
        pipe = MagicMock()
        # Time between the connected chunk and fresh audio is under two seconds.
        def chunks():
            yield b"\x10\x00"
            relay.CLIENTS.clear()
            yield b"\x00" * 8
            yield b"\x20\x00"
            yield b"\x20\x00"

        pipe.read.side_effect = chunks()
        renderer = MagicMock()
        worker = MagicMock()
        relay.CLIENTS.add(object())
        with (
            patch("builtins.open", return_value=MagicMock(__enter__=MagicMock(return_value=pipe))),
            patch.object(relay.RING, "write") as write,
            patch.dict(reader.__globals__, {"Thread": worker}) as namespace,
            patch.object(namespace["time"], "monotonic", side_effect=(1.0, 1.8, 1.9, 2.0)),
            self.assertRaises(StopIteration),
        ):
            reader(renderer)
        self.assertEqual(write.call_count, 4)
        worker.assert_called_once_with(target=renderer.resume_wav, daemon=True)
        worker.return_value.start.assert_called_once_with()

    def test_play_omits_the_artwork_url_but_keeps_track_metadata(self):
        renderer = renderer_class(relay.RENDERER_IP)
        control = renderer.action.__func__.__globals__
        with (
            patch.dict(control, {"is_standalone": lambda: True}),
            patch.object(renderer, "owns_stream", return_value=True),
            patch.dict(relay.ART, {"bytes": b"art", "id": 5}),
            patch.dict(relay.NOW_PLAYING, {"title": "Test track"}),
            patch.object(relay.Renderer, "action") as command,
        ):
            renderer.play(relay.STREAM_URL)
        metadata = command.call_args_list[0].args[1]
        self.assertIn("Test track", metadata)
        self.assertIn("http-get:*:audio/wav:*", metadata)
        self.assertNotIn("albumArtURI", metadata)

        handler = MagicMock()
        app["Play3StreamHandler"].serve_art(handler)
        handler.send_error.assert_called_once_with(404)

    def test_only_resumes_a_stopped_standalone_speaker_on_the_owned_stream(self):
        renderer = renderer_class(relay.RENDERER_IP)
        control = renderer.resume_wav.__func__.__globals__
        for standalone, uri, transport, expected in [
            (True, relay.STREAM_URL, "STOPPED", True),
            (True, relay.STREAM_URL, "PAUSED_PLAYBACK", False),
            (False, relay.STREAM_URL, "STOPPED", False),
            (True, "http://192.0.2.20/other", "STOPPED", False),
        ]:
            with self.subTest(standalone=standalone, uri=uri, transport=transport):
                def read(_service, action, _arguments):
                    if action == "GetMediaInfo":
                        return ElementTree.fromstring(f"<Info><CurrentURI>{uri}</CurrentURI></Info>")
                    return ElementTree.fromstring(
                        f"<Info><CurrentTransportState>{transport}</CurrentTransportState></Info>"
                    )

                with (
                    patch.dict(control, {"is_standalone": lambda: standalone, "read_sonos": read}),
                    patch.object(relay.Renderer, "action") as command,
                ):
                    renderer.resume_wav()
                if expected:
                    command.assert_called_once_with("Play", "<Speed>1</Speed>")
                else:
                    command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
