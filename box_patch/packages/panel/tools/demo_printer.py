#!/usr/bin/env python3
"""The panel on this Mac with a pretend printer, to try everything before an install:
python3 -B tools/demo_printer.py (from the panel package folder), then http://127.0.0.1:18770

Slots: 1A a Bambu spool, 1B our tag with a maker and a name, 1C a factory QIDI tag, 1D a tag
that answered but could not be read. Each Box reader has two blank tags at its antenna, the
two sides of one spool, so a spool's second tag can be written. Nothing reaches a printer.
"""

import ast
import pathlib
import re
import sys
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "printer"))
import qidi_tag_tool as q  # noqa: E402

BAMBU = {"uid": "55365C10", "detail": "PLA Matte", "tag_color": "000000", "color": "000000", "weight": 1000,
         "diameter": 1.75, "min_temp": 190, "max_temp": 230, "dry_temp": 55, "dry_hours": 8,
         "brand": "Bambu Lab", "filament": "QFA01", "tray": "8876F63EE47049A7A76DBD2077E0FDDA"}
OURS = {"uid": "E83E2288", "detail": "PETG+ синий", "brand": "eSun", "tag_color": "2850FF", "color": "2850FF",
        "filament": "QFG00", "tray": "0123456789ABCDEF"}
FACTORY = {"uid": "2B752AF4", "detail": "QIDI", "brand": "QIDI", "tag_color": "1353AB", "color": "1353AB",
           "filament": "QFB54", "raw": "0B0E01"}
UNREAD = {"uid": "5B692AF4", "unread": "ключ метки не подошёл", "detail": "Метка не прочитана", "sak": 8}


class DemoPrinter(q.Printer):
    def __init__(self):
        q.Printer.__init__(self, "demo", fetch=self.answer)
        self.variables = {}
        for index, (tag, filament, color) in enumerate(((BAMBU, "QFA01", "000000"), (OURS, "QFG00", "2850FF"),
                                                         (FACTORY, "QFB54", "1353AB"), (UNREAD, "QFA01", "FAFAFA"))):
            self.variables["bambu_tag_slot%d" % index] = dict(tag)
            self.variables["filament_slot%d" % index] = filament
            self.variables["color_slot%d" % index] = [color]
        self.remaining = [246.7, 812.0, 1000.0, 35.0]
        self.files = {}
        self.results = {}
        # Two blank tags per reader: [uid, key, blocks]
        self.antennas = {"card_reader_1": [self._blank("04A1B2C3"), self._blank("04D5E6F7")],
                         "card_reader_2": [self._blank("04112233"), self._blank("04445566")]}
        self.upload = self.files.__setitem__

    @staticmethod
    def _blank(uid):
        return [uid, "default", {str(block): "00" * 16 for block in (4, 5, 6, 8, 9, 10)}]

    def _map_text(self):
        return self.files.get(q.MAP_FILE) or (HERE.parent.parent / "bambu" / "printer" / "bambu_map.cfg").read_text(
            encoding="utf-8")

    def tag_rules(self):
        text = self.files.get(q.TAG_RULES_FILE)
        return q.json.loads(text) if text else {}

    def answer(self, path, post=False):
        if post:
            return self._script(urllib.parse.parse_qs(path.split("?", 1)[1])["script"][0])
        if path.startswith("/server/"):
            return {"result": []}
        query = urllib.parse.unquote(path.split("?", 1)[1]) if "?" in path else ""
        status = {}
        for part in query.split("&"):
            name = part.split("=")[0]
            if name == "save_variables":
                status[name] = {"variables": self.variables}
            elif name == "print_stats":
                status[name] = {"state": "standby", "filename": "", "filament_used": 0.}
            elif name == "bambu_spool_usage":
                status[name] = {"slots": [{"key": "uid:%d" % i, "remaining": r, "used": 0.}
                                          for i, r in enumerate(self.remaining)]}
            elif name == "box_extras":
                status[name] = {"loaded_slot": "slot16"}
            elif name == "filament_switch_sensor filament_switch_sensor":
                status[name] = {"filament_detected": False}
            elif name.startswith("bambu_rfid "):
                status[name] = {"tag_result": self.results.get(name.split()[1])}
        return {"result": {"status": status}}

    def _script(self, script):
        words = script.split()
        if words[0] == "SAVE_VARIABLE":
            match = re.fullmatch(r"SAVE_VARIABLE VARIABLE=(\w+) VALUE=\"(.*)\"", script, re.S)
            self.variables[match.group(1)] = ast.literal_eval(match.group(2))
            return {"result": "ok"}
        fields = dict(word.split("=", 1) for word in words[1:] if "=" in word)
        reader = fields.get("READER")
        if words[0] in ("BAMBU_RFID_TAG_READ", "BAMBU_RFID_TAG_WRITE"):
            self.results[reader] = dict(self._tag(fields, reader), id=int(fields["ID"]))
        elif words[0] == "BAMBU_RFID_SLOT_READ":
            index = int(fields["SLOT"])
            tag = self.variables.get("bambu_tag_slot%d" % index) or {}
            if tag.get("unread"):
                self.results[reader] = {"id": int(fields["ID"]), "ok": False, "error": "unknown_tag", "uid": tag["uid"]}
            else:
                self.results[reader] = dict(tag, id=int(fields["ID"]), ok=True, color=tag.get("color"),
                                            profile=tag.get("filament"))
        return {"result": "ok"}

    def _tag(self, fields, reader):
        exclude = fields.get("EXCLUDE", "").split(",")
        tags = [tag for tag in self.antennas[reader] if tag[0] not in exclude]
        if not tags:
            return {"ok": False, "error": "absent"}
        if "UID" in fields and fields["UID"] != tags[0][0]:
            return {"ok": False, "error": "other_tag"}
        uid, key, blocks = tags[0]
        for block in blocks:
            if "BLOCK" + block in fields:
                blocks[block] = fields["BLOCK" + block]
        named = bytes.fromhex(blocks["6"])[12] == q.NAME_FLAG
        shown = {b: v for b, v in blocks.items() if b in ("4", "5", "6") or named}
        return {"ok": True, "uid": uid, "key": key, "blocks": shown}

    def read_slot(self, index, skip_neighbour=False):
        """The motor read, at once: no Box to wait for."""
        self.reading[index] = True
        self.read_errors.pop(index, None)
        self._read_with_motor(index, skip_neighbour)


def main():
    q.apply_features(None)
    q.Handler.app = q.App(printer=DemoPrinter(), my_colors=q.MyColors(pathlib.Path("/tmp/qidi_demo_colors.json")),
                          my_materials=q.MyMaterials(pathlib.Path("/tmp/qidi_demo_materials.json")),
                           my_names=q.MyNames(pathlib.Path("/tmp/qidi_demo_names.json")))
    print("Демо панели: http://127.0.0.1:%d" % (q.PORT,), flush=True)
    q.ThreadingHTTPServer(("127.0.0.1", q.PORT), q.Handler).serve_forever()


if __name__ == "__main__":
    main()
