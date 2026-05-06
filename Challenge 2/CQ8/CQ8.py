#!/usr/bin/env python3
"""
CQ8 — Compare MQTT PUBLISH topic depth for traffic to the *local* broker in
A.pcapng vs B.pcapng.

CQ8a: total PUBLISH messages to the local broker in A.pcapng.
CQ8b: total PUBLISH messages to the local broker in B.pcapng.

Also writes a grouped bar chart (histogram-style) comparing both captures.

Local broker: destination 127.0.0.1 or ::1, TCP port 1883.

Requires: pip install scapy matplotlib
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
from scapy.all import IP, IPv6, Raw, TCP, rdpcap

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PCAP_A = SCRIPT_DIR / "A.pcapng"
PCAP_B = SCRIPT_DIR / "B.pcapng"
OUTPUT_FIGURE = SCRIPT_DIR / "CQ8_topic_depth.png"
MQTT_PORT = 1883
# Loopback addresses treated as "the local broker" (matches Wireshark filters
# like ip.dst == 127.0.0.1 || ipv6.dst == ::1)
LOCAL_BROKERS = frozenset({"127.0.0.1", "::1"})
# Plot legend labels (filenames shown in the chart)
CHART_LABEL_A = PCAP_A.name
CHART_LABEL_B = PCAP_B.name


def _reassemble_flow(chunks: list[tuple[int, bytes]]) -> bytes:
    """
    Rebuild one direction of a TCP stream (client → broker).

    Packets arrive as segments; we sort by TCP sequence number and append
    payload bytes, trimming overlaps the same way a naive TCP reassembler would.
    """
    chunks = sorted(chunks, key=lambda x: x[0])
    out = bytearray()
    next_seq: int | None = None

    for seq, data in chunks:
        if not data:
            continue

        if next_seq is None:
            # First segment in this direction: start the buffer at this SEQ.
            out.extend(data)
            next_seq = seq + len(data)
        elif seq >= next_seq:
            # Contiguous or after a gap we do not fill: just append.
            out.extend(data)
            next_seq = seq + len(data)
        elif seq + len(data) > next_seq:
            # Overlap with data we already have: keep only the tail that is new.
            overlap_skip = next_seq - seq
            out.extend(data[overlap_skip:])
            next_seq = seq + len(data)

    return bytes(out)


def _read_mqtt_frames(buf: bytes) -> list[tuple[int, int, bytes]]:
    """
    Split raw TCP payload bytes into MQTT frames.

    Each frame: one byte fixed header (type in high nibble, flags in low nibble),
    then a Remaining Length encoded as MQTT variable-byte integer, then that
    many bytes of "remaining length" (variable header + payload).
    """
    frames: list[tuple[int, int, bytes]] = []
    i = 0

    while i < len(buf):
        if i + 1 > len(buf):
            break

        b0 = buf[i]
        msg_type = b0 >> 4  # e.g. 1=CONNECT, 3=PUBLISH
        flags = b0 & 0x0F
        i += 1

        # Remaining Length: 1–4 bytes, 7 bits per byte, continuation bit MSB
        mult = 1
        rl = 0
        while True:
            if i >= len(buf):
                return frames
            byte = buf[i]
            i += 1
            rl += (byte & 0x7F) * mult
            mult *= 128
            if mult > 128**4:
                return frames
            if (byte & 0x80) == 0:
                break

        if i + rl > len(buf):
            # Incomplete frame at end of capture — stop.
            break

        body = buf[i : i + rl]
        i += rl
        frames.append((msg_type, flags, body))

    return frames


def _read_str(body: bytes, off: int) -> tuple[bytes | None, int]:
    """MQTT UTF-8 string: 2-byte big-endian length, then payload bytes."""
    if off + 2 > len(body):
        return None, off
    slen = int.from_bytes(body[off : off + 2], "big")
    off += 2
    if off + slen > len(body):
        return None, off
    s = body[off : off + slen]
    off += slen
    return s, off


def _read_vbi(body: bytes, off: int) -> tuple[int | None, int]:
    """MQTT Variable Byte Integer (used in MQTT 5 property lengths, etc.)."""
    mult = 1
    value = 0
    while off < len(body):
        byte = body[off]
        off += 1
        value += (byte & 0x7F) * mult
        mult *= 128
        if mult > 128**4:
            return None, off
        if (byte & 0x80) == 0:
            return value, off
    return None, off


def _connect_protocol_level(body: bytes) -> int | None:
    """
    Read protocol level byte from CONNECT payload start (after protocol name).

    3 = MQTT 3.1, 4 = MQTT 3.1.1, 5 = MQTT 5.0 — affects how PUBLISH is parsed.
    """
    off = 0
    proto, off = _read_str(body, off)
    if proto is None:
        return None
    if off + 1 > len(body):
        return None
    return int(body[off])


def _parse_publish_topic(flags: int, body: bytes, protocol_level: int) -> str | None:
    """
    Read the Topic Name from a PUBLISH and skip the rest of the variable header
    so we do not mis-align later frames. We ignore the payload.

    MQTT 3.x: Topic (string) + Packet Identifier (only if QoS > 0) | payload
    MQTT 5:  Topic + Packet Identifier (if QoS > 0) + Properties + payload
    """
    qos = (flags >> 1) & 0x03
    off = 0

    topic_raw, off = _read_str(body, off)
    if topic_raw is None:
        return None
    topic = topic_raw.decode("utf-8", errors="replace")

    if protocol_level == 5:
        # Packet Identifier present for QoS 1 or 2
        if qos > 0:
            if off + 2 > len(body):
                return None
            off += 2
        # Property Length + properties (skip; we only need the topic for depth)
        prop_len, off = _read_vbi(body, off)
        if prop_len is None or off + prop_len > len(body):
            return None
        off += prop_len
    else:
        # MQTT 3.1 / 3.1.1: optional 2-byte message id, then payload
        if qos > 0:
            if off + 2 > len(body):
                return None
            off += 2

    return topic


def _flow_key(packet) -> tuple[str, int, str, int] | None:
    """(client_ip, client_tcp_port, broker_ip, broker_tcp_port) or None."""
    if packet.haslayer(IP):
        ip = packet[IP]
    elif packet.haslayer(IPv6):
        ip = packet[IPv6]
    else:
        return None
    tcp = packet[TCP]
    return (ip.src, int(tcp.sport), ip.dst, int(tcp.dport))


def topic_layer_count(topic: str) -> int:
    """
    Topic "depth": count non-empty segments separated by '/'.

    Example: "home/room/sensor" → 3. Empty segments from stray slashes are ignored.
    """
    return len([part for part in topic.split("/") if part != ""])


def publish_depths_for_pcap(pcap_path: Path, mqtt_port: int) -> list[int]:
    """
    Return one integer depth per PUBLISH (type 3) sent to the local broker.

    Steps:
      1) Group TCP segments by flow (5-tuple) where dst is loopback and dport is MQTT.
      2) Reassemble each flow to a byte stream.
      3) Track protocol level from CONNECT (type 1) on that flow.
      4) For each PUBLISH, parse topic and append its layer count.
    """
    packets = rdpcap(str(pcap_path))
    # Each key is one TCP connection from some client to the local broker.
    flows: dict[tuple[str, int, str, int], list[tuple[int, bytes]]] = defaultdict(list)

    for p in packets:
        if not p.haslayer(TCP) or not p.haslayer(Raw):
            continue

        fk = _flow_key(p)
        if fk is None:
            continue

        _src, _sport, dst, dport = fk
        # Only client → local broker MQTT
        if dst not in LOCAL_BROKERS or dport != mqtt_port:
            continue

        flows[fk].append((int(p[TCP].seq), bytes(p[Raw].load)))

    depths: list[int] = []

    for chunks in flows.values():
        stream = _reassemble_flow(chunks)
        protocol_level: int | None = None

        for msg_type, flags, body in _read_mqtt_frames(stream):
            if msg_type == 1:
                # CONNECT — refresh protocol level for this connection
                lvl = _connect_protocol_level(body)
                if lvl is not None:
                    protocol_level = lvl
            elif msg_type == 3:
                # PUBLISH — if CONNECT was missed, assume 3.1.1 (level 4)
                level = protocol_level if protocol_level is not None else 4
                topic = _parse_publish_topic(flags, body, level)
                if topic is None or topic == "":
                    continue
                depths.append(topic_layer_count(topic))

    return depths


def plot_grouped_histogram(
    depths_a: list[int],
    depths_b: list[int],
    out_path: Path,
    label_a: str,
    label_b: str,
) -> None:
    """
    Grouped bars: for each topic depth present in either capture, draw A vs B.

    X-axis: number of topic layers; Y-axis: how many PUBLISH messages.
    """
    # Use a light grid if available; otherwise matplotlib's default style.
    if "seaborn-v0_8-whitegrid" in plt.style.available:
        plt.style.use("seaborn-v0_8-whitegrid")
    else:
        plt.style.use("default")

    count_a = Counter(depths_a)
    count_b = Counter(depths_b)
    all_depths = sorted(set(count_a) | set(count_b))

    if not all_depths:
        raise SystemExit("No local-broker PUBLISH messages found — nothing to plot.")

    heights_a = [count_a[d] for d in all_depths]
    heights_b = [count_b[d] for d in all_depths]

    x_positions = range(len(all_depths))
    bar_width = 0.36

    fig, ax = plt.subplots(figsize=(9, 5))

    # Slight horizontal offset so A and B bars sit side-by-side per depth.
    ax.bar(
        [i - bar_width / 2 for i in x_positions],
        heights_a,
        bar_width,
        label=label_a,
        color="#1f77b4",
        edgecolor="black",
        linewidth=0.4,
    )
    ax.bar(
        [i + bar_width / 2 for i in x_positions],
        heights_b,
        bar_width,
        label=label_b,
        color="#ff7f0e",
        edgecolor="black",
        linewidth=0.4,
    )

    ax.set_xlabel("Number of topic layers")
    ax.set_ylabel("Number of PUBLISH messages")
    ax.set_title("Distribution of MQTT topic depth for local broker")
    ax.set_xticks(list(x_positions))
    ax.set_xticklabels([str(d) for d in all_depths])
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> None:
    """Run CQ8 analysis on the configured PCAP paths and write the figure."""
    depths_a = publish_depths_for_pcap(PCAP_A, mqtt_port=MQTT_PORT)
    depths_b = publish_depths_for_pcap(PCAP_B, mqtt_port=MQTT_PORT)

    print(f"CQ8a — PUBLISH messages to local broker in {PCAP_A.name}: {len(depths_a)}")
    print(f"CQ8b — PUBLISH messages to local broker in {PCAP_B.name}: {len(depths_b)}")

    plot_grouped_histogram(
        depths_a,
        depths_b,
        OUTPUT_FIGURE,
        label_a=CHART_LABEL_A,
        label_b=CHART_LABEL_B,
    )


if __name__ == "__main__":
    main()
