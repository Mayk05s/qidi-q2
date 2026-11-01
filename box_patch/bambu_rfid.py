# Read Bambu Lab spool tags through the QIDI Box FM17550 reader.
#
# Install as klippy/extras/bambu_rfid.py. box_rfid.so stays in place: with
# hook_stock_read enabled, its card read is wrapped so that a Bambu tag is
# answered with the legacy QIDI codes and the stock code saves them to the slot.
# The exact tag color and any mapped profile then replace the stock values in that
# slot, and what the tag really holds is saved as bambu_tag_slotN for the panel.
# Other tags go to the stock read unchanged. Reads never write to a tag; only
# BAMBU_RFID_TAG_WRITE does, and only to a tag named by its UID.
# bambu_map.cfg next to printer.cfg holds editable material and color mappings.
#
# Config, one section per reader:
#   [bambu_rfid card_reader_1]
#   cs_pin: mcu_box1:PC6
#   hook_stock_read: True
#
# G-code:
#   BAMBU_RFID_DIAG READER=card_reader_1   read chip registers only, RF untouched
#   BAMBU_RFID_READ READER=card_reader_1 [TIMEOUT=30]
#       wait for a tag to pass the antenna while the spool turns, read it,
#       then restore the registers
#   BAMBU_RFID_TAG_READ READER=card_reader_1 ID=1 [TIMEOUT=15] [EXCLUDE=7081205A,...]
#   BAMBU_RFID_TAG_WRITE READER=card_reader_1 ID=2 UID=7081205A BLOCK4=<hex> [BLOCK5=<hex>]
#       read or write a QIDI-format tag; the result is in this object's status as tag_result
#   BAMBU_RFID_SPOOL_SET SLOT=0 GRAMS=800
#       set how much filament the spool in a slot holds; printing then subtracts from it.
#       Kept in spool_usage.json next to printer.cfg, by tag, so it follows the spool.
import configparser
import hashlib
import hmac
import json
import logging
import os
import re
import struct

# FM17550 registers
REG_COMMAND = 0x01
REG_COM_IRQ = 0x04
REG_ERROR = 0x06
REG_STATUS2 = 0x08
REG_FIFO_DATA = 0x09
REG_FIFO_LEVEL = 0x0A
REG_CONTROL = 0x0C
REG_BIT_FRAMING = 0x0D
REG_TX_MODE = 0x12
REG_RX_MODE = 0x13
REG_TX_CONTROL = 0x14
REG_TX_AUTO = 0x15
REG_MOD_WIDTH = 0x24
REG_RF_CFG = 0x26
REG_CW_GS_P = 0x28
REG_T_MODE = 0x2A
REG_T_PRESCALER = 0x2B
REG_T_RELOAD_HI = 0x2C
REG_T_RELOAD_LO = 0x2D
REG_VERSION = 0x37

CMD_IDLE = 0x00
CMD_TRANSCEIVE = 0x0C
CMD_AUTHENT = 0x0E

IRQ_RX = 0x20
IRQ_IDLE = 0x10
IRQ_TIMER = 0x01
ERR_COLLISION = 0x08
ERR_CRC = 0x04
ERR_FATAL = 0xD3  # write, temperature, buffer, parity and protocol errors
CRYPTO1_ON = 0x08
RF_ON = 0x03

# Registers changed by a tag read and restored afterwards. Command goes last:
# its reset value 0x20 switches the receiver off, which a read must undo and put back.
SAVED_REGS = (REG_CONTROL, REG_TX_MODE, REG_RX_MODE, REG_TX_CONTROL, REG_TX_AUTO,
              REG_MOD_WIDTH, REG_T_MODE, REG_T_PRESCALER, REG_T_RELOAD_HI,
              REG_T_RELOAD_LO, REG_RF_CFG, REG_CW_GS_P, REG_COMMAND)
DIAG_REGS = (("Version", REG_VERSION), ("Command", REG_COMMAND),
             ("Status2", REG_STATUS2), ("Control", REG_CONTROL),
             ("TxMode", REG_TX_MODE), ("RxMode", REG_RX_MODE),
             ("TxControl", REG_TX_CONTROL), ("TxAuto", REG_TX_AUTO),
             ("ModWidth", REG_MOD_WIDTH), ("RFCfg", 0x26),
             ("RxThreshold", 0x18), ("GsNOn", 0x27), ("CWGsP", 0x28))

POLL_ATTEMPTS = 60
POLL_DELAY = 0.002
FIELD_SETTLE = 0.02
RETRY_DELAY = 0.05
# The stock code stops unless two reads in a row match, but a turning tag may be
# gone by the next poll. Repeat a good answer long enough to cover both reads.
ANSWER_HOLD = 1.0
EXACT_COLOR_DELAY = 2.5
# A tag written by our panel marks block 5 with this, then RGB and a profile id.
CUSTOM_COLOR_MAGIC = b"QXCL"
DEFAULT_KEY = bytes.fromhex("FFFFFFFFFFFF")
# Tried in this order: QIDI factory tags use the first, blank tags the second.
TAG_KEYS = (("qidi", bytes.fromhex("D3F7D3F7D3F7")), ("default", DEFAULT_KEY))
CLASSIC_1K_SAK = 0x08
MIFARE_WRITE = 0xA0
MIFARE_ACK = 0x0A
TAG_BLOCKS = (4, 5, 6)
# Never the sector trailer, block 7: a bad write there locks the sector for good.
WRITABLE_BLOCKS = (4, 5)
TAG_READ_TIMEOUT = 15.
# Both tags of a Bambu spool hold the same tray number here, in sector 2.
TRAY_SECTOR_BLOCK = 8
TRAY_BLOCK = 9
USAGE_FILE = "spool_usage.json"
USAGE_INTERVAL = 1.
USAGE_SAVE_INTERVAL = 30.
BOX_SLOTS = 4
MAX_SPOOL_GRAMS = 5000
FILAMENT_MM2 = 2.405  # Cross-section of 1.75 mm filament.
DEFAULT_DENSITY = 1.24
DENSITY = {"PLA": 1.24, "PETG": 1.27, "PET": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21,
           "PA": 1.14, "PC": 1.20, "PVA": 1.23, "PPS": 1.30}
TAG_WRITE_TIMEOUT = 5.
# A QIDI Studio profile id such as QFL30, usable as a material in bambu_map.cfg.
PROFILE_ID = re.compile(r"QF[A-Z0-9]{2,4}")

BAMBU_MASTER = bytes.fromhex("9a759cf2c4f7caff222cb9769b41bc96")

# Codes from the Q2 firmware config officiall_filas_list.cfg
QIDI_COLORS = {
    1: "FAFAFA", 2: "060606", 3: "D9E3ED", 4: "5CF30F", 5: "63E492", 6: "2850FF",
    7: "FE98FE", 8: "DFD628", 9: "228332", 10: "99DEFF", 11: "1714B0", 12: "CEC0FE",
    13: "CADE4B", 14: "1353AB", 15: "5EA9FD", 16: "A878FF", 17: "FE717A", 18: "FF362D",
    19: "E2DFCD", 20: "898F9B", 21: "6E3812", 22: "CAC59F", 23: "F28636", 24: "B87F2B",
}
COLORED_CHROMA = 10.
NEUTRAL_CHROMA = 12.
LIGHTNESS_WEIGHT = 1.5
# Longest matching prefix of the Bambu detailed type wins.
QIDI_MATERIALS = (
    ("PLA Silk", 4), ("PLA Metal", 3), ("PLA Matte", 2), ("PLA Wood", 6),
    ("PLA-CF", 5), ("PLA Lite", 7), ("PLA Basic", 7), ("PLA", 7),
    ("PETG HF", 41), ("PETG-CF", 42), ("PETG Translucent", 45), ("PETG", 39),
    ("ABS-GF", 12), ("ABS", 11), ("ASA-CF", 20), ("ASA Aero", 19), ("ASA", 18),
    ("TPU", 50), ("PA6-CF", 25), ("PAHT-CF", 30), ("PA-CF", 25), ("PA", 24),
    ("PET-CF", 37), ("PC", 23), ("PVA", 47),
)


class RfidError(Exception):
    pass


class TagAbsent(RfidError):
    """No tag answered the wake-up: nothing is at the antenna right now."""


class NotBambuTag(RfidError):
    """A failed authentication halts the tag, so retrying cannot help."""


class SeveralTags(RfidError):
    """More than one tag answered, such as a spool's next to the one being written."""


class TagProblem(RfidError):
    """This tag cannot be used, so retrying cannot help. `code` names the reason."""

    def __init__(self, code, uid=None):
        RfidError.__init__(self, code)
        self.code = code
        self.uid = uid


def derive_sector_keys(uid):
    """HKDF-SHA256 of the UID: 16 Key A values, one per sector."""
    prk = hmac.new(BAMBU_MASTER, uid, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < 96:
        block = hmac.new(prk, block + b"RFID-A\0" + bytes([counter]),
                         hashlib.sha256).digest()
        okm += block
        counter += 1
    return [okm[i * 6:i * 6 + 6] for i in range(16)]


def _lab(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255. for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
               for c in rgb]
    xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
           0.2126 * r + 0.7152 * g + 0.0722 * b,
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    fx, fy, fz = [v ** (1. / 3) if v > 0.008856 else 7.787 * v + 16. / 116
                  for v in xyz]
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _chroma(lab):
    return (lab[1] ** 2 + lab[2] ** 2) ** 0.5


def nearest_qidi_color(hex_color):
    """Closest QIDI color code in CIELAB. A muted color must not land on a neutral
    such as grey, and lightness weighs more so a dark olive does not become khaki."""
    target = _lab(hex_color)
    colored = _chroma(target) >= COLORED_CHROMA
    candidates = [code for code in QIDI_COLORS
                  if colored == (_chroma(_lab(QIDI_COLORS[code])) >= NEUTRAL_CHROMA)]

    def distance(code):
        lab = _lab(QIDI_COLORS[code])
        return ((LIGHTNESS_WEIGHT * (target[0] - lab[0])) ** 2
                + (target[1] - lab[1]) ** 2 + (target[2] - lab[2]) ** 2)
    return min(candidates, key=distance)


def qidi_material(detail, materials=QIDI_MATERIALS):
    matches = [m for m in materials if detail.startswith(m[0])]
    if not matches:
        return None
    return max(matches, key=lambda m: len(m[0]))[1]


def _text(data):
    return data.split(b"\0")[0].decode("ascii", "replace")


def load_map(path):
    """Read bambu_map.cfg. Returns (materials, colors); raises ValueError if invalid."""
    parser = configparser.ConfigParser(delimiters=("=",), comment_prefixes=("#", ";"),
                                       inline_comment_prefixes=("#", ";"),
                                       interpolation=None)
    parser.optionxform = str
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        raise ValueError(str(e))
    materials = []
    for name, code in parser.items("materials") if parser.has_section("materials") else ():
        if PROFILE_ID.fullmatch(code):
            materials.append((name, code))
        elif code.isdigit() and 1 <= int(code) <= 99:
            materials.append((name, int(code)))
        else:
            raise ValueError("bad material code: %s = %s" % (name, code))
    colors = {}
    for source, shown in parser.items("colors") if parser.has_section("colors") else ():
        source, shown = source.lstrip("#").upper(), shown.lstrip("#").upper()
        if not re.fullmatch("[0-9A-F]{6}", source) or not re.fullmatch("[0-9A-F]{6}", shown):
            raise ValueError("bad color: %s = %s" % (source, shown))
        colors[source] = shown
    return tuple(materials) or QIDI_MATERIALS, colors


def parse_bambu(block4, block5, block6, mapping=(QIDI_MATERIALS, {})):
    materials, colors = mapping
    tag_color = block5[:3].hex().upper()
    color = colors.get(tag_color, tag_color)
    detail = _text(block4)
    material = qidi_material(detail, materials)
    profile = material if isinstance(material, str) else None
    if profile is not None:
        # The stock code needs a legacy code to accept the read; the module
        # replaces the profile afterwards.
        material = qidi_material(detail) or 7
    return {
        "brand": "Bambu Lab",
        "diameter": round(struct.unpack("<f", block5[8:12])[0], 2),
        "dry_temp": int.from_bytes(block6[0:2], "little"),
        "dry_hours": int.from_bytes(block6[2:4], "little"),
        "tag_color": tag_color,
        "profile": profile,
        "detail": detail,
        "color": color,
        "alpha": block5[3],
        "weight": int.from_bytes(block5[4:6], "little"),
        "min_temp": int.from_bytes(block6[10:12], "little"),
        "max_temp": int.from_bytes(block6[8:10], "little"),
        "qidi_material": material,
        "qidi_color": nearest_qidi_color(color),
    }


class Fm17550:
    """Register-level reader driver. `spi` needs spi_transfer(); `pause` sleeps."""

    def __init__(self, spi, pause, get_mapping=None):
        self.spi = spi
        self.pause = pause
        self.get_mapping = get_mapping
        self.sak = None

    def read_reg(self, reg):
        return bytes(self.spi.spi_transfer([(reg << 1) | 0x80, 0])["response"])[1]

    def write_reg(self, reg, value):
        self.spi.spi_transfer([reg << 1, value])

    def _reset_comm(self):
        self.write_reg(REG_COMMAND, CMD_IDLE)
        self.write_reg(REG_FIFO_LEVEL, 0x80)
        self.write_reg(REG_COM_IRQ, 0x7F)

    def _write_fifo(self, data):
        for byte in data:
            self.write_reg(REG_FIFO_DATA, byte)

    def _crc(self, enabled):
        value = 0x80 if enabled else 0
        self.write_reg(REG_TX_MODE, value)
        self.write_reg(REG_RX_MODE, value)

    def transceive(self, data, last_bits=0, crc=False, tx_crc=False):
        self._crc(crc)
        if tx_crc:  # A write is acknowledged with 4 bits and no CRC.
            self.write_reg(REG_TX_MODE, 0x80)
        self._reset_comm()
        self._write_fifo(data)
        self.write_reg(REG_COMMAND, CMD_TRANSCEIVE)
        self.write_reg(REG_BIT_FRAMING, 0x80 | last_bits)
        try:
            for _ in range(POLL_ATTEMPTS):
                irq = self.read_reg(REG_COM_IRQ)
                if irq & IRQ_RX:
                    break
                if irq & IRQ_TIMER:
                    raise RfidError("no answer from tag")
                self.pause(POLL_DELAY)
            else:
                raise RfidError("reader did not finish the exchange")
            err = self.read_reg(REG_ERROR)
            if err & ERR_COLLISION:
                raise SeveralTags("several tags are in the field")
            if err & (ERR_FATAL | (ERR_CRC if crc else 0)):
                raise RfidError("reader error 0x%02X" % (err,))
            level = self.read_reg(REG_FIFO_LEVEL) & 0x7F
            return bytes(self.read_reg(REG_FIFO_DATA) for _ in range(level))
        finally:
            self.write_reg(REG_BIT_FRAMING, 0)
            self.write_reg(REG_COMMAND, CMD_IDLE)

    def snapshot(self):
        return [(reg, self.read_reg(reg)) for reg in SAVED_REGS]

    def restore(self, saved):
        self.write_reg(REG_STATUS2, self.read_reg(REG_STATUS2) & ~CRYPTO1_ON)
        self._reset_comm()
        self.write_reg(REG_BIT_FRAMING, 0)
        for reg, value in saved:
            self.write_reg(reg, value)

    def setup_type_a(self):
        self.write_reg(REG_CONTROL, 0x10)
        self.write_reg(REG_MOD_WIDTH, 0x26)
        # Reset values read back from the chip are 0x48 and 0x20. Higher receiver
        # gain and field strength extend the range for a tag passing on a spool.
        self.write_reg(REG_RF_CFG, 0x68)
        self.write_reg(REG_CW_GS_P, 0x3F)
        self.write_reg(REG_TX_AUTO, self.read_reg(REG_TX_AUTO) | 0x40)
        # 25 ms timeout that starts when transmission ends
        self.write_reg(REG_T_MODE, 0x80)
        self.write_reg(REG_T_PRESCALER, 3)
        self.write_reg(REG_T_RELOAD_HI, 0xBD)
        self.write_reg(REG_T_RELOAD_LO, 0x2C)
        tx_control = self.read_reg(REG_TX_CONTROL)
        if tx_control & RF_ON != RF_ON:
            self.write_reg(REG_TX_CONTROL, tx_control | RF_ON)
            self.pause(FIELD_SETTLE)

    def select_tag(self):
        """Activate a tag with a 4-byte UID and return that UID."""
        # WUPA rather than REQA: it also wakes a tag left halted by an earlier try.
        try:
            atqa = self.transceive([0x52], last_bits=7)
        except RfidError as e:
            raise TagAbsent(str(e))
        if len(atqa) != 2:
            raise RfidError("unexpected tag answer")
        frame = self.transceive([0x93, 0x20])
        if len(frame) != 5 or frame[0] ^ frame[1] ^ frame[2] ^ frame[3] != frame[4]:
            raise RfidError("bad UID checksum")
        if frame[0] == 0x88:
            raise RfidError("not a 4-byte UID tag")
        sak = self.transceive([0x93, 0x70] + list(frame), crc=True)
        if len(sak) != 1 or sak[0] & 0x04:
            raise RfidError("tag selection failed")
        self.sak = sak[0]
        return frame[:4]

    def authenticate(self, block, key, uid):
        self._crc(True)
        self.write_reg(REG_STATUS2, 0)
        self._reset_comm()
        self._write_fifo([0x60, block] + list(key) + list(uid))
        self.write_reg(REG_COMMAND, CMD_AUTHENT)
        self.write_reg(REG_BIT_FRAMING, 0x80)
        for _ in range(POLL_ATTEMPTS):
            if self.read_reg(REG_STATUS2) & CRYPTO1_ON:
                self.write_reg(REG_BIT_FRAMING, 0)
                return
            self.pause(POLL_DELAY)
        self.write_reg(REG_BIT_FRAMING, 0)
        raise NotBambuTag("key rejected: this is not a Bambu tag")

    def read_block(self, block):
        data = self.transceive([0x30, block], crc=True)
        if len(data) != 16:
            raise RfidError("short read of block %d" % (block,))
        return data

    def write_block(self, block, data):
        """MIFARE write in its two steps, then read back. Needs an authenticated sector."""
        if block not in WRITABLE_BLOCKS or len(data) != 16:
            raise ValueError("refused write to block %r" % (block,))
        for frame in ([MIFARE_WRITE, block], list(data)):
            answer = self.transceive(frame, tx_crc=True)
            if len(answer) != 1:
                raise RfidError("no write acknowledgement")
            if answer[0] & 0x0F != MIFARE_ACK:
                raise TagProblem("denied")
        if self.read_block(block) != bytes(data):
            raise RfidError("read back differs from what was written")

    def _open_tag(self, exclude=()):
        """Select a tag and authenticate sector 1 with a QIDI key; returns (uid, key name).
        A tag in `exclude` counts as no tag, so a search goes on to the next one."""
        for name, key in TAG_KEYS:
            self.write_reg(REG_STATUS2, 0)
            uid = self.select_tag()
            if uid in exclude:
                raise TagAbsent("only an excluded tag is in the field")
            if self.sak != CLASSIC_1K_SAK:
                raise TagProblem("not_classic", uid)
            try:
                self.authenticate(4, key, uid)
                return uid, name
            except NotBambuTag:
                pass
        self.write_reg(REG_STATUS2, 0)
        uid = self.select_tag()
        try:
            self.authenticate(4, derive_sector_keys(uid)[1], uid)
        except NotBambuTag:
            raise TagProblem("locked", uid)
        raise TagProblem("bambu", uid)

    def read_tag(self, timeout, monotonic, check_abort=None, exclude=()):
        def action():
            uid, key = self._open_tag(exclude)
            blocks = {b: self.read_block(b).hex().upper() for b in TAG_BLOCKS}
            return {"uid": uid.hex().upper(), "key": key, "blocks": blocks}
        return self._retrying(action, timeout, monotonic, check_abort)

    def write_tag(self, uid, blocks, timeout, monotonic, check_abort=None):
        """Write only to the tag `uid`: another tag may have come to the antenna since it was read."""
        def action():
            found, key = self._open_tag()
            if found != uid:
                raise TagProblem("other_tag")
            for block, data in sorted(blocks.items()):
                self.write_block(block, data)
            return {"uid": found.hex().upper(), "key": key}
        return self._retrying(action, timeout, monotonic, check_abort)

    def _read_tray(self, uid):
        """Hex tray number, or None. The tag is already read, so a failure here costs nothing.
        Crypto stays on: moving to another sector of a selected tag is an encrypted exchange."""
        try:
            self._reset_comm()
            self._write_fifo([0x60, TRAY_SECTOR_BLOCK] + list(derive_sector_keys(uid)[2]) + list(uid))
            self.write_reg(REG_COMMAND, CMD_AUTHENT)
            self.write_reg(REG_BIT_FRAMING, 0x80)
            for _ in range(POLL_ATTEMPTS):
                if self.read_reg(REG_COM_IRQ) & (IRQ_IDLE | IRQ_TIMER):
                    break
                self.pause(POLL_DELAY)
            self.write_reg(REG_BIT_FRAMING, 0)
            if not self.read_reg(REG_STATUS2) & CRYPTO1_ON or self.read_reg(REG_ERROR) & ERR_FATAL:
                return None
            data = self.read_block(TRAY_BLOCK)
        except RfidError:
            return None
        return data.hex().upper() if any(data) else None

    def _read_once(self):
        uid = self.select_tag()
        self.authenticate(4, derive_sector_keys(uid)[1], uid)
        blocks = [self.read_block(b) for b in (4, 5, 6)]
        if self.get_mapping is None:
            tag = parse_bambu(*blocks)
        else:
            tag = parse_bambu(*blocks, mapping=self.get_mapping())
        tag["tray_uid"] = self._read_tray(uid)
        return uid, tag

    def read_custom(self):
        """Return (block 4, tag) for a tag written with an exact color, else None."""
        saved = self.snapshot()
        try:
            self.setup_type_a()
            uid = self.select_tag()
            self.authenticate(4, DEFAULT_KEY, uid)
            block4 = self.read_block(4)
            block5 = self.read_block(5)
        finally:
            self.restore(saved)
        if block5[:4] != CUSTOM_COLOR_MAGIC or block4[1] not in QIDI_COLORS:
            return None
        profile = block5[7:13].split(b"\0")[0].decode("ascii", "replace")
        return bytes(block4), {"color": block5[4:7].hex().upper(), "qidi_color": block4[1],
                               "uid": uid.hex().upper(), "detail": "Своя метка",
                               "profile": profile if PROFILE_ID.fullmatch(profile) else None}

    def read_bambu(self, timeout=0., monotonic=None, check_abort=None):
        """Return (uid, parsed tag), retrying until timeout as the tag may be
        out of range while the spool turns. Registers are restored on any exit."""
        return self._retrying(self._read_once, timeout, monotonic, check_abort)

    def _retrying(self, action, timeout, monotonic, check_abort):
        saved = self.snapshot()
        try:
            self.setup_type_a()
            deadline = monotonic() + timeout if timeout else 0.
            while True:
                try:
                    return action()
                except (NotBambuTag, TagProblem):
                    raise
                except RfidError:
                    if not timeout or monotonic() >= deadline:
                        raise
                if check_abort is not None:
                    check_abort()
                self.write_reg(REG_STATUS2, 0)
                self.pause(RETRY_DELAY)
        finally:
            self.restore(saved)


def _clean_text(text):
    # The value travels inside a quoted G-code parameter.
    return re.sub(r"[^\w +.\-]", "", text)[:32]


def slot_update_scripts(variables, stepper_name, tag):
    """G-code that finishes a read in the slot: exact color, mapped profile and the
    record of the real tag. Empty unless the slot holds the palette color we answered
    with, so a read the stock code rejected, or a change made since, is left alone."""
    color_name = "color_" + stepper_name
    filament_name = "filament_" + stepper_name
    expected = QIDI_COLORS[tag["qidi_color"]]
    if variables.get(color_name) != [expected]:
        return []
    scripts = []
    if tag["color"] != expected:
        scripts.append("SAVE_VARIABLE VARIABLE=%s VALUE=\"['%s']\"" % (color_name, tag["color"]))
    filament = tag.get("profile") or variables.get(filament_name)
    if tag.get("profile"):
        scripts.append("SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (filament_name, tag["profile"]))
    record = {"uid": tag.get("uid", ""), "detail": _clean_text(tag.get("detail", "")),
              "tag_color": tag.get("tag_color", tag["color"]), "color": tag["color"],
              "filament": filament}
    for key in ("weight", "diameter", "min_temp", "max_temp", "dry_temp", "dry_hours"):
        if tag.get(key) is not None:
            record[key] = tag[key]
    if tag.get("tray_uid"):
        record["tray"] = tag["tray_uid"]
    if tag.get("brand"):
        record["brand"] = _clean_text(tag["brand"])
    scripts.append("SAVE_VARIABLE VARIABLE=bambu_tag_%s VALUE=\"%r\"" % (stepper_name, record))
    return scripts


def stock_record_script(variables, stepper_name):
    """Note that the stock code read a tag of its own, such as an original QIDI one."""
    color = variables.get("color_" + stepper_name)
    color = color[0] if isinstance(color, list) and color else ""
    return "SAVE_VARIABLE VARIABLE=bambu_tag_%s VALUE=\"%r\"" % (
        stepper_name, {"stock": True, "color": str(color), "filament": str(variables.get("filament_" + stepper_name, ""))})


def spool_key(variables, slot):
    """What the count belongs to: the spool when its tag is known, otherwise the slot."""
    record = variables.get("bambu_tag_slot%d" % (slot,))
    if isinstance(record, dict):
        if record.get("tray"):
            return "tray:" + record["tray"]
        if record.get("uid"):
            return "uid:" + record["uid"]
    return "slot:%d" % (slot,)


class SpoolLedger:
    """Grams set by the user and grams printed since, per spool."""

    def __init__(self, path):
        self.path = path
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as f:
                self.spools = json.load(f)
            if not isinstance(self.spools, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            self.spools = {}
        except (OSError, ValueError) as e:
            logging.warning("bambu_rfid: %s ignored: %s", path, e)
            self.spools = {}

    def add(self, key, grams):
        entry = self.spools.setdefault(key, {"set_g": None, "used_g": 0.})
        entry["used_g"] += grams
        self.dirty = True

    def set_remaining(self, key, grams):
        self.spools[key] = {"set_g": float(grams), "used_g": 0.}
        self.dirty = True

    def view(self, key):
        entry = self.spools.get(key) or {"set_g": None, "used_g": 0.}
        remaining = None
        if entry["set_g"] is not None:
            remaining = max(0., entry["set_g"] - entry["used_g"])
        return {"key": key, "remaining": remaining, "used": entry["used_g"]}

    def save(self):
        if not self.dirty:
            return
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.spools, f)
        os.replace(temp, self.path)
        self.dirty = False


class FilamentMeter:
    """Turns the print's total filament into millimeters per slot. The last moment of a print
    still counts, and the total starting again from zero is not taken for a negative use."""

    def __init__(self):
        self.last = None
        self.slot = None

    def feed(self, state, used, slot):
        if slot is not None:
            self.slot = slot
        active = state in ("printing", "paused")
        if self.last is None:
            if active:
                self.last = used
            return None
        delta = used - self.last
        self.last = used if active else None
        if delta <= 0 or self.slot is None:
            return None
        return self.slot, delta


def active_slot(box_status):
    match = re.fullmatch(r"slot(\d+)", str(box_status.get("loaded_slot", "")))
    if match and int(match.group(1)) < BOX_SLOTS:
        return int(match.group(1))
    return None


def slot_density(box_status, slot):
    try:
        kind = box_status["boxes"][0]["filament_info"][slot]["filament_type"]
    except (KeyError, IndexError, TypeError):
        return DEFAULT_DENSITY
    return DENSITY.get(kind, DEFAULT_DENSITY)


class SpoolUsage:
    """One per printer, whichever reader section creates it first."""

    def __init__(self, printer, path):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.ledger = SpoolLedger(path)
        self.meter = FilamentMeter()
        self.next_save = 0.
        printer.register_event_handler("klippy:ready", self._start)
        printer.register_event_handler("klippy:disconnect", self._flush)
        printer.lookup_object("gcode").register_command(
            "BAMBU_RFID_SPOOL_SET", self.cmd_SET, desc="Set the filament left on a spool")

    @classmethod
    def of(cls, printer, path):
        usage = printer.lookup_object("bambu_spool_usage", None)
        if usage is None:
            usage = cls(printer, path)
            printer.add_object("bambu_spool_usage", usage)
        return usage

    def _variables(self):
        saved = self.printer.lookup_object("save_variables", None)
        return saved.allVariables if saved is not None else {}

    def _start(self):
        self.reactor.register_timer(self._tick, self.reactor.monotonic() + USAGE_INTERVAL)

    def _flush(self):
        try:
            self.ledger.save()
        except OSError:
            logging.exception("bambu_rfid: could not save %s", self.ledger.path)

    def _tick(self, eventtime):
        # An error in a reactor timer would shut the printer down.
        try:
            self._count(eventtime)
        except Exception:
            logging.exception("bambu_rfid: filament count failed")
        return eventtime + USAGE_INTERVAL

    def _count(self, eventtime):
        stats = self.printer.lookup_object("print_stats").get_status(eventtime)
        box = self.printer.lookup_object("box_extras", None)
        box_status = box.get_status(eventtime) if box is not None else {}
        part = self.meter.feed(stats["state"], stats["filament_used"], active_slot(box_status))
        if part is not None:
            slot, millimeters = part
            grams = millimeters * FILAMENT_MM2 * slot_density(box_status, slot) / 1000.
            self.ledger.add(spool_key(self._variables(), slot), grams)
        if self.ledger.dirty and (self.meter.last is None or eventtime >= self.next_save):
            self._flush()
            self.next_save = eventtime + USAGE_SAVE_INTERVAL

    def get_status(self, eventtime):
        variables = self._variables()
        return {"slots": [self.ledger.view(spool_key(variables, slot)) for slot in range(BOX_SLOTS)]}

    def cmd_SET(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=BOX_SLOTS - 1)
        grams = gcmd.get_float("GRAMS", minval=0., maxval=MAX_SPOOL_GRAMS)
        self.ledger.set_remaining(spool_key(self._variables(), slot), grams)
        self._flush()
        gcmd.respond_info("Slot %d: %.0f g left" % (slot, grams))


class StockReadProxy:
    """Stands in for box_rfid's fm17550_read_card query command."""

    def __init__(self, real, chip, report, monotonic, on_bambu=None, on_stock=None):
        self.real = real
        self.chip = chip
        self.report = report
        self.monotonic = monotonic
        self.on_bambu = on_bambu
        self.on_stock = on_stock
        self.busy = False
        self.last_uid = None
        self.held_data = None
        self.held_until = 0.

    def __getattr__(self, name):
        return getattr(self.real, name)

    def _read_bambu_codes(self):
        uid, tag = self.chip.read_bambu()
        if tag["qidi_material"] is None:
            logging.info("bambu_rfid: no QIDI material for %s", tag["detail"])
            return None
        if uid != self.last_uid:
            self.last_uid = uid
            self.report("Bambu %s, #%s -> QIDI material %d, color %d" % (
                tag["detail"], tag["color"], tag["qidi_material"], tag["qidi_color"]))
        tag["uid"] = uid.hex().upper()
        if self.on_bambu is not None:
            self.on_bambu(tag)
        return bytes([tag["qidi_material"], tag["qidi_color"], 1]) + bytes(13)

    def _read_custom_codes(self):
        try:
            custom = self.chip.read_custom()
        except RfidError:
            return None
        if custom is None:
            return None
        data, tag = custom
        if self.on_bambu is not None:
            self.on_bambu(tag)
        return data

    def send(self, *args, **kwargs):
        if self.busy:
            return {"status": 0, "data": b""}
        now = self.monotonic()
        if self.held_data is not None and now < self.held_until:
            return {"status": 1, "data": self.held_data}
        self.held_data = None
        try:
            data = self._read_bambu_codes()
        except TagAbsent:
            # Skipping the stock read here keeps the polling fast between passes.
            return {"status": 0, "data": b""}
        except NotBambuTag:
            data = self._read_custom_codes()
        except RfidError:
            data = None
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: read failed, using the stock read")
            data = None
        if data is None:
            result = self.real.send(*args, **kwargs)
            # Only values the stock code accepts count; a tag it cannot read answers with noise.
            raw = result.get("data") or b""
            if self.on_stock is not None and result.get("status") == 1 and len(raw) >= 2 \
                    and 1 <= raw[0] <= 99 and 1 <= raw[1] <= 24:
                self.on_stock()
            return result
        self.held_data = data
        self.held_until = now + ANSWER_HOLD
        return {"status": 1, "data": data}


class BambuRfid:
    def __init__(self, config):
        from . import bus
        self.printer = config.get_printer()
        self.name = config.get_name().split()[1]
        self.reactor = self.printer.get_reactor()
        spi = bus.MCU_SPI_from_config(config, 0, default_speed=1000000,
                                      share_type="spi_cs")
        self.chip = Fm17550(spi, self._pause, self._get_mapping)
        self.proxy = None
        self.stock_timer = None
        self.tag_result = None
        config_file = self.printer.get_start_args().get("config_file", "")
        self.map_path = os.path.join(os.path.dirname(config_file), "bambu_map.cfg")
        SpoolUsage.of(self.printer, os.path.join(os.path.dirname(config_file), USAGE_FILE))
        gcode = self.printer.lookup_object("gcode")
        self.gcode = gcode
        if config.getboolean("hook_stock_read", False):
            self.printer.register_event_handler("klippy:connect", self._hook_stock)
        gcode.register_mux_command("BAMBU_RFID_DIAG", "READER", self.name,
                                   self.cmd_DIAG, desc="Read FM17550 registers")
        gcode.register_mux_command("BAMBU_RFID_READ", "READER", self.name,
                                   self.cmd_READ, desc="Read a Bambu Lab tag")
        gcode.register_mux_command("BAMBU_RFID_TAG_READ", "READER", self.name,
                                   self.cmd_TAG_READ, desc="Read a QIDI-format tag")
        gcode.register_mux_command("BAMBU_RFID_TAG_WRITE", "READER", self.name,
                                   self.cmd_TAG_WRITE, desc="Write a QIDI-format tag")

    def get_status(self, eventtime):
        return {"tag_result": self.tag_result}

    def _hook_stock(self):
        stock = self.printer.lookup_object("box_rfid " + self.name, None)
        real = getattr(stock, "fm17550_read_card", None)
        if real is None:
            logging.warning("bambu_rfid: stock read of %s not found, hook not set", self.name)
            return
        self.proxy = StockReadProxy(real, self.chip, self._report,
                                    self.reactor.monotonic,
                                    lambda tag: self._schedule_exact_color(stock, tag),
                                    lambda: self._schedule_stock_record(stock))
        try:
            stock.fm17550_read_card = self.proxy
        except AttributeError:
            self.proxy = None
            logging.warning("bambu_rfid: cannot wrap stock read of %s", self.name)
            return
        logging.info("bambu_rfid: stock read of %s wrapped", self.name)

    def _schedule_exact_color(self, stock, tag):
        stepper_name = getattr(getattr(stock, "stepper", None), "stepper_name", None)
        if stepper_name is None:
            return

        def apply(eventtime):
            self.reactor.unregister_timer(timer)
            try:
                saved = self.printer.lookup_object("save_variables")
                for script in slot_update_scripts(saved.allVariables, stepper_name, tag):
                    self.gcode.run_script(script)
            except Exception:
                logging.exception("bambu_rfid: could not save the exact color")
            return self.reactor.NEVER
        timer = self.reactor.register_timer(
            apply, self.reactor.monotonic() + EXACT_COLOR_DELAY)

    def _schedule_stock_record(self, stock):
        stepper_name = getattr(getattr(stock, "stepper", None), "stepper_name", None)
        if stepper_name is None or self.stock_timer is not None:
            return

        def apply(eventtime):
            self.reactor.unregister_timer(self.stock_timer)
            self.stock_timer = None
            try:
                saved = self.printer.lookup_object("save_variables")
                self.gcode.run_script(stock_record_script(saved.allVariables, stepper_name))
            except Exception:
                logging.exception("bambu_rfid: could not note the stock read")
            return self.reactor.NEVER
        self.stock_timer = self.reactor.register_timer(
            apply, self.reactor.monotonic() + EXACT_COLOR_DELAY)

    def _report(self, message):
        logging.info("bambu_rfid %s: %s", self.name, message)
        self.gcode.respond_info("%s: %s" % (self.name, message))

    def _get_mapping(self):
        # Read on every tag so an edit in Fluidd applies without a restart.
        try:
            return load_map(self.map_path)
        except ValueError as e:
            self._report("bambu_map.cfg ignored: %s" % (e,))
            return QIDI_MATERIALS, {}

    def _pause(self, delay):
        self.reactor.pause(self.reactor.monotonic() + delay)

    def _check_idle(self, gcmd):
        stats = self.printer.lookup_object("print_stats", None)
        if stats is not None:
            state = stats.get_status(self.reactor.monotonic())["state"]
            if state in ("printing", "paused"):
                raise gcmd.error("bambu_rfid: refused while a print is active")
        try:
            self._check_stock_idle()
        except RfidError as e:
            raise gcmd.error("bambu_rfid: %s" % (e,))

    def _check_stock_idle(self):
        # Two users of the chip at once would corrupt both exchanges.
        stock = self.printer.lookup_object("box_rfid " + self.name, None)
        if getattr(stock, "read_rfid_timer", None) is not None:
            raise RfidError("the stock RFID read is running")

    def cmd_DIAG(self, gcmd):
        self._check_idle(gcmd)
        values = ["%s=0x%02X" % (name, self.chip.read_reg(reg))
                  for name, reg in DIAG_REGS]
        gcmd.respond_info("%s: %s" % (self.name, " ".join(values)))

    def cmd_READ(self, gcmd):
        self._check_idle(gcmd)
        # Keeps the wrapped stock read off the chip while this command uses it.
        if self.proxy is not None:
            self.proxy.busy = True
        try:
            uid, tag = self.chip.read_bambu(
                gcmd.get_float("TIMEOUT", 30., minval=0., maxval=60.),
                self.reactor.monotonic, self._check_stock_idle)
        except RfidError as e:
            raise gcmd.error("bambu_rfid %s: %s" % (self.name, e))
        finally:
            if self.proxy is not None:
                self.proxy.busy = False
        gcmd.respond_info(
            "%s: UID %s, %s, #%s, %d g, %d-%d C -> QIDI material %s, color %d"
            % (self.name, uid.hex().upper(), tag["detail"], tag["color"],
               tag["weight"], tag["min_temp"], tag["max_temp"],
               tag["qidi_material"], tag["qidi_color"]))

    def _tag_command(self, gcmd, action):
        """Run a tag operation; what happened to the tag goes to tag_result for the panel."""
        self._check_idle(gcmd)
        request = gcmd.get_int("ID", 0)
        # Keeps the wrapped stock read off the chip while this command uses it.
        if self.proxy is not None:
            self.proxy.busy = True
        try:
            result = dict(action(), ok=True)
        except TagProblem as e:
            result = {"ok": False, "error": e.code}
            if e.uid is not None:
                result["uid"] = e.uid.hex().upper()
        except TagAbsent:
            result = {"ok": False, "error": "absent"}
        except SeveralTags:
            result = {"ok": False, "error": "several"}
        except RfidError as e:
            logging.info("bambu_rfid %s: tag operation failed: %s", self.name, e)
            result = {"ok": False, "error": "failed"}
        finally:
            if self.proxy is not None:
                self.proxy.busy = False
        result["id"] = request
        self.tag_result = result
        gcmd.respond_info("%s: %s" % (self.name, "UID " + result["uid"] if result["ok"] else result["error"]))

    def cmd_TAG_READ(self, gcmd):
        timeout = gcmd.get_float("TIMEOUT", TAG_READ_TIMEOUT, minval=0., maxval=60.)
        try:
            exclude = [bytes.fromhex(uid) for uid in gcmd.get("EXCLUDE", "").split(",") if uid]
        except ValueError:
            raise gcmd.error("bambu_rfid: EXCLUDE must be hex UIDs")
        self._tag_command(gcmd, lambda: self.chip.read_tag(
            timeout, self.reactor.monotonic, self._check_stock_idle, exclude))

    def cmd_TAG_WRITE(self, gcmd):
        timeout = gcmd.get_float("TIMEOUT", TAG_WRITE_TIMEOUT, minval=0., maxval=60.)
        try:
            uid = bytes.fromhex(gcmd.get("UID"))
            blocks = {4: bytes.fromhex(gcmd.get("BLOCK4"))}
            if gcmd.get("BLOCK5", None) is not None:
                blocks[5] = bytes.fromhex(gcmd.get("BLOCK5"))
        except ValueError:
            raise gcmd.error("bambu_rfid: UID and blocks must be hex")
        if len(uid) != 4 or any(len(data) != 16 for data in blocks.values()):
            raise gcmd.error("bambu_rfid: UID must be 4 bytes and a block 16")
        self._tag_command(gcmd, lambda: self.chip.write_tag(
            uid, blocks, timeout, self.reactor.monotonic, self._check_stock_idle))


def load_config_prefix(config):
    return BambuRfid(config)
