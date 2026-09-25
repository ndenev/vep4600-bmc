#!/usr/bin/env python3

import argparse
import hashlib
import stat
import struct
import sys
import zlib
from pathlib import Path


MODULE_MAGIC = b"$MODULE$"
CRAMFS_MAGIC = 0x28CD3D45
LIB_PATH = "/usr/local/lib/libipmipdk.so.2.26.0"


def u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def p32(buf, off, val):
    struct.pack_into("<I", buf, off, val & 0xFFFFFFFF)


def parse_ladder(text, name):
    vals = [int(x, 0) for x in text.replace(",", " ").split()]
    if len(vals) != 5:
        raise SystemExit(f"{name} must contain exactly 5 values")
    if any(v < 0 or v > 255 for v in vals):
        raise SystemExit(f"{name} values must fit in one byte")
    return bytes(vals)


def inode(buf, p):
    a, b, c = struct.unpack_from("<III", buf, p)
    return {
        "mode": a & 0xFFFF,
        "uid": a >> 16,
        "size": b & 0x00FFFFFF,
        "gid": b >> 24,
        "namelen": c & 0x3F,
        "offset": c >> 6,
        "pos": p,
    }


def name_at(buf, p, words):
    raw = buf[p : p + words * 4]
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace"), words * 4


class Cramfs:
    def __init__(self, image, base, alloc_size):
        self.image = image
        self.base = base
        self.alloc_size = alloc_size
        self.buf = image[base : base + alloc_size]
        if u32(self.buf, 0) != CRAMFS_MAGIC:
            raise SystemExit(f"no little-endian cramfs at 0x{base:x}")
        self.size = u32(self.buf, 4)
        if self.size > alloc_size:
            raise SystemExit(
                f"cramfs used size 0x{self.size:x} exceeds allocation 0x{alloc_size:x}"
            )
        self.root = inode(self.buf, 64)

    def walk_dir(self, node=None, prefix=""):
        if node is None:
            node = self.root
        p = node["offset"] << 2
        end = p + node["size"]
        while p < end:
            child = inode(self.buf, p)
            p += 12
            name, n = name_at(self.buf, p, child["namelen"])
            p += n
            path = prefix + "/" + name
            yield path, child
            if stat.S_ISDIR(child["mode"]):
                yield from self.walk_dir(child, path)

    def find(self, wanted):
        wanted = "/" + wanted.strip("/")
        for path, node in self.walk_dir():
            if path == wanted:
                return node
        return None

    def read_file(self, node):
        if not stat.S_ISREG(node["mode"]):
            raise SystemExit(f"{LIB_PATH} is not a regular file")
        if node["size"] == 0:
            return b""
        table = node["offset"] << 2
        blocks = (node["size"] + 4095) // 4096
        ends = list(struct.unpack_from("<" + "I" * blocks, self.buf, table))
        out = bytearray()
        start = table + blocks * 4
        for end in ends:
            out.extend(zlib.decompress(self.buf[start:end]))
            start = end
        return bytes(out[: node["size"]])

    def replace_file_same_size(self, node, data):
        if len(data) != node["size"]:
            raise SystemExit("replacement file size changed; cramfs inode update not implemented")

        table = node["offset"] << 2
        blocks = (node["size"] + 4095) // 4096
        old_ends = list(struct.unpack_from("<" + "I" * blocks, self.buf, table))
        data_start = table + blocks * 4
        data_end = old_ends[-1]
        old_span = data_end - data_start

        chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)]
        best = None
        best_level = None
        # Prefer level 9 because it reproduced the known patched image exactly.
        for level in (9, 8, 7, 6, 5, 4, 3, 2, 1):
            compressed = [zlib.compress(chunk, level) for chunk in chunks]
            total = sum(len(c) for c in compressed)
            if total <= old_span:
                best = compressed
                best_level = level
                break
        if best is None:
            raise SystemExit(
                f"patched file compressed data does not fit: need > {old_span} bytes"
            )

        p = data_start
        new_ends = []
        for comp in best:
            self.buf[p : p + len(comp)] = comp
            p += len(comp)
            new_ends.append(p)
        if p < data_end:
            self.buf[p:data_end] = b"\0" * (data_end - p)
        struct.pack_into("<" + "I" * blocks, self.buf, table, *new_ends)
        return old_span, p - data_start, best_level

    def update_crc(self):
        used = self.size
        old = u32(self.buf, 0x20)
        p32(self.buf, 0x20, 0)
        crc = zlib.crc32(self.buf[:used]) & 0xFFFFFFFF
        p32(self.buf, 0x20, crc)
        self.image[self.base : self.base + self.alloc_size] = self.buf
        return old, crc


def find_fmh_headers(image):
    headers = []
    for off in range(0, len(image) - 64 + 1, 0x40):
        if image[off : off + 8] != MODULE_MAGIC:
            continue
        if image[off + 0x3E : off + 0x40] != b"\xaa\x55":
            continue
        hdr = image[off : off + 64]
        name = hdr[0x18:0x20].split(b"\0", 1)[0].decode("ascii", "replace")
        headers.append(
            {
                "off": off,
                "name": name,
                "type": u16(image, off + 0x22),
                "payload": u32(image, off + 0x24),
                "size": u32(image, off + 0x28),
                "crc": u32(image, off + 0x32),
            }
        )
    return headers


def rebalance_fmh_header(image, off, checksum_offset=0x31):
    image[off + checksum_offset] = 0
    image[off + checksum_offset] = (-sum(image[off : off + 64])) & 0xFF
    if sum(image[off : off + 64]) & 0xFF:
        raise SystemExit(f"FMH checksum did not balance at 0x{off:x}")


def update_root_fmh(image, header, root_used_size):
    payload = header["payload"]
    old = u32(image, header["off"] + 0x32)
    crc = zlib.crc32(image[payload : payload + root_used_size]) & 0xFFFFFFFF
    p32(image, header["off"] + 0x32, crc)
    rebalance_fmh_header(image, header["off"])
    return old, crc


def update_image_yafu_crc(image, headers):
    type2 = [h for h in headers if h["type"] == 2]
    if len(type2) != 1:
        raise SystemExit(f"expected one type-2/YAFU image checksum FMH, found {len(type2)}")
    h = type2[0]
    off = h["off"]
    old = u32(image, off + 0x32)
    crc_input = bytearray()
    for i, b in enumerate(image):
        if off + 0x32 <= i <= off + 0x35:
            continue
        if i == off + 0x17:
            continue
        crc_input.append(b)
    crc = zlib.crc32(crc_input) & 0xFFFFFFFF
    p32(image, off + 0x32, crc)
    # The type-2 ast2500e/YAFU checksum header uses byte +0x17 for its
    # additive header checksum. Yafuflash also excludes this byte from the
    # image-wide CRC.
    rebalance_fmh_header(image, off, 0x17)
    return h["name"], old, crc


def apply_patch(data, offset, old_hex, new_hex, label):
    old = bytes.fromhex(old_hex)
    new = bytes.fromhex(new_hex)
    cur = data[offset : offset + len(old)]
    if cur == new:
        return "already"
    if cur != old:
        raise SystemExit(
            f"{label}: unexpected bytes at 0x{offset:x}: got {cur.hex()} expected {old.hex()}"
        )
    data[offset : offset + len(old)] = new
    return "patched"


def patch_library(lib, rise, decline, pwm):
    data = bytearray(lib)
    results = []
    patches = [
        (0x0ABAC, "9a01001a", "0000a0e1", "Fan5_Status gate"),
        (0x0AE0C, "0201001a", "0000a0e1", "Fan5 RPM gate"),
        (0x12040, "0100000a", "010000ea", "all-fan PWM Fan5 branch"),
        (0x12B08, "8ed0ffeb", "0000a0e1", "Fan5 LED-off call"),
        (0x131D8, "98ceffeb", "0000a0e1", "Fan5 PWM-zero call"),
    ]
    for offset, old, new, label in patches:
        results.append((label, apply_patch(data, offset, old, new, label)))

    old_ladder = bytes.fromhex("1b202428ff181b2024ff1432415064")
    new_ladder = rise + decline + pwm
    cur = data[0x198FC : 0x198FC + len(old_ladder)]
    if cur == new_ladder:
        results.append(("FSC ladder", "already"))
    elif cur == old_ladder:
        data[0x198FC : 0x198FC + len(old_ladder)] = new_ladder
        results.append(("FSC ladder", "patched"))
    else:
        raise SystemExit(
            "FSC ladder: unexpected bytes at 0x198fc: "
            f"got {cur.hex()} expected {old_ladder.hex()}"
        )

    return bytes(data), results


def main():
    ap = argparse.ArgumentParser(
        description="Patch Dell VEP4600 BMC .ima for Fan5 support and FSC ladder changes."
    )
    ap.add_argument("input_ima", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--rise", default="30 35 40 46 255")
    ap.add_argument("--decline", default="27 32 37 43 255")
    ap.add_argument("--pwm", default="20 30 45 65 100")
    ap.add_argument("--library", default=LIB_PATH)
    args = ap.parse_args()

    rise = parse_ladder(args.rise, "--rise")
    decline = parse_ladder(args.decline, "--decline")
    pwm = parse_ladder(args.pwm, "--pwm")

    image = bytearray(args.input_ima.read_bytes())
    headers = find_fmh_headers(image)
    root_headers = [h for h in headers if h["name"] == "root"]
    if len(root_headers) != 1:
        raise SystemExit(f"expected one root FMH, found {len(root_headers)}")
    root_h = root_headers[0]

    fs = Cramfs(image, root_h["payload"], root_h["size"])
    node = fs.find(args.library)
    if node is None:
        raise SystemExit(f"not found in cramfs: {args.library}")

    old_lib = fs.read_file(node)
    new_lib, patch_results = patch_library(old_lib, rise, decline, pwm)
    old_span, new_span, zlevel = fs.replace_file_same_size(node, new_lib)
    old_cramfs_crc, new_cramfs_crc = fs.update_crc()
    old_root_crc, new_root_crc = update_root_fmh(image, root_h, fs.size)
    yafu_name, old_yafu_crc, new_yafu_crc = update_image_yafu_crc(image, headers)

    args.output.write_bytes(image)

    print(f"input:  {args.input_ima}")
    print(f"output: {args.output}")
    print(f"sha256: {hashlib.sha256(image).hexdigest()}")
    print(f"root payload: 0x{root_h['payload']:x}, root used size: 0x{fs.size:x}")
    print(f"library: {args.library}, size: {len(new_lib)}")
    for label, status in patch_results:
        print(f"{label}: {status}")
    print(f"compressed library span: old={old_span} new={new_span} zlib_level={zlevel}")
    print(f"cramfs crc: 0x{old_cramfs_crc:08x} -> 0x{new_cramfs_crc:08x}")
    print(f"root FMH crc: 0x{old_root_crc:08x} -> 0x{new_root_crc:08x}")
    print(f"{yafu_name} image crc: 0x{old_yafu_crc:08x} -> 0x{new_yafu_crc:08x}")


if __name__ == "__main__":
    main()
