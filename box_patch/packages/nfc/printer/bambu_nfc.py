# Writes our own tags through the QIDI Box readers: the NFC tags package.
#
# Install as klippy/extras/bambu_nfc.py next to bambu_rfid.py. Config:
#   [bambu_nfc]
# Reading such a tag at insertion stays with bambu_rfid (option custom_tags).
#
# G-code, for each bambu_rfid reader:
#   BAMBU_RFID_TAG_READ READER=card_reader_1 ID=1 [TIMEOUT=15] [EXCLUDE=7081205A,...]
#   BAMBU_RFID_TAG_WRITE READER=card_reader_1 ID=2 UID=7081205A BLOCK4=<hex> [BLOCK5=<hex>] [BLOCK6=<hex>]
#       [EXCLUDE=...]
#       read or write a QIDI-format tag; the result is in the reader's status as tag_result.
#       A write goes only to the tag named by its UID and only to blocks 4, 5 and 6:
#       block 5 holds the exact color and profile, block 6 the spool number (QXSP + 8 bytes).
try:
    from . import bambu_rfid
except ImportError:
    import bambu_rfid

MIFARE_WRITE = 0xA0
MIFARE_ACK = 0x0A
# Never the sector trailer, block 7: a bad write there locks the sector for good.
WRITABLE_BLOCKS = (4, 5, 6) + bambu_rfid.NAME_BLOCKS
TAG_READ_TIMEOUT = 15.
TAG_WRITE_TIMEOUT = 5.


def write_block(chip, block, data):
    """MIFARE write in its two steps, then read back. Needs an authenticated sector."""
    if block not in WRITABLE_BLOCKS or len(data) != 16:
        raise ValueError("refused write to block %r" % (block,))
    for frame in ([MIFARE_WRITE, block], list(data)):
        answer = chip.transceive(frame, tx_crc=True)
        if len(answer) != 1:
            raise bambu_rfid.RfidError("no write acknowledgement")
        if answer[0] & 0x0F != MIFARE_ACK:
            raise bambu_rfid.TagProblem("denied")
    if chip.read_block(block) != bytes(data):
        raise bambu_rfid.RfidError("read back differs from what was written")


def write_tag(chip, uid, blocks, timeout, monotonic, check_abort=None, exclude=()):
    """Write only to the tag `uid`: another tag may have come to the antenna since it was read.
    A tag in `exclude`, such as the other tag of the same spool, counts as no tag."""
    def action():
        found, key = chip._open_tag(exclude)
        if found != uid:
            raise bambu_rfid.TagProblem("other_tag")
        for block, data in sorted(blocks.items()):
            # The maker and name are in sector 2, opened with the default key of our own tags.
            if block == bambu_rfid.NAME_BLOCKS[0] and not chip.nested_auth(block, bambu_rfid.DEFAULT_KEY, found):
                raise bambu_rfid.TagProblem("denied")
            write_block(chip, block, data)
        return {"uid": found.hex().upper(), "key": key}
    return chip._retrying(action, timeout, monotonic, check_abort)


def _exclude(gcmd):
    try:
        return [bytes.fromhex(uid) for uid in gcmd.get("EXCLUDE", "").split(",") if uid]
    except ValueError:
        raise gcmd.error("bambu_rfid: EXCLUDE must be hex UIDs")


def cmd_TAG_READ(reader, gcmd):
    timeout = gcmd.get_float("TIMEOUT", TAG_READ_TIMEOUT, minval=0., maxval=60.)
    exclude = _exclude(gcmd)
    reader._tag_command(gcmd, lambda: reader.chip.read_tag(
        timeout, reader.reactor.monotonic, reader._check_stock_idle, exclude))


def cmd_TAG_WRITE(reader, gcmd):
    timeout = gcmd.get_float("TIMEOUT", TAG_WRITE_TIMEOUT, minval=0., maxval=60.)
    exclude = _exclude(gcmd)
    try:
        uid = bytes.fromhex(gcmd.get("UID"))
        blocks = {4: bytes.fromhex(gcmd.get("BLOCK4"))}
        for block in (5, 6) + bambu_rfid.NAME_BLOCKS:
            if gcmd.get("BLOCK%d" % (block,), None) is not None:
                blocks[block] = bytes.fromhex(gcmd.get("BLOCK%d" % (block,)))
    except ValueError:
        raise gcmd.error("bambu_rfid: UID and blocks must be hex")
    if len(uid) != 4 or any(len(data) != 16 for data in blocks.values()):
        raise gcmd.error("bambu_rfid: UID must be 4 bytes and a block 16")
    reader._tag_command(gcmd, lambda: write_tag(
        reader.chip, uid, blocks, timeout, reader.reactor.monotonic, reader._check_stock_idle, exclude))


class BambuNfc:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self._register)

    def _register(self):
        gcode = self.printer.lookup_object("gcode")
        for _, reader in self.printer.lookup_objects("bambu_rfid"):
            gcode.register_mux_command("BAMBU_RFID_TAG_READ", "READER", reader.name,
                                       lambda gcmd, r=reader: cmd_TAG_READ(r, gcmd), desc="Read a QIDI-format tag")
            gcode.register_mux_command("BAMBU_RFID_TAG_WRITE", "READER", reader.name,
                                       lambda gcmd, r=reader: cmd_TAG_WRITE(r, gcmd), desc="Write a QIDI-format tag")


def load_config(config):
    return BambuNfc(config)
