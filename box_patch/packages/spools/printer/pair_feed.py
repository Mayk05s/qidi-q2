"""Feeding the two spools of one reader together (spools package): pair_logic decides how each
spool turns and which tag is whose, this module turns the motors through box_feed and reads the
reader of bambu_rfid."""
import collections
import logging
import math
import re

try:
    from . import bambu_rfid, pair_logic
    from .box_feed import FEED_LENGTH, SLOT_ENABLE_DELAY, \
        legacy_filament_id, tag_save_scripts
except ImportError:
    import bambu_rfid
    import pair_logic
    from box_feed import FEED_LENGTH, SLOT_ENABLE_DELAY, \
        legacy_filament_id, tag_save_scripts

# The pair logic is asked every PAIR_POLL seconds; a motor is given PAIR_PIECE seconds of travel
# whenever less than PAIR_LEAD is left, so a stop takes effect within about half a second.
PAIR_POLL = 0.2
# A read slower than this is logged: pieces join only if a tick comes within PAIR_LEAD.
PAIR_SLOW_READ = 0.3
PAIR_PIECE = 0.4
PAIR_LEAD = 0.6
# A read blocks Klipper for 0.3 to 1.6 s (2 Nov), and the other reader's read comes in between:
# the travel planned ahead outlasts the longest recent wait between two plannings, up to this.
PAIR_LEAD_MAX = 2.5
PAIR_GAPS = 12
# A piece is joined to the one before only if that one ends at least this far ahead, else the
# steps could reach the MCU too late.
PAIR_JOIN = 0.25
# Backwards the filament end stays this far past the slot sensor so the gear keeps it.
PAIR_MIN_POS = 60.


def read_pair(reader):
    """One look at the antenna of a bambu_rfid reader: ("tag", tag), ("collision", None) when
    two tags answer, or ("none", None). Settled tags are not left out here, the logic needs to
    see them to keep track of where they stand."""
    proxy = getattr(reader, "proxy", None)
    if proxy is not None:
        proxy.busy = True
    try:
        return pair_logic.READ_TAG, reader._read_slot_tag(0., [], [])
    except bambu_rfid.SeveralTags:
        return pair_logic.READ_COLLISION, None
    except bambu_rfid.TagAbsent:
        return pair_logic.READ_NONE, None
    except bambu_rfid.RfidError as e:
        # The reason goes into a SAVE_VARIABLE value: no quotes or brackets in it.
        reason = re.sub(r"[\"'\[\]{}()]", "", "%s: %s" % (type(e).__name__, e))
        uid = getattr(e, "uid", None)
        if not uid:
            # Nothing answered clearly enough to have a number: the same as no tag.
            logging.info("bambu_rfid: %s no tag number: %s", getattr(reader, "name", "?"), reason)
            return pair_logic.READ_NONE, None
        # A tag is there, only its contents could not be read: it is still this spool's tag.
        uid = uid.hex().upper() if isinstance(uid, (bytes, bytearray)) else str(uid).upper()
        logging.info("bambu_rfid: %s tag %s found, not read: %s", getattr(reader, "name", "?"), uid, reason)
        tag = {"uid": uid, "unread": reason, "detail": UNREAD_DETAIL}
        for key in ("sak", "atqa"):
            value = getattr(e, key, None)
            if value is not None:
                tag[key] = value.hex().upper() if isinstance(value, (bytes, bytearray)) else value
        return pair_logic.READ_TAG, tag
    finally:
        if proxy is not None:
            proxy.busy = False


UNREAD_DETAIL = "Метка не прочитана"


def unread_save_scripts(slot, tag, replacement):
    """G-code that records a tag found but not read; a replacement set for its number gives the
    slot its material and color, otherwise those stay as they are."""
    scripts = []
    record = {key: tag[key] for key in ("uid", "unread", "detail", "sak", "atqa") if tag.get(key) is not None}
    if replacement:
        if replacement.get("filament"):
            scripts.append("SAVE_VARIABLE VARIABLE=filament_slot%d VALUE=\"'%s'\"" % (slot, replacement["filament"]))
            record["filament"] = replacement["filament"]
        if replacement.get("color"):
            scripts.append("SAVE_VARIABLE VARIABLE=color_slot%d VALUE=\"['%s']\"" % (slot, replacement["color"]))
            record["color"] = replacement["color"]
        record["replacement"] = True
    scripts.append("SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"%r\"" % (slot, record))
    return scripts


def piece(distance, start_v, speed, end_v, accel):
    """Times of one move that starts and ends at the given speeds, as trapq takes them:
    (accel_t, cruise_t, decel_t, axis_r, start_v, cruise_v, accel). A move too short for the
    full speed tops out lower."""
    length = abs(distance)
    cruise_v = min(speed, math.sqrt(max(0., accel * length + (start_v ** 2 + end_v ** 2) / 2.)))
    cruise_v = max(cruise_v, start_v, end_v)
    accel_t = (cruise_v - start_v) / accel
    decel_t = (cruise_v - end_v) / accel
    rest = length - (start_v + cruise_v) / 2. * accel_t - (cruise_v + end_v) / 2. * decel_t
    cruise_t = max(0., rest) / cruise_v if cruise_v else 0.
    return accel_t, cruise_t, decel_t, (1. if distance >= 0 else -1.), start_v, cruise_v, accel


def piece_position(start, times, t):
    """Where a piece that began at start has got to at time t, relative to its start."""
    accel_t, cruise_t, decel_t, axis_r, start_v, cruise_v, accel = times
    t = max(0., t - start)
    a = min(t, accel_t)
    d = start_v * a + accel * a * a / 2.
    c = min(max(0., t - accel_t), cruise_t)
    d += cruise_v * c
    e = min(max(0., t - accel_t - cruise_t), decel_t)
    d += cruise_v * e - accel * e * e / 2.
    return axis_r * d


class PairDriver:
    """Runs pair_logic for the two slots of one reader. Each motor gets short pieces of travel
    planned a little ahead, joined at full speed, so a spool turns smoothly; the logic sees
    where the spool really is now, whether it has stopped, and what the reader sees, the same
    things the emulator gives it. A tag the logic settles is written into its slot."""

    def __init__(self, mover, reader, slots):
        self.mover, self.reader, self.slots = mover, reader, slots
        self.names = {slots[0]: "A", slots[1]: "B"}
        self.logic = self._new_logic()
        self.track = {slot: Track() for slot in slots}
        self.saved = {slot: None for slot in slots}
        self.tags_seen = {}
        self.timer = None
        self.accel = 200.
        self.planned_at = None
        self.gaps = collections.deque(maxlen=PAIR_GAPS)
        # Slots this run turns; the other slot of the pair may be ejecting on its own meanwhile.
        self.active = set()

    @staticmethod
    def _new_logic():
        return pair_logic.PairLogic(("A", "B"), limit=FEED_LENGTH,
                                    start_delay=0., min_pos=PAIR_MIN_POS)

    def _spool(self, slot):
        return self.logic.spool(self.names[slot])

    def insert(self, slot, now, after=0.):
        """A spool was put in; after is when its catch move ends, the logic starts it then."""
        if self.logic.done and not any(s.inserted and not s.out for s in self.logic.spools):
            self.logic = self._new_logic()
        spool = self._spool(slot)
        if spool.inserted:
            # A spool put in again starts from scratch.
            spool.__init__(spool.name)
        self.logic.insert(self.names[slot], now)
        self.track[slot] = Track(after)
        self.saved[slot] = None
        self.active.add(slot)
        self._start(now)

    def reread(self, slot, now):
        """Read the tag of a spool that is already fed: 52% of a turn back, 104% forward and
        back to where it was, only this spool turning. Not while the pair is still feeding."""
        spool = self._spool(slot)
        if spool.inserted and not spool.out and not self.logic.done:
            return False
        if not spool.inserted or spool.out:
            # Fed before Klipper started, or by the stock feed: it stands at the safe point.
            if self.logic.done and not any(s.inserted and not s.out for s in self.logic.spools):
                self.logic = self._new_logic()
                spool = self._spool(slot)
            spool.__init__(spool.name)
            spool.inserted = spool.started = True
            spool.inserted_at = now
            spool.pos = spool.rot = FEED_LENGTH
            self.track[slot] = Track(planned=FEED_LENGTH)
        if not self.logic.start_reread(self.names[slot], now):
            return False
        self.saved[slot] = None
        self.active.add(slot)
        self._start(now)
        return True

    def _start(self, now):
        # The speed mode is taken at the start of a run and holds until it ends.
        if self.timer is None:
            speeds = self.mover.mode()[1]
            self.accel = speeds["accel"]
            self.logic.speed, self.logic.rock_speed = speeds["feed"], speeds["feed"] / 2.
            reactor = self.mover.printer.get_reactor()
            self.timer = reactor.register_timer(self._tick, now + PAIR_POLL)

    def _tick(self, eventtime):
        reactor = self.mover.printer.get_reactor()
        try:
            estimated = self.mover.printer.lookup_object("toolhead").mcu.estimated_print_time(eventtime)
            for slot in self.slots:
                self._follow(slot, estimated, eventtime)
            # Klipper stands still while the reader reads: the motors get enough travel first.
            self._plan_all(eventtime, estimated)
            reader = self.mover.readers.get(self.reader)
            started = reactor.monotonic()
            kind, tag = read_pair(reader) if reader is not None else ("none", None)
            now = reactor.monotonic()
            if now - started > PAIR_SLOW_READ:
                logging.info("bambu_rfid: pair %s read took %.2f s", self.reader, now - started)
            # A spool is known by its tray number when the tag has one: Bambu spools and spools
            # tagged in our panel carry two tags with the same tray, and they are one spool.
            uid = (tag.get("tray") or tag["uid"]) if tag else None
            # A full reading of a tag replaces one where only its number was known.
            if tag and (not tag.get("unread") or uid not in self.tags_seen):
                self.tags_seen[uid] = tag
            pair_logic.note_reading(self.logic, kind, uid)
            said = len(self.logic.log)
            self.logic.tick(eventtime, kind, uid)
            for line in self.logic.log[said:]:
                logging.info("bambu_rfid: pair %s: %s", self.reader, line)
            self._save_tags()
            estimated = self.mover.printer.lookup_object("toolhead").mcu.estimated_print_time(now)
            self._plan_all(now, estimated)
            if self.logic.done and not any(self.track[s].end > estimated for s in self.slots):
                self._finish()
                self.timer = None
                return reactor.NEVER
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: pair %s failed", self.reader)
            self._finish()
            self.timer = None
            return reactor.NEVER
        return eventtime + PAIR_POLL

    def _follow(self, slot, estimated, eventtime):
        """Tell the logic where the spool is now and whether its motor still turns."""
        spool, track = self._spool(slot), self.track[slot]
        track.forget(estimated)
        pos = track.position(estimated)
        spool.moving = track.end > estimated
        if not spool.moving:
            pos = track.planned
        spool.rot += pos - spool.pos
        spool.pos = pos
        stepper = self.mover.printer.lookup_object("box_stepper slot%d" % (slot,))
        if spool.inserted and not spool.out and stepper.get_status(eventtime)["runout_button"] != 0:
            # The filament has left the slot sensor: whatever is still planned turns nothing.
            spool.out = True
            spool.dir = 0
            print_time = estimated + SLOT_ENABLE_DELAY
            self.mover.printer.lookup_object("stepper_enable").lookup_enable(
                "slot%d" % (slot,)).motor_disable(print_time)
            # Let the slot go now, not when the pair is done: put in again, it starts afresh.
            self.active.discard(slot)
            self.mover.feeding.discard(slot)
            self.mover._finish_after([slot], track.end)
            self.mover.printer.lookup_object("gcode").respond_raw(
                "!! Filament of slot %d left the sensor, insert it again" % (slot,))

    def _plan_all(self, now, estimated):
        """Plan both motors far enough ahead to last until the next planning, judged by how long
        the recent waits between plannings were."""
        if self.planned_at is not None:
            self.gaps.append(now - self.planned_at)
        self.planned_at = now
        lead = PAIR_LEAD
        if self.gaps:
            lead = min(PAIR_LEAD_MAX, max(lead, max(self.gaps) + PAIR_JOIN + 0.1))
        for slot in self.slots:
            # A few pieces at most: each turns for PAIR_PIECE or ends a run.
            for _ in range(8):
                if not self._plan(slot, estimated, lead):
                    break

    def _plan(self, slot, estimated, lead=PAIR_LEAD):
        """Give the motor its next piece when what is planned runs out within lead; True if one
        was given."""
        spool, track = self._spool(slot), self.track[slot]
        left = track.end - estimated
        if left > lead or spool.out:
            return False
        if track.speed and left < PAIR_JOIN:
            # Too late to join at speed: the run ends where it is planned to and starts again.
            if left > 0.:
                return False
            logging.warning("bambu_rfid: slot%d piece came late, the motor stopped", slot)
            track.speed = 0.
        want = spool.dir * spool.speed
        if not track.speed and not want:
            return False
        begin = track.end if left >= PAIR_JOIN else None
        if track.speed and want != track.speed:
            # Stop first; a new direction or speed starts from standstill next time.
            v = abs(track.speed)
            times = piece(track.speed * v / (2. * self.accel), v, v, 0., self.accel)
        else:
            v = abs(want)
            ahead = want * PAIR_PIECE
            if not track.speed:
                ahead += want * v / (2. * self.accel)
            # Where this run has to end, if anywhere: backwards the filament must not come
            # closer to the slot sensor than PAIR_MIN_POS, and the logic may name a target.
            stops = [PAIR_MIN_POS] if want < 0 else []
            if spool.target is not None:
                stops.append(spool.target)
            room = min((track.planned - stop) * (-1. if want > 0 else 1.) for stop in stops) if stops else None
            if room is not None and room - abs(ahead) - v * v / (2. * self.accel) < 0.:
                # The last piece stops exactly there.
                if room < 0.5:
                    return False
                times = piece(room if want > 0 else -room, abs(track.speed), v, 0., self.accel)
            else:
                times = piece(ahead, abs(track.speed), v, v, self.accel)
        start, end = self.mover.start_piece(slot, begin, times)
        track.add(start, end, times)
        if spool.target is not None and abs(track.planned - spool.target) < 0.01:
            track.planned = spool.target
        return True

    def _save_tags(self):
        """Write a tag the logic has just settled; the details come from a reading of that tag."""
        gcode = self.mover.printer.lookup_object("gcode")
        for slot in self.slots:
            spool = self._spool(slot)
            details = self.tags_seen.get(spool.tag)
            if not spool.tag or details is None:
                continue
            # Saved again only when it is new or now read in full.
            state = (spool.tag, bool(details.get("unread")))
            if state == self.saved[slot]:
                continue
            if details.get("unread"):
                lookup = getattr(self.mover.readers.get(self.reader), "mapping_for_uid", None)
                replacement = lookup(spool.tag) if lookup is not None else None
                scripts = unread_save_scripts(slot, details, replacement)
            else:
                filament = details.get("profile") or legacy_filament_id(details.get("material"))
                scripts = tag_save_scripts(slot, details, filament)
            for script in scripts:
                gcode.run_script(script)
            self.saved[slot] = state
            logging.info("bambu_rfid: slot%d has tag %s (%s)%s", slot, spool.tag,
                         pair_logic.SURE_NAMES.get(spool.sure, ""), ", not read" if details.get("unread") else "")

    def _finish(self):
        active, self.active = sorted(self.active), set()
        ready = [slot for slot in active if not self._spool(slot).out]
        for slot in ready:
            # An eject can then run fast over this length.
            self.mover.lengths[slot] = self.track[slot].planned
        if ready:
            self.mover._mark_ready(ready)
        if active:
            self.mover._finish_after(active, max(self.track[slot].end for slot in active))


class Track:
    """What is planned for one motor: the pieces, where they lead and how fast it goes at the end.
    Positions are counted from where the spool was put in."""

    def __init__(self, end=0., planned=0.):
        self.pieces = []
        self.end = end
        self.planned = planned
        self.speed = 0.

    def add(self, start, end, times):
        self.pieces.append((start, times, self.planned))
        self.planned += piece_position(start, times, start + times[0] + times[1] + times[2])
        self.end = end
        accel_t, cruise_t, decel_t, axis_r, start_v, cruise_v, accel = times
        end_v = cruise_v - accel * decel_t
        self.speed = axis_r * end_v if end_v > 1e-6 else 0.

    def forget(self, t):
        """Pieces followed by one that has begun by t do not change the position any more."""
        while len(self.pieces) > 1 and self.pieces[1][0] <= t:
            self.pieces.pop(0)

    def position(self, t):
        if not self.pieces:
            return self.planned
        for start, times, base in reversed(self.pieces):
            if t >= start:
                return base + piece_position(start, times, t)
        return self.pieces[0][2]
