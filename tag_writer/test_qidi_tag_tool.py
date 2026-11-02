"""Tests against a simulated ACR122U + MIFARE Classic tag. Needs no hardware."""
import pathlib
import tempfile
import threading
import unittest

import qidi_tag_tool as q

CLASSIC_ATR = bytes.fromhex("3B8F8001804F0CA000000306030001000000006A")
ULTRALIGHT_ATR = bytes.fromhex("3B8F8001804F0CA0000003060300030000000068")
OK = b"\x90\x00"
FAIL = b"\x63\x00"


class FakeTransport:
    def __init__(self, key, block, atr=CLASSIC_ATR, write_ok=True):
        self.tag_key, self.block, self.atr_value = key, bytes(block), atr
        self.write_ok = write_ok
        self.loaded = None
        self.authed = False
        self.halted = False
        self.connected = False
        self.writes = []
        self.color_block = bytes(16)

    def reader_name(self):
        return "Fake ACR122U"

    def connect(self):
        self.connected, self.authed, self.halted = True, False, False

    def disconnect(self, reset=False):
        self.connected = False

    def atr(self):
        return self.atr_value

    def transmit(self, apdu):
        h = apdu.hex().upper()
        if h.startswith("FFCA"):
            return bytes.fromhex("AABBCCDD") + OK
        if self.halted:
            return FAIL
        if h.startswith("FF82"):
            self.loaded = h[10:]
            return OK
        if h.startswith("FF86"):
            # Exact ACR122U layout: FF 86 00 00 05 | 01 00 block keytype keynum
            if len(apdu) != 10 or apdu[4] != 5 or apdu[5:7] != b"\x01\x00" \
                    or apdu[7] != 4 or apdu[8] != 0x60 or apdu[9] != 0:
                return FAIL
            if self.loaded == self.tag_key:
                self.authed = True
                return OK
            self.halted = True  # real tags stop responding after a failed auth
            return FAIL
        if h.startswith("FFB0"):
            if not self.authed:
                return FAIL
            data = {4: self.block, 5: self.color_block}.get(apdu[3], bytes(16))
            return data + OK
        if h.startswith("FFD6"):
            self.writes.append(apdu[3])
            if not self.authed or not self.write_ok:
                return FAIL
            if apdu[3] == 4:
                self.block = apdu[5:]
            else:
                self.color_block = apdu[5:]
            return OK
        return FAIL


QIDI = "D3F7D3F7D3F7"
BLANK = "FFFFFFFFFFFF"
SOURCE = bytes([39, 18, 1]) + bytes(9) + b"QIDI"


class ExactColorTests(unittest.TestCase):
    def app(self, target):
        return q.App(transport=target)

    def write(self, target, color):
        return self.app(target).write({"material": 2, "color": color, "vendor": 1, "tail": "00" * 13})

    def test_custom_color_goes_to_color_block_and_reads_back(self):
        target = FakeTransport(BLANK, bytes(16))
        result = self.write(target, "D3B7A7")
        self.assertEqual(target.block[:3], bytes([2, result["palette_code"], 1]))
        self.assertEqual(target.writes, [4, 5])
        self.assertEqual(q.Tag(target).read()["color"], "D3B7A7")
        self.assertTrue(result["exact_saved"])

    def test_profile_is_written_and_read_back(self):
        target = FakeTransport(BLANK, bytes(16))
        self.app(target).write({"material": 39, "color": "FF362D", "vendor": 1,
                                "tail": "00" * 13, "profile": "QFG00"})
        data = q.Tag(target).read()
        self.assertEqual((data["profile"], data["color"]), ("QFG00", "FF362D"))
        self.assertEqual(target.block[0], 39)

    def test_bad_profile_rejected(self):
        with self.assertRaises(q.TagError):
            self.app(FakeTransport(BLANK, bytes(16))).write(
                {"material": 7, "color": "FF362D", "vendor": 1, "tail": "00" * 13, "profile": "x"})

    def test_palette_color_writes_only_block_4(self):
        target = FakeTransport(BLANK, bytes(16))
        result = self.write(target, "FF362D")
        self.assertEqual(target.writes, [4])
        self.assertEqual(result["palette_code"], 18)
        self.assertFalse(result["exact_saved"])

    def test_palette_color_clears_our_old_exact_color(self):
        target = FakeTransport(BLANK, bytes(16))
        self.write(target, "D3B7A7")
        self.write(target, "FF362D")
        self.assertEqual(target.color_block, bytes(16))
        self.assertIsNone(q.Tag(target).read()["color"])

    def test_foreign_data_in_color_block_is_kept(self):
        target = FakeTransport(BLANK, bytes(16))
        target.color_block = b"\x01" * 16
        result = self.write(target, "D3B7A7")
        self.assertEqual(target.color_block, b"\x01" * 16)
        self.assertEqual(target.writes, [4])
        self.assertTrue(result["exact_skipped"])

    def test_invalid_color_rejected(self):
        with self.assertRaises(q.TagError):
            self.write(FakeTransport(BLANK, bytes(16)), "red")


class MyColorsTests(unittest.TestCase):
    def make(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return q.MyColors(pathlib.Path(d.name) / "c.json")

    def test_add_keeps_after_reload_without_duplicates(self):
        colors = self.make()
        colors.change(add="D3B7A7")
        colors.change(add="D3B7A7")
        self.assertEqual(q.MyColors(colors.path).load(), ["D3B7A7"])

    def test_remove(self):
        colors = self.make()
        colors.change(add="D3B7A7")
        colors.change(add="112233")
        self.assertEqual(colors.change(remove="D3B7A7"), ["112233"])

    def test_editing_keeps_position(self):
        colors = self.make()
        for value in ("111111", "222222", "333333"):
            colors.change(add=value)
        self.assertEqual(colors.change(add="ABCDEF", remove="222222"), ["111111", "ABCDEF", "333333"])

    def test_bad_color_rejected(self):
        with self.assertRaises(q.TagError):
            self.make().change(add="red")

    def test_broken_file_gives_empty_palette(self):
        colors = self.make()
        colors.path.write_text("not json")
        self.assertEqual(colors.load(), [])

    def test_limit(self):
        colors = self.make()
        colors.path.write_text(q.json.dumps(["%06X" % i for i in range(q.MY_COLORS_LIMIT)]))
        with self.assertRaises(q.TagError):
            colors.change(add="ABCDEF")


class MyMaterialsTests(unittest.TestCase):
    def make(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return q.MyMaterials(pathlib.Path(d.name) / "m.json")

    def test_types_and_ids_saved_separately(self):
        items = self.make()
        items.change(type_on="PLA")
        items.change(add="QFA01")
        items.change(add="QFA01")
        data = q.MyMaterials(items.path).load()
        self.assertEqual(data, {"ids": ["QFA01"], "types": ["PLA"]})

    def test_type_off_keeps_chosen_materials(self):
        items = self.make()
        items.change(type_on="PLA")
        items.change(add="QFA01")
        data = items.change(type_off="PLA")
        self.assertEqual(data, {"ids": ["QFA01"], "types": []})

    def test_bad_values_rejected(self):
        with self.assertRaises(q.TagError):
            self.make().change(add="bad")
        with self.assertRaises(q.TagError):
            self.make().change(type_on="a b'c")

    def test_old_or_broken_file_is_empty(self):
        items = self.make()
        for text in ('["QFA01"]', "not json"):
            items.path.write_text(text)
            self.assertEqual(items.load(), {"ids": [], "types": []})


class GroupTests(unittest.TestCase):
    def test_group_rules_cover_all_profiles(self):
        self.assertEqual([m["name"] for m in q.load_materials() if q.group_of(m["name"]) == "Другое"], [])
        self.assertEqual(q.group_of("QIDI PETG-CF"), "PETG")
        self.assertEqual(q.group_of("QIDI PET-CF"), "PET")
        self.assertEqual(q.group_of("QIDI UltraPA"), "PA")
        self.assertEqual(q.group_of("QIDI WOOD Rapido"), "PLA")


class PrinterModeTests(unittest.TestCase):
    def test_host_pattern_accepts_only_this_port(self):
        match = q.PRINTER_MODE_HOST.fullmatch
        self.assertTrue(match("192.168.1.197:%d" % q.PORT))
        self.assertTrue(match("linaro-alip:%d" % q.PORT))
        self.assertFalse(match("192.168.1.197"))
        self.assertFalse(match("evil.test:80"))

    def test_no_reader_on_printer(self):
        q.printer_mode = True
        try:
            self.assertIsNone(q.App().status()["reader"])
        finally:
            q.printer_mode = False


class FakeBoxPrinter(q.Printer):
    """A printer whose module answers tag commands from a dictionary of blocks."""

    def __init__(self, uid="7081205A", block4=bytes(16), block5=bytes(16), state="standby"):
        q.Printer.__init__(self, "printer", fetch=self.answer)
        self.uid, self.state = uid, state
        self.blocks = {"4": block4.hex().upper(), "5": block5.hex().upper(), "6": "00" * 16}
        self.error = None
        self.scripts = []
        self.result = None

    def answer(self, path, post=False):
        if path.startswith("/printer/objects/query?save_variables"):
            return {"result": {"status": {"save_variables": {"variables": {}},
                                          "print_stats": {"state": self.state}}}}
        if path.startswith("/printer/objects/query?"):
            name = q.urllib.parse.unquote(path.split("?", 1)[1])
            return {"result": {"status": {name: {"tag_result": self.result}}}}
        script = q.urllib.parse.parse_qs(path.split("?", 1)[1])["script"][0]
        self.scripts.append(script)
        fields = dict(part.split("=") for part in script.split()[1:])
        if self.error:
            self.result = {"id": int(fields["ID"]), "ok": False, "error": self.error}
        else:
            self.result = {"id": int(fields["ID"]), "ok": True, "uid": self.uid, "key": "default",
                           "blocks": self.blocks}
            for block in ("4", "5"):
                if "BLOCK" + block in fields:
                    self.blocks[block] = fields["BLOCK" + block]
        return {"result": "ok"}


class BoxTagTests(unittest.TestCase):
    BODY = {"source": "box2", "uid": "7081205A", "material": 39, "vendor": 1, "tail": "00" * 13}

    def app(self, printer):
        my = pathlib.Path(tempfile.mkdtemp())
        return q.App(printer=printer, my_colors=q.MyColors(my / "c.json"), my_materials=q.MyMaterials(my / "m.json"))

    def test_read_goes_to_the_chosen_reader(self):
        printer = FakeBoxPrinter(block4=bytes([39, 6, 1]) + bytes(13))
        result = self.app(printer).read({"source": "box2"})
        self.assertEqual(result["block"][:6], "270601")
        self.assertIn("READER=card_reader_2", printer.scripts[0])

    def test_write_keeps_exact_color(self):
        printer = FakeBoxPrinter()
        result = self.app(printer).write(dict(self.BODY, color="123456", profile="QFL30"))
        self.assertTrue(result["exact_saved"])
        self.assertTrue(printer.blocks["5"].startswith(q.CUSTOM_COLOR_MAGIC.hex().upper() + "123456"))

    def test_write_answers_with_what_the_tag_holds(self):
        printer = FakeBoxPrinter()
        result = self.app(printer).write(dict(self.BODY, color="123456", profile="QFL30"))
        self.assertEqual(result["readback"]["color"], "123456")
        self.assertEqual(result["readback"]["profile"], "QFL30")
        self.assertEqual(result["readback"]["block"][:2], "27")

    def test_failed_read_back_is_reported(self):
        printer = FakeBoxPrinter()
        original = printer.answer
        def answer(path, post=False):
            data = original(path, post)
            if "WRITE" in printer.scripts[-1:][0] if printer.scripts else False:
                printer.error = "absent"
            return data
        printer.fetch = answer
        result = self.app(printer).write(dict(self.BODY, color="FAFAFA"))
        self.assertNotIn("readback", result)
        self.assertIn("не найдена", result["readback_error"])

    def test_foreign_color_block_is_left_alone(self):
        printer = FakeBoxPrinter(block5=bytes([9]) * 16)
        result = self.app(printer).write(dict(self.BODY, color="123456"))
        self.assertTrue(result["exact_skipped"])
        self.assertEqual(printer.blocks["5"], "09" * 16)

    def test_write_needs_the_uid_that_was_read(self):
        printer = FakeBoxPrinter(uid="AAAAAAAA")
        with self.assertRaisesRegex(q.TagError, "другая метка"):
            self.app(printer).write(dict(self.BODY, color="FAFAFA"))
        self.assertFalse([s for s in printer.scripts if "WRITE" in s])
        with self.assertRaisesRegex(q.TagError, "прочитай"):
            self.app(printer).write(dict(self.BODY, color="FAFAFA", uid="1 BLOCK4=00"))

    def test_reads_at_the_same_time_do_not_mix_up_results(self):
        printer = FakeBoxPrinter()
        original = printer.answer
        def answer(path, post=False):
            # Another slot's read starts while this one is on its way.
            if post:
                next(printer.tag_requests)
            return original(path, post)
        printer.fetch = answer
        self.assertEqual(printer.tag_command("card_reader_1", ("BAMBU_RFID_TAG_READ", ""))["uid"], "7081205A")

    def test_module_error_gets_a_readable_message(self):
        printer = FakeBoxPrinter()
        printer.error = "bambu"
        with self.assertRaisesRegex(q.TagError, "Bambu"):
            self.app(printer).read({"source": "box1"})

    def test_search_step_passes_excluded_tags(self):
        printer = FakeBoxPrinter()
        result = self.app(printer).find({"source": "box1", "exclude": ["AAAAAAAA"]})
        self.assertEqual(result["status"], "found")
        self.assertIn("TIMEOUT=2 EXCLUDE=AAAAAAAA", printer.scripts[0])

    def test_search_goes_on_while_nothing_is_there(self):
        printer = FakeBoxPrinter()
        printer.error = "absent"
        self.assertEqual(self.app(printer).find({"source": "box1"})["status"], "searching")

    def test_search_reports_a_tag_that_cannot_be_written(self):
        printer = FakeBoxPrinter()
        printer.error = "bambu"
        self.assertEqual(self.app(printer).find({"source": "box1"})["status"], "unusable")

    def test_search_rejects_bad_uid(self):
        with self.assertRaises(ValueError):
            self.app(FakeBoxPrinter()).find({"source": "box1", "exclude": ["1 BLOCK4=00"]})

    def test_refused_during_a_print(self):
        printer = FakeBoxPrinter(state="printing")
        with self.assertRaisesRegex(q.PrinterError, "печатает"):
            self.app(printer).read({"source": "box1"})
        self.assertEqual(printer.scripts, [])

    def test_old_module_is_reported(self):
        printer = FakeBoxPrinter()
        printer.fetch = lambda path, post=False: {"result": {"status": {
            "save_variables": {"variables": {}}, "print_stats": {"state": "standby"}}}}
        with self.assertRaisesRegex(q.PrinterError, "Повтори"):
            self.app(printer).read({"source": "box1"})


class SampleLinkTests(unittest.TestCase):
    def test_project_is_served_only_by_its_token(self):
        app = q.App()
        app.samples = lambda body: b"PK-data"
        path = app.sample_link({})["path"]
        token = q.SAMPLE_LINK.fullmatch(path).group(1)
        self.assertEqual(app.sample_file_data(token), b"PK-data")
        self.assertIsNone(app.sample_file_data("0" * 32))

    def test_new_project_replaces_the_old_one(self):
        app = q.App()
        app.samples = lambda body: b"one"
        first = q.SAMPLE_LINK.fullmatch(app.sample_link({})["path"]).group(1)
        app.samples = lambda body: b"two"
        app.sample_link({})
        self.assertIsNone(app.sample_file_data(first))


class LoadedSlotTests(unittest.TestCase):
    def printer(self, slot, detected):
        scripts = []
        def fetch(path, post=False):
            if post:
                scripts.append(path)
                return {"result": "ok"}
            if "box_extras" in path:
                return {"result": {"status": {"box_extras": {"loaded_slot": slot},
                                              "filament_switch_sensor filament_switch_sensor": {"filament_detected": detected}}}}
            return {"result": {"status": {"save_variables": {"variables": {}}, "print_stats": {"state": "standby"}}}}
        printer = q.Printer("printer", fetch=fetch)
        printer.scripts = scripts
        return printer

    def test_loaded_slot_itself_is_refused(self):
        printer = self.printer("slot1", True)
        with self.assertRaisesRegex(q.PrinterError, "1B"):
            printer.read_slot(1)
        self.assertEqual(printer.scripts, [])

    def motor_printer(self, answers, neighbour=None):
        """A printer with slot1 loaded whose module gives `answers` to the tag reads."""
        scripts = []
        answers = iter(answers)
        result = {}

        def fetch(path, post=False):
            if "box_extras" in path:
                return {"result": {"status": {"box_extras": {"loaded_slot": "slot1"},
                                              "filament_switch_sensor filament_switch_sensor": {"filament_detected": True}}}}
            if path.startswith("/printer/objects/query?save_variables"):
                return {"result": {"status": {"print_stats": {"state": "standby"},
                                              "save_variables": {"variables": {"bambu_tag_slot1": neighbour}}}}}
            if path.startswith("/printer/objects/query?"):
                name = q.urllib.parse.unquote(path.split("?", 1)[1])
                return {"result": {"status": {name: {"tag_result": result.get("now")}}}}
            script = q.urllib.parse.parse_qs(path.split("?", 1)[1])["script"][0]
            scripts.append(script)
            if script.startswith("BAMBU_RFID_SLOT_READ"):
                request = int(dict(p.split("=") for p in script.split()[1:])["ID"])
                result["now"] = dict(next(answers), id=request)
            return {"result": "ok"}
        printer = q.Printer("printer", fetch=fetch)
        printer.scripts = scripts
        return printer

    def test_other_slot_is_read_by_module_and_saved(self):
        tag = {"ok": True, "uid": "AABBCCDD", "material": 2, "color": "112233", "tag_color": "112233",
               "detail": "PLA Matte", "profile": None, "brand": "Bambu Lab", "weight": 1000, "tray": None}
        printer = self.motor_printer([tag],
                                    neighbour={"uid": "11223344", "tray": "ABCD"})
        printer.read_slot(0)
        printer.read_thread.join(10)
        self.assertEqual(printer.read_errors, {})
        self.assertFalse(printer.reading[0])
        self.assertNotIn("READ_RFID", " ".join(printer.scripts))
        self.assertIn("SLOT=0", printer.scripts[0])
        self.assertIn("EXCLUDE=11223344", printer.scripts[0])
        self.assertIn("TRAYS=ABCD", printer.scripts[0])
        self.assertIn("VARIABLE=filament_slot0 VALUE=\"'QFA01'\"", printer.scripts[1])
        self.assertIn("VARIABLE=color_slot0 VALUE=\"['112233']\"", printer.scripts[2])
        self.assertIn("'uid': 'AABBCCDD'", printer.scripts[3])
        self.assertIn("'filament': 'QFA01'", printer.scripts[3])

    def test_tag_not_found_is_reported_once(self):
        printer = self.motor_printer([{"ok": False, "error": "absent"}])
        printer.read_slot(0)
        printer.read_thread.join(10)
        self.assertIn("Метка не найдена", printer.read_errors[0])
        self.assertEqual(len(printer.scripts), 1)

    def test_failed_motor_read_keeps_the_slot(self):
        printer = self.motor_printer([{"ok": False, "error": "several"}])
        printer.read_slot(0)
        printer.read_thread.join(10)
        self.assertIn("несколько", printer.read_errors[0])
        self.assertEqual(len(printer.scripts), 1)

    def test_material_without_a_profile_is_reported(self):
        tag = {"ok": True, "uid": "AABBCCDD", "material": 99, "color": "112233", "detail": "Odd"}
        printer = self.motor_printer([tag])
        printer.read_slot(0)
        printer.read_thread.join(10)
        self.assertIn("Odd", printer.read_errors[0])
        self.assertEqual(len(printer.scripts), 1)

    def test_read_runs_once_filament_is_out(self):
        for slot, detected in (("slot1", False), ("", True)):
            printer = self.printer(slot, detected)
            printer.read_slot(0)
            printer.read_thread.join(10)
            self.assertEqual(len(printer.scripts), 2)


class SpoolTests(unittest.TestCase):
    def printer(self, usage=None):
        sent = []
        def fetch(path, post=False):
            if post:
                sent.append(q.urllib.parse.parse_qs(path.split("?", 1)[1])["script"][0])
                return {"result": "ok"}
            status = {"save_variables": {"variables": {}}, "print_stats": {"state": "standby"}}
            if usage:
                status["bambu_spool_usage"] = {"slots": usage}
            return {"result": {"status": status}}
        printer = q.Printer("printer", fetch=fetch)
        printer.sent = sent
        return printer

    def test_slots_carry_the_remainder(self):
        usage = [{"key": "uid:A", "remaining": 640., "used": 10.}] * 4
        self.assertEqual(self.printer(usage).slots()["slots"][1]["spool"]["remaining"], 640.)

    def test_old_module_has_no_remainder(self):
        self.assertIsNone(self.printer().slots()["slots"][0]["spool"])

    def test_set_sends_the_command(self):
        printer = self.printer()
        files = {}
        printer.upload = files.__setitem__
        printer.set_spool(2, 750)
        self.assertEqual(files, {"spool_set_2.txt": "750\n"})
        self.assertEqual(printer.sent, [])

    def test_bad_values_are_refused(self):
        printer = self.printer()
        for index, grams in ((9, 100), (0, -1), (0, 6000), (0, "1 X"), (0, True)):
            with self.assertRaises(q.PrinterError):
                printer.set_spool(index, grams)
        self.assertEqual(printer.sent, [])


class OpenSamplesTests(unittest.TestCase):
    def app(self):
        app = q.App()
        app.samples = lambda body: b"PK-data"
        return app

    def test_starts_studio_with_the_project(self):
        calls = []
        def run(command):
            calls.append(command)
            return type("Done", (), {"returncode": 0})
        self.app().open_samples({}, run)
        self.assertEqual(calls[0][:3], ["open", "-a", "QIDIStudio"])
        self.assertEqual(pathlib.Path(calls[0][3]).read_bytes(), b"PK-data")

    def test_reports_when_studio_does_not_start(self):
        with self.assertRaisesRegex(q.PrinterError, "Скачать"):
            self.app().open_samples({}, lambda command: type("Done", (), {"returncode": 1}))

    def test_refused_on_the_printer(self):
        q.printer_mode = True
        try:
            with self.assertRaises(q.PrinterError):
                self.app().open_samples({})
        finally:
            q.printer_mode = False


class ImportMaterialsTests(unittest.TestCase):
    RAW = q.json.dumps({
        "QIDI PLA Basic": {"filament_id": "QFA00", "nozzle_temperature": [190, 240],
                           "filament_dev_ams_drying_temperature": ["45"], "filament_dev_ams_drying_time": ["12"]},
        "New Plastic": {"filament_id": "QFZ01", "nozzle_temperature": [200, 250]},
        "broken": {"filament_id": "bad"}, "note": "text"})

    def test_reads_values_and_marks_new(self):
        items = q.import_materials(self.RAW, [{"id": "QFA00", "name": "QIDI PLA Basic"}])
        self.assertEqual(items[0], {"id": "QFA00", "name": "QIDI PLA Basic", "min_temp": 190, "max_temp": 240,
                                    "dry_temp": 45, "dry_hours": 12})
        self.assertEqual(items[1], {"id": "QFZ01", "name": "New Plastic", "min_temp": 200, "max_temp": 250, "new": True})
        self.assertEqual(len(items), 2)

    def test_first_import_marks_nothing_new(self):
        self.assertFalse([m for m in q.import_materials(self.RAW, []) if m.get("new")])

    def test_empty_list_is_rejected(self):
        with self.assertRaises(ValueError):
            q.import_materials("{}", [])


class SampleProjectTests(unittest.TestCase):
    SAMPLE = {"slot": 2, "material": "Bambu Lab PLA Matte", "min_temp": 190, "max_temp": 230,
              "dry_temp": 55, "dry_hours": 8}

    def open(self, samples):
        import xml.etree.ElementTree as et
        import zipfile
        import io
        archive = zipfile.ZipFile(io.BytesIO(q.sample_model.build_project(samples)))
        self.assertIsNone(archive.testzip())
        return et.fromstring(archive.read("3D/3dmodel.model")), et.fromstring(archive.read("Metadata/model_settings.config"))

    def test_objects_follow_counts_and_slots(self):
        white = {"slot": 0, "material": "PLA Lite"}
        model, settings = self.open([white, white, self.SAMPLE])
        ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
        self.assertEqual(len(model.findall(f"{ns}resources/{ns}object")), 2)
        extruders = [o.find("metadata[@key='extruder']").get("value") for o in settings.findall("object")]
        self.assertEqual(extruders, ["1", "3"])
        sequence = settings.find("plate/metadata[@key='print_sequence']").get("value")
        self.assertEqual(sequence, "by object")

    def test_text_has_material_nozzle_and_drying(self):
        self.assertEqual(q.sample_model.text_lines(self.SAMPLE),
                         ["Bambu Lab PLA Matte", "Сопло 190–230 °C", "Сушка 55 °C, 8 ч"])

    def test_text_stays_inside_plate(self):
        plate = q.sample_model.Plate()
        vertices, _ = q.sample_model.sample_mesh(self.SAMPLE, plate)
        self.assertLessEqual(max(v[0] for v in vertices), plate.width + .01)
        self.assertLessEqual(max(v[1] for v in vertices), plate.depth + .01)
        self.assertGreaterEqual(min(v[1] for v in vertices), 0)
        self.assertAlmostEqual(max(v[2] for v in vertices), 1.6)

    def covers(self, rectangles, x, y):
        return any(r[0] <= x < r[2] and r[1] <= y < r[3] for r in rectangles)

    def test_text_keeps_clear_of_hole_and_edges(self):
        m = q.sample_model
        plate = m.Plate()
        boxes = m.text_boxes(self.SAMPLE, plate)
        self.assertGreater(min(b[0] for b in boxes), plate.hole_x + plate.hole_r)
        self.assertLessEqual(max(b[2] for b in boxes), plate.width - plate.margin + .01)
        self.assertGreater(min(b[1] for b in boxes), 0.5)
        self.assertLess(max(b[3] for b in boxes), plate.depth - 0.5)

    def test_only_text_rises_above_the_plate(self):
        m = q.sample_model
        plate = m.Plate()
        x0, y0, x1, y1 = m.text_boxes(self.SAMPLE, plate)[0]
        top = m.top_rectangles(self.SAMPLE, plate)
        self.assertTrue(self.covers(top, (x0 + x1) / 2, (y0 + y1) / 2))
        self.assertFalse(self.covers(top, plate.width - 2, plate.depth / 2))

    def test_corners_are_round_and_hole_goes_through(self):
        m = q.sample_model
        plate = m.Plate()
        base = m._rectangles(m._outline_rows(plate), plate)
        self.assertFalse(self.covers(base, .3, .3))
        self.assertFalse(self.covers(base, plate.width - .3, plate.depth - .3))
        self.assertFalse(self.covers(base, plate.hole_x, plate.depth / 2))
        self.assertTrue(self.covers(base, plate.width - 3, plate.depth / 2))

    def test_same_color_groups_stay_close_and_groups_keep_clearance(self):
        m = q.sample_model
        plate = m.Plate()
        a, b = {"slot": 0, "material": "PLA"}, {"slot": 1, "material": "PLA"}
        placed = m.layout([a] * 4 + [b] * 4, plate)
        self.assertEqual([len(p[4]) for p in placed], [4, 4])
        first, second = placed
        inside = first[4]
        self.assertEqual(inside[1][1] - inside[0][1], plate.width + m.GROUP_GAP)
        apart_x = max(first[0], second[0]) - (min(first[0], second[0]) + first[2])
        apart_y = max(first[1], second[1]) - (min(first[1], second[1]) + first[3])
        self.assertGreaterEqual(max(apart_x, apart_y), m.GAP)
        for p in placed:
            self.assertGreaterEqual(p[0], 0)
            self.assertGreaterEqual(p[1], 0)
            self.assertLessEqual(p[0] + p[2], m.BED)
            self.assertLessEqual(p[1] + p[3], m.BED)

    def test_more_than_six_fit_when_grouped(self):
        m = q.sample_model
        plate = m.Plate()
        samples = [{"slot": 0, "material": "PLA"}] * 12
        self.assertEqual(sum(len(p[4]) for p in m.layout(samples, plate)), 12)

    def test_too_many_are_refused(self):
        m = q.sample_model
        with self.assertRaises(m.SampleError):
            m.layout([{"slot": 0, "material": "PLA"}] * 1000, m.Plate())

    def test_project_is_current_version_with_settings(self):
        import io
        import zipfile
        m = q.sample_model
        data = m.build_project([self.SAMPLE], {2: "D3B7A7"})
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("QIDIStudio-" + m.STUDIO_VERSION, archive.read("3D/3dmodel.model").decode())
        settings = q.json.loads(archive.read("Metadata/project_settings.config"))
        self.assertEqual(settings["print_sequence"], "by object")
        self.assertEqual(settings["filament_colour"][2], "#D3B7A7")
        self.assertEqual(len(settings["filament_type"]), m.SLOTS)

    def test_limits(self):
        with self.assertRaises(q.sample_model.SampleError):
            q.sample_model.build_project([])

    def test_every_profile_fits(self):
        plate = q.sample_model.Plate()
        for m in q.load_materials():
            sample = dict(m, slot=0, material=m["name"])
            q.sample_model.text_boxes(sample, plate)

    def test_too_long_name_is_refused(self):
        m = q.sample_model
        with self.assertRaises(m.SampleError):
            m.text_boxes({"material": "Very Long Plastic Name With Many Many Words Here And More Words"}, m.Plate())

    def test_preview_matches_project_layout(self):
        data = q.sample_model.preview([self.SAMPLE, self.SAMPLE])
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["lines"][1], "Сопло 190–230 °C")
        self.assertTrue(data["items"][0]["boxes"])
        empty = q.sample_model.preview([])
        self.assertEqual(empty["items"], [])
        self.assertTrue(empty["template"]["boxes"])

    def test_app_builds_from_slots_and_skips_zero(self):
        record = {"detail": "PLA Matte", "brand": "Bambu Lab", "tag_color": "D3B7A7", "color": "D3B7A7",
                  "filament": "QFA01", "min_temp": 190, "max_temp": 230, "dry_temp": 55, "dry_hours": 8}
        values = {"bambu_tag_slot2": record, "color_slot2": ["D3B7A7"], "filament_slot2": "QFA01",
                  "filament_slot0": "QFA00", "color_slot0": ["FAFAFA"]}

        def fetch(path, post=False):
            return {"result": {"status": {"save_variables": {"variables": values},
                                          "print_stats": {"state": "standby"}}}}
        app = q.App(printer=q.Printer("192.0.2.1", fetch))
        data = app.samples({"items": [{"index": 0, "count": 2}, {"index": 2, "count": 1}]})
        self.assertTrue(data.startswith(b"PK"))
        seen = []
        original = q.sample_model.build_project
        q.sample_model.build_project = lambda samples, colors=None: seen.extend(samples) or b"PK"
        try:
            app.samples({"items": [{"index": 1, "count": 1, "filament": "QFG00"},
                                   {"index": 2, "count": 1}, {"index": 2, "count": 1, "filament": "QFG00"}]})
        finally:
            q.sample_model.build_project = original
        self.assertEqual((seen[0]["slot"], seen[0]["material"]), (1, "QIDI PETG Basic"))
        self.assertGreaterEqual(seen[0]["min_temp"], 200)
        # A slot with a real tag keeps the tag's text unless it is edited.
        self.assertEqual(seen[1]["material"], "Bambu Lab PLA Matte")
        self.assertEqual(seen[2]["material"], "QIDI PETG Basic")
        with self.assertRaises(q.PrinterError):
            app.samples({"items": [{"index": 1, "count": 1, "filament": "QFBAD"}]})
        with self.assertRaises(q.PrinterError):
            app.samples({"items": [{"index": 9, "count": 1}]})
        with self.assertRaises(q.PrinterError):
            app.samples({"items": [{"index": 0, "count": 0}]})


class MapEditTests(unittest.TestCase):
    TEXT = "# head\n[materials]\n# note\nPLA Matte = 2\nPLA = 7\n\n[colors]\n# D3B7A7 = C4A484\n"

    def test_adds_color_rule_and_replaces_material(self):
        out = q.update_map(self.TEXT, "PLA Matte", "D3B7A7", "QFL30", "112233")
        self.assertIn("PLA Matte = QFL30\n", out)
        self.assertNotIn("PLA Matte = 2", out)
        self.assertIn("D3B7A7 = 112233\n", out)
        self.assertIn("# D3B7A7 = C4A484", out)
        self.assertIn("# note", out)

    def test_new_material_goes_into_its_section(self):
        out = q.update_map(self.TEXT, "PETG HF", "D3B7A7", "QFG02", "D3B7A7")
        self.assertLess(out.index("PETG HF = QFG02"), out.index("[colors]"))

    def test_color_same_as_tag_removes_the_rule(self):
        text = "[materials]\nPLA = 7\n[colors]\nD3B7A7 = 112233\n"
        out = q.update_map(text, "PLA", "D3B7A7", "QFA00", "D3B7A7")
        self.assertNotIn("112233", out)

    def test_result_is_a_valid_map(self):
        out = q.update_map(self.TEXT, "PLA Matte", "D3B7A7", "QFL30", "112233")
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "m.cfg"
            path.write_text(out)
            from bambu_rfid import load_map
            materials, colors = load_map(str(path))
        self.assertIn(("PLA Matte", "QFL30"), materials)
        self.assertEqual(colors, {"D3B7A7": "112233"})

    def test_bad_values_rejected(self):
        for args in (("PLA\n[x]", "D3B7A7", "QFA00", "D3B7A7"), ("PLA", "bad", "QFA00", "D3B7A7"),
                     ("PLA", "D3B7A7", "x", "D3B7A7"), ("PLA", "D3B7A7", "QFA00", "bad")):
            with self.assertRaises(q.PrinterError):
                q.update_map(self.TEXT, *args)


class PrinterTests(unittest.TestCase):
    def make(self, state="standby", variables=None):
        self.calls = []
        values = {"color_slot0": ["FAFAFA"], "filament_slot0": "QFA00"}
        values.update(variables or {})

        def fetch(path, post=False):
            self.calls.append((path, post))
            return {"result": {"status": {"save_variables": {"variables": values},
                                          "print_stats": {"state": state}}}}
        return q.Printer("192.0.2.1", fetch)

    def test_slots_lists_values(self):
        data = self.make().slots()
        first = data["slots"][0]
        self.assertEqual((first["label"], first["filament"], first["color"]), ("1A", "QFA00", "FAFAFA"))
        self.assertEqual([s["label"] for s in data["slots"]], ["1A", "1B", "1C", "1D"])
        self.assertIsNone(data["slots"][1]["color"])

    def test_old_numeric_color_is_not_shown_as_hex(self):
        data = self.make(variables={"color_slot1": 1}).slots()
        self.assertIsNone(data["slots"][1]["color"])

    def test_save_sends_new_format(self):
        self.make().save_slot(2, "QFA01", "D3B7A7")
        scripts = [q.urllib.parse.parse_qs(p.split("?", 1)[1])["script"][0]
                   for p, post in self.calls if post]
        self.assertEqual(scripts, ["SAVE_VARIABLE VARIABLE=filament_slot2 VALUE=\"'QFA01'\"",
                                   "SAVE_VARIABLE VARIABLE=color_slot2 VALUE=\"['D3B7A7']\""])

    def test_save_refused_while_printing(self):
        for state in ("printing", "paused"):
            with self.assertRaises(q.PrinterError):
                self.make(state).save_slot(0, "QFA01", "D3B7A7")
            self.assertFalse([c for c in self.calls if c[1]])

    def test_invalid_values_never_reach_the_printer(self):
        printer = self.make()
        for args in ((5, "QFA01", "D3B7A7"), (0, "bad", "D3B7A7"), (0, "QFA01", "red"),
                     (0, "QFA01'\nFIRMWARE_RESTART", "D3B7A7"), (0, "QFA01", "D3B7A7\nX")):
            with self.assertRaises(q.PrinterError):
                printer.save_slot(*args)
        self.assertFalse([c for c in self.calls if c[1]])

    def test_slots_include_real_tag_record(self):
        record = {"detail": "PLA Matte", "tag_color": "D3B7A7", "uid": "55365C10",
                  "color": "C4A484", "filament": "QFL30"}
        values = {"bambu_tag_slot2": record, "color_slot2": ["C4A484"], "filament_slot2": "QFL30"}
        data = self.make(variables=values).slots()
        self.assertEqual(data["slots"][2]["tag"], record)
        self.assertIsNone(data["slots"][0]["tag"])

    def test_record_hidden_when_slot_changed_since(self):
        record = {"detail": "PLA Matte", "tag_color": "D3B7A7", "color": "C4A484", "filament": "QFL30"}
        values = {"bambu_tag_slot2": record, "color_slot2": ["060606"], "filament_slot2": "QFA02"}
        self.assertIsNone(self.make(variables=values).slots()["slots"][2]["tag"])

    def test_save_mapping_keeps_record_valid(self):
        record = {"detail": "PLA Matte", "tag_color": "D3B7A7", "uid": "55365C10",
                  "color": "D3B7A7", "filament": "QFA01"}
        printer = self.make(variables={"bambu_tag_slot2": record})
        uploaded = []
        printer._map_text = lambda: "[materials]\n[colors]\n"
        printer._upload_map = uploaded.append
        printer.save_mapping(2, "PLA Matte", "D3B7A7", "QFL30", "C4A484")
        self.assertIn("PLA Matte = QFL30", uploaded[0])
        scripts = [q.urllib.parse.parse_qs(p.split("?", 1)[1])["script"][0]
                   for p, post in self.calls if post]
        for text in ("'tag_color': 'D3B7A7'", "'color': 'C4A484'", "'filament': 'QFL30'", "'uid': '55365C10'"):
            self.assertIn(text, scripts[-1])

    def test_read_slot_uses_stock_command(self):
        q.READ_SETTLE = 0
        printer = self.make()
        printer.read_slot(2)
        printer.read_thread.join(2)
        scripts = [q.urllib.parse.parse_qs(p.split("?", 1)[1])["script"][0]
                   for p, post in self.calls if post]
        self.assertEqual(scripts, ["SAVE_VARIABLE VARIABLE=bambu_tag_slot2 VALUE=\"{'empty': True}\"",
                                   "READ_RFID SLOT=2"])
        self.assertFalse(printer.reading[2])

    def same_tag_values(self):
        record = {"uid": "7081205A", "color": "EFE255", "filament": "QFA00"}
        return {"bambu_tag_slot0": record, "bambu_tag_slot1": dict(record), "bambu_tag_slot2": {"empty": True},
                "bambu_tag_slot3": {"uid": "472EDD02", "color": "68724D", "filament": "QFA01"},
                "color_slot0": ["EFE255"], "filament_slot0": "QFA00", "color_slot1": ["EFE255"], "filament_slot1": "QFA00",
                "color_slot3": ["68724D"], "filament_slot3": "QFA01"}

    def test_same_tag_in_two_slots_is_reported(self):
        slots = self.make(variables=self.same_tag_values()).slots()["slots"]
        self.assertEqual([slot["same_tag"] for slot in slots], [[1], [0], [], []])

    def test_two_tags_of_one_spool_count_as_the_same_spool(self):
        values = self.same_tag_values()
        values["bambu_tag_slot0"] = dict(values["bambu_tag_slot0"], tray="AA")
        values["bambu_tag_slot1"] = dict(values["bambu_tag_slot1"], uid="11111111", tray="AA")
        self.assertEqual(self.make(variables=values).slots()["slots"][0]["same_tag"], [1])

    def test_forget_tag_empties_only_that_slot(self):
        printer = self.make()
        printer.forget_tag(1)
        scripts = [q.urllib.parse.parse_qs(p.split("?", 1)[1])["script"][0] for p, post in self.calls if post]
        self.assertEqual(scripts, ["SAVE_VARIABLE VARIABLE=bambu_tag_slot1 VALUE=\"{'empty': True}\""])

    def test_forget_tag_refused_while_printing(self):
        status = {"result": {"status": {"save_variables": {"variables": {}}, "print_stats": {"state": "printing"}}}}
        with self.assertRaises(q.PrinterError):
            q.Printer("192.0.2.1", lambda path, post=False: status).forget_tag(1)

    def test_skip_neighbour_uses_our_read_with_empty_head(self):
        printer = self.make()
        started = []
        printer._read_with_motor = started.append
        printer.read_slot(1, skip_neighbour=True)
        printer.read_thread.join(2)
        self.assertEqual(started, [1])
        self.assertFalse(any(post and "READ_RFID" in p for p, post in self.calls))

    def test_read_error_reaches_the_slot(self):
        status = {"result": {"status": {"save_variables": {"variables": {}}, "print_stats": {"state": "standby"}}}}

        def fetch(path, post=False):
            if post and "READ_RFID" in path:
                raise q.PrinterError("Filament is loaded")
            return status
        printer = q.Printer("192.0.2.1", fetch)
        printer.read_slot(0)
        printer.read_thread.join(2)
        self.assertEqual(printer.slots()["slots"][0]["read_error"], "Filament is loaded")

    def test_empty_and_stock_records_are_reported(self):
        values = {"bambu_tag_slot0": {"empty": True},
                  "bambu_tag_slot1": {"stock": True, "color": "060606", "filament": "QFA02"},
                  "color_slot1": ["060606"], "filament_slot1": "QFA02"}
        slots = self.make(variables=values).slots()["slots"]
        self.assertTrue(slots[0]["tag"]["empty"])
        self.assertTrue(slots[1]["tag"]["stock"])

    def test_read_slot_does_not_wait_for_the_motor(self):
        release = threading.Event()
        status = {"result": {"status": {"save_variables": {"variables": {}}, "print_stats": {"state": "standby"}}}}

        def fetch(path, post=False):
            if post and "READ_RFID" in path:
                release.wait(5)
            return status
        printer = q.Printer("192.0.2.1", fetch)
        printer.read_slot(0)
        self.assertTrue(printer.read_thread.is_alive())
        release.set()
        printer.read_thread.join(2)

    def test_read_slot_refused_for_rack_and_while_printing(self):
        with self.assertRaises(q.PrinterError):
            self.make().read_slot(16)
        with self.assertRaises(q.PrinterError):
            self.make("printing").read_slot(0)

    def test_host_from_env(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / ".env"
            path.write_text("PRINTER_HOST=10.0.0.5\nPRINTER_USER=mks\n")
            self.assertEqual(q.read_printer_host(path), "10.0.0.5")
            self.assertIsNone(q.read_printer_host(pathlib.Path(d) / "none"))

    def test_profiles_have_nozzle_range(self):
        for m in q.load_materials():
            self.assertLess(m["min_temp"], m["max_temp"])

    def test_materials_list_has_profile_ids(self):
        ids = {m["id"]: m["name"] for m in q.load_materials()}
        self.assertEqual(ids["QFA01"], "QIDI PLA Matte Basic")
        self.assertIn("QFL30", ids)


class TagTests(unittest.TestCase):
    def test_reads_factory_tag(self):
        r = q.Tag(FakeTransport(QIDI, SOURCE)).read()
        self.assertEqual((r["key"], r["block"]), ("qidi", SOURCE.hex().upper()))

    def test_falls_back_to_default_key_after_failed_auth(self):
        r = q.Tag(FakeTransport(BLANK, bytes(16))).read()
        self.assertEqual(r["key"], "default")

    def test_exact_copy_to_blank_tag(self):
        src = q.Tag(FakeTransport(QIDI, SOURCE)).read()["block"]
        data = bytes.fromhex(src)
        target = FakeTransport(BLANK, bytes(16))
        q.Tag(target).write_block4(q.build_block(data[0], data[1], data[2], src[6:]))
        self.assertEqual(target.block, SOURCE)

    def test_color_change_touches_only_color_byte(self):
        src = SOURCE.hex().upper()
        target = FakeTransport(BLANK, bytes(16))
        q.Tag(target).write_block4(q.build_block(39, 6, 1, src[6:]))
        self.assertEqual(target.block, bytes([39, 6, 1]) + SOURCE[3:])

    def test_only_block_4_is_written(self):
        target = FakeTransport(BLANK, bytes(16))
        q.Tag(target).write_block4(SOURCE)
        self.assertEqual(target.writes, [4])

    def test_rejects_non_classic_tag(self):
        with self.assertRaises(q.TagError):
            q.Tag(FakeTransport(BLANK, bytes(16), atr=ULTRALIGHT_ATR)).read()

    def test_unknown_key_reports_error(self):
        with self.assertRaises(q.TagError):
            q.Tag(FakeTransport("A0A1A2A3A4A5", bytes(16))).read()

    def test_write_refused_reports_error(self):
        with self.assertRaises(q.TagError):
            q.Tag(FakeTransport(BLANK, bytes(16), write_ok=False)).write_block4(SOURCE)

    def test_invalid_values_rejected(self):
        tail = "00" * 13
        for args in ((0, 1, 1, tail), (1, 25, 1, tail), (1, 1, 256, tail), (1, 1, 1, "00")):
            with self.assertRaises(q.TagError):
                q.build_block(*args)


class FeatureTests(unittest.TestCase):
    def tearDown(self):
        q.apply_features(None)

    def test_page_has_a_guide_tab_and_its_panel(self):
        self.assertIn('id="tab-guide"', q.PAGE)
        self.assertIn('id="panel-guide"', q.PAGE)

    def test_file_decides_and_no_file_means_all(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "features.json"
            q.load_features(path)
            self.assertEqual(q.features, set(q.ALL_FEATURES))
            path.write_text('["bambu"]')
            q.load_features(path)
            self.assertEqual(q.features, {"bambu"})
            path.write_text("not json")
            q.load_features(path)
            self.assertEqual(q.features, set(q.ALL_FEATURES))


if __name__ == "__main__":
    unittest.main()
