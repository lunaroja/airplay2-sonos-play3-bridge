#!/usr/bin/env python3
"""Find Sonos speakers and print settings for one explicitly selected speaker."""

import argparse
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree

ZONE_PLAYER = "urn:schemas-upnp-org:device:ZonePlayer:1"
SSDP = ("239.255.255.250", 1900)
QUERY = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP[0]}:{SSDP[1]}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    f"ST: {ZONE_PLAYER}\r\n\r\n"
).encode("ascii")
DESCRIPTION_LIMIT = 65536


class Speaker(NamedTuple):
    ip: str
    uuid: str
    name: str
    model: str


def discover_addresses():
    addresses = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as search:
        search.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        search.sendto(QUERY, SSDP)
        deadline = time.monotonic() + 2.0
        while len(addresses) < 64:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            search.settimeout(remaining)
            try:
                reply, (address, _) = search.recvfrom(4096)
            except socket.timeout:
                break
            lines = reply.decode("latin-1").splitlines()
            if lines and lines[0].upper().startswith("HTTP/1.1 200"):
                for line in lines[1:]:
                    key, _, value = line.partition(":")
                    if key.strip().lower() == "st" and value.strip() == ZONE_PLAYER:
                        addresses.add(address)
                        break
    return addresses


def read_description(address):
    # Use the responding address and a fixed path. Ignore advertised URLs.
    connection = HTTPConnection(address, 1400, timeout=2)
    try:
        connection.request("GET", "/xml/device_description.xml")
        reply = connection.getresponse()
        if reply.status != 200:
            return None
        description = reply.read(DESCRIPTION_LIMIT + 1)
        if len(description) > DESCRIPTION_LIMIT or b"<!DOCTYPE" in description.upper():
            return None
        device = ElementTree.fromstring(description).find("{*}device")
        if device is None or device.findtext("{*}deviceType") != ZONE_PLAYER:
            return None

        uuid = device.findtext("{*}UDN", "")
        if not re.fullmatch(r"uuid:RINCON_[A-Fa-f0-9]+", uuid):
            return None

        def label(tag, fallback):
            value = device.findtext("{*}" + tag, "")
            return "".join(char for char in value if char.isprintable()).strip()[:80] or fallback

        return Speaker(address, uuid.removeprefix("uuid:"), label("roomName", address),
                       label("modelName", "Sonos"))
    except (OSError, HTTPException, ElementTree.ParseError):
        return None
    finally:
        connection.close()


def bridge_address(speaker):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((speaker.ip, 1400))
        return route.getsockname()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select", metavar="UUID", help="print relay.env for the selected Sonos")
    parser.add_argument("--output", type=Path, help="write a new config file instead of printing it")
    args = parser.parse_args()
    if args.output and not args.select:
        parser.error("--output requires --select")
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            speakers = sorted(filter(None, workers.map(read_description, discover_addresses())),
                              key=lambda item: (item.name.casefold(), item.ip))
    except OSError as error:
        parser.exit(1, f"Sonos discovery failed: {error}\n")
    if not speakers:
        parser.exit(1, "No Sonos speakers found. Check that the speaker is awake and on the same LAN.\n")
    if args.select:
        matches = [speaker for speaker in speakers if speaker.uuid.casefold() == args.select.casefold()]
        if len(matches) != 1:
            parser.error("Select one unique speaker UUID from the discovery list.")
        speaker = matches[0]
        try:
            address = bridge_address(speaker)
        except OSError as error:
            parser.exit(1, f"Could not find a local route to the speaker: {error}\n")
        config = (
            f"BRIDGE_IP={address}\nSONOS_IP={speaker.ip}\nSONOS_UUID={speaker.uuid}\n"
            "STREAM_FORMAT=wav\nMAX_VOLUME=70\n"
        )
        if args.output:
            try:
                with args.output.open("x") as output:
                    output.write(config)
            except OSError as error:
                parser.exit(1, f"Could not create {args.output}: {error}\n")
        else:
            print(config, end="")
        return
    for speaker in speakers:
        print(f"{speaker.name} ({speaker.model})  {speaker.ip}  {speaker.uuid}")


if __name__ == "__main__":
    main()
