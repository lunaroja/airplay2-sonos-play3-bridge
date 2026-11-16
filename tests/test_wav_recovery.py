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

    def test_existing_wav_client_gets_bounded_silence_during_a_skip(self):
        handler_type = app["Play3StreamHandler"]
        handler = MagicMock()
        namespace = handler_type.serve_wav.__globals__
        pcm_a = b"\x11\x00\x12\x00"
        pcm_b = b"\x21\x00\x22\x00"

        def chunks():
            yield (0, b"")  # Before playback, leave the header-only connection alone.
            yield (4, pcm_a)
            yield (4, b"")
            yield (4, b"")
            yield (8, pcm_b)
            yield (8, b"")
            yield (8, b"")  # Do not add more silence after the five-second limit.
            yield (12, pcm_a)
            relay.STATE["active"] = False
            yield (12, b"")

        with (
            patch.object(namespace["select"], "poll") as make_poll,
            patch.object(namespace["time"], "monotonic", side_effect=(
                1, 2, 2.25, 2.5, 2.6, 8, 9, 10, 10.25,
            )),
            patch.object(relay.RING, "join_pos", return_value=0),
            patch.object(relay.RING, "read_from", side_effect=chunks()) as read,
        ):
            make_poll.return_value.poll.side_effect = [False] * 9 + [True]
            handler_type.serve_wav(handler)

        header, first, gap_a, gap_b, second, long_gap, third = [
            call.args[0] for call in handler.wfile.write.call_args_list
        ]
        self.assertEqual(header, relay.wav_header())
        self.assertEqual((first, second, third), (pcm_a, pcm_b, pcm_a))
        self.assertEqual((gap_a, gap_b, long_gap), (bytes(44100), bytes(44100), bytes(882000)))
        self.assertEqual([call.args[0] for call in read.call_args_list], [0, 0, 4, 4, 4, 8, 8, 8, 12])
        self.assertEqual(relay.CLIENTS, set())

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
