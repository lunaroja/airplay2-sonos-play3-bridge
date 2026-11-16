import runpy
import unittest
from pathlib import Path
from xml.etree import ElementTree

discovery = runpy.run_path(str(Path(__file__).resolve().parents[1] / "discover-sonos.py"))
Speaker = discovery["Speaker"]
topology_status = discovery["topology_status"]
UUID = "RINCON_00000000000001400"


class SonosEligibilityTest(unittest.TestCase):
    def status(self, model, flag, member_body=""):
        speaker = Speaker("192.0.2.20", UUID, "Test room", model)
        topology = ElementTree.fromstring(
            f'<ZoneGroupState><ZoneGroup Coordinator="{UUID}">'
            f'<ZoneGroupMember UUID="{UUID}" AirPlayEnabled="{flag}">'
            f'{member_body}</ZoneGroupMember></ZoneGroup></ZoneGroupState>'
        )
        return topology_status(topology, speaker)

    def test_native_airplay_takes_precedence_over_the_device_name(self):
        self.assertEqual(self.status("Sonos Move 2", "1"), "native AirPlay 2")
        self.assertEqual(self.status("Sonos Play:3", "1"), "native AirPlay 2")

    def test_zero_flag_needs_a_known_legacy_model(self):
        self.assertEqual(self.status("Sonos Play:3", "0"), "eligible")
        self.assertEqual(self.status("Sonos Move 2", "0"), "AirPlay support unverified")
        self.assertEqual(self.status("Sonos Play:3", ""), "AirPlay support unverified")

    def test_bonded_speaker_is_not_eligible(self):
        self.assertEqual(self.status("Sonos Playbar", "0", "<Satellite/>"),
                         "grouped or bonded; ungroup before setup")
