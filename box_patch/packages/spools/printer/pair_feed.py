"""Feeding the two spools of one reader together (spools package): pair_logic decides how each
spool turns and which tag is whose, this module turns the motors through box_feed and reads the
reader of bambu_rfid."""
import collections
import logging
import math
import re

try:
    from . import bambu_rfid, pair_logic
    from .box_feed import FEED_LENGTH, PAIR_SWING_ROOM, SLOT_ENABLE_DELAY, \
        legacy_filament_id, tag_save_scripts
except ImportError:
    import bambu_rfid
    import pair_logic
    from box_feed import FEED_LENGTH, PAIR_SWING_ROOM, SLOT_ENABLE_DELAY, \
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
# A filament that reaches the splitter sensor (b_endstop, 0 when pressed) goes back this far
# past where it stopped: on 2 Nov all four stood in the splitter and the stock load refused.
SPLITTER_BACK = 250.
# A search that has not ended by then ends anyway: with the safe point short of the splitter the
# logic can keep rocking a spool there without end (seen in the model at 750 mm). Tags still
# missing are looked for after the splitter check.
PAIR_RUN_MAX = 120.
# A tag is at the antenna over about this much filament (24 degrees of a turn, as in the model;
# not measured on the printer). A reread turns slowly enough for the reader to look at least
# REREAD_LOOKS times meanwhile, judged by the recent waits between two readings of this reader.
REREAD_ZONE = 40.
REREAD_LOOKS = 1.5
REREAD_MIN_SPEED = 10.


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
        # The tag last written into each slot since its spool was put in; a reread keeps it.
        self.written = {slot: None for slot in slots}
        self.unconfirmed = set()
        self.tags_seen = {}
        self.timer = None
        self.accel = 200.
        self.planned_at = None
        self.gaps = collections.deque(maxlen=PAIR_GAPS)
        # Slots this run turns; the other slot of the pair may be ejecting on its own meanwhile.
        self.active = set()
        # Whether the splitter sensor was free at the last look; a filament in the toolhead keeps
        # it pressed, and then it tells nothing.
        self.splitter_free = None
        self.started_at = 0.
        # Tags of spools this run does not turn that stand at the antenna, said once per run.
        self.standing_said = set()

    def _new_logic(self):
        # Short of the nearer splitter of the two slots, with room for the swing past the limit.
        safe = getattr(self.mover, "safe_length", None)
        limit = min(safe(slot) for slot in self.slots) if safe is not None else FEED_LENGTH
        return pair_logic.PairLogic(("A", "B"), limit=limit - PAIR_SWING_ROOM,
                                    start_delay=0., min_pos=PAIR_MIN_POS)

    def release_slot(self, slot):
        """The stock code takes this slot: nothing more is planned for it. What is planned runs."""
        spool = self._spool(slot)
        spool.dir = 0
        spool.out = True
        self.active.discard(slot)
        if self.track[slot].end > 0.:
            self.mover._finish_after([slot], self.track[slot].end)

    def moved_by(self, slot, distance):
        """The spool was moved outside this run, by the splitter check."""
        spool = self._spool(slot)
        spool.pos += distance
        spool.rot += distance
        self.track[slot].planned += distance

    def _standing_tags(self, eventtime):
        """Tags of the spools of this reader that the logic does not follow but that are in
        their slot: one loaded in the toolhead, or fed in an earlier run. Their tag may stand at
        the antenna, or pass it while a print pulls the filament (6 Nov in the model: 1B took the
        tag of 1A standing in the toolhead, the only tag the antenna saw)."""
        standing = {}
        try:
            saved = self.mover.printer.lookup_object("save_variables").allVariables
        except Exception:
            return standing
        for slot in self.slots:
            spool = self._spool(slot)
            if spool.inserted and not spool.out:
                continue
            try:
                stepper = self.mover.printer.lookup_object("box_stepper slot%d" % (slot,))
                if stepper.get_status(eventtime)["runout_button"] != 0:
                    continue
            except Exception:
                continue
            record = saved.get("bambu_tag_slot%d" % (slot,))
            if not isinstance(record, dict) or record.get("empty"):
                continue
            for key in ("uid", "tray"):
                if record.get(key):
                    standing[record[key]] = slot
        return standing

    def _not_movable(self, slot):
        """In the toolhead or taken by the stock code: never turned by the pair."""
        in_head = getattr(self.mover, "in_head", None)
        owns = getattr(self.mover, "stock_owns", None)
        try:
            return bool((in_head is not None and in_head(slot)) or (owns is not None and owns(slot)))
        except Exception:
            logging.exception("bambu_rfid: slot%d toolhead check failed", slot)
            return False

    def has_tag(self, slot):
        return bool(self._spool(slot).tag)

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
        self.written[slot] = None
        self.active.add(slot)
        self._start(now)

    def reread(self, slot, now):
        """Read the tag of a spool that is already fed: 104% of a turn back and forward again to
        where it was, never further, only this spool turning. Not while the pair is still feeding."""
        spool = self._spool(slot)
        if spool.inserted and not spool.out and not self.logic.done and self.timer is not None:
            return False
        if not spool.inserted or spool.out:
            # Fed before Klipper started, or by the stock feed: it stands at the safe point.
            if self.logic.done and not any(s.inserted and not s.out for s in self.logic.spools):
                self.logic = self._new_logic()
                spool = self._spool(slot)
            spool.__init__(spool.name)
            spool.inserted = spool.started = True
            spool.inserted_at = now
            # Taken to stand at the safe point, where the search would have left it.
            assumed = self.logic.limit + PAIR_SWING_ROOM
            spool.pos = spool.rot = assumed
            guessed = getattr(self.mover, "guessed", None)
            if guessed is not None:
                guessed.add(slot)
            self.track[slot] = Track(planned=assumed)
        self.logic.rock_speed = self._reread_speed()
        if not self.logic.start_reread(self.names[slot], now):
            return False
        self.saved[slot] = None
        self.active.add(slot)
        self._start(now)
        return True

    def _start(self, now):
        # The speed mode is taken at the start of a run and holds until it ends.
        if self.timer is None:
            self.splitter_free = None
            self.started_at = now
            self.standing_said = set()
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
            if self._splitter_reached(estimated):
                self._finish()
                self.timer = None
                return reactor.NEVER
            held = self._held_at_cap()
            if held is None and eventtime - self.started_at > PAIR_RUN_MAX:
                held = min(self.active) if self.active else self.slots[0]
                logging.info("bambu_rfid: pair %s: the search took %.0f s, it ends", self.reader, PAIR_RUN_MAX)
            if held is not None:
                # The search wants it further than it may go: it ends here, and a tag still
                # missing is looked for after the splitter check, turning back from there.
                logging.info("bambu_rfid: pair %s: slot%d stands at its safe point, the search ends",
                             self.reader, held)
                for slot in self.slots:
                    spool = self._spool(slot)
                    spool.dir, spool.reread = 0, 0
                self.logic.reread_queue = []
                self.logic.done = True
                # The others of the run go on straight to their safe point, without the search.
                cap = self.mover.forward_cap
                for slot in sorted(self.active):
                    if not self._spool(slot).out:
                        self._run_on_to(slot, cap(slot), estimated)
                self._finish()
                self.timer = None
                return reactor.NEVER
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
            if kind == pair_logic.READ_TAG:
                standing = self._standing_tags(eventtime)
                uids = {uid, tag.get("uid"), tag.get("tray")} - {None}
                owner = next((standing[u] for u in uids if u in standing), None)
                if owner is not None:
                    # Not on a spool this run turns: for them the antenna is empty.
                    if uid not in self.standing_said:
                        self.standing_said.add(uid)
                        logging.info("bambu_rfid: pair %s: tag %s stands on slot%d, not taken",
                                     self.reader, uid, owner)
                    kind, tag, uid = pair_logic.READ_NONE, None, None
            if kind == pair_logic.READ_TAG and tag.get("unread"):
                found = next((s for s in self.logic.spools if s.reread and s.tag
                              and not self.tags_seen.get(s.tag, {}).get("unread")), None)
                if found is not None and found.tag != uid:
                    # A spool can carry two tags (Bambu spools do): one read in full in this
                    # reread is not replaced by another found but not read (7 Nov, 1A: its tag
                    # 8876F63E... then 55365C10, not read).
                    logging.info("bambu_rfid: pair %s: tag %s not read, %s keeps %s",
                                 self.reader, uid, found.name, found.tag)
                    kind, tag, uid = pair_logic.READ_NONE, None, None
            # A full reading of a tag replaces one where only its number was known.
            if tag and (not tag.get("unread") or uid not in self.tags_seen):
                self.tags_seen[uid] = tag
            pair_logic.note_reading(self.logic, kind, uid)
            # Pieces just planned count as turning, though their motor starts a moment later.
            planned_at = self.mover.printer.lookup_object("toolhead").mcu.estimated_print_time(now)
            for slot in self.slots:
                if self.track[slot].end > planned_at:
                    self._spool(slot).moving = True
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
            self.mover.motor_off(slot, estimated + SLOT_ENABLE_DELAY)
            # Let the slot go now, not when the pair is done: put in again, it starts afresh.
            self.active.discard(slot)
            self.mover.feeding.discard(slot)
            self.mover._finish_after([slot], track.end)
            self.mover.lengths.pop(slot, None)
            self.mover.mark_empty([slot])
            self.mover.printer.lookup_object("gcode").respond_raw(
                "!! Filament of slot %d left the sensor, insert it again" % (slot,))

    def _splitter_reached(self, estimated):
        """When the splitter sensor gets pressed while a motor of this pair turns forward, those
        motors stop and go back SPLITTER_BACK past where they stop; the run then ends. True if so."""
        extras = self.mover.printer.lookup_object("box_extras", None)
        pressed = getattr(extras, "b_endstop_state", None) == 0
        if getattr(self.mover, "checking", None) is not None \
                or estimated < getattr(self.mover, "splitter_back_until", 0.) + 0.5:
            # Another spool is at the sensor for its splitter check, or just leaving it.
            self.splitter_free = None
            return False
        was_free, self.splitter_free = self.splitter_free, not pressed
        if was_free is None and pressed:
            logging.info("bambu_rfid: pair %s: splitter sensor already pressed, not watched", self.reader)
        if not (was_free and pressed):
            return False
        forward = [slot for slot in sorted(self.active)
                   if not self._spool(slot).out and self.track[slot].speed > 0]
        if not forward:
            return False
        for slot in self.slots:
            self._spool(slot).dir = 0
        gcode = self.mover.printer.lookup_object("gcode")
        for slot in forward:
            track = self.track[slot]
            begin = track.end if track.end - estimated >= PAIR_JOIN else None
            if begin is not None:
                # It is still turning at the end of what is planned: stop there first.
                v = abs(track.speed)
                times = piece(track.speed * v / (2. * self.accel), v, v, 0., self.accel)
                start, end = self.mover.start_piece(slot, begin, times)
                track.add(start, end, times)
                begin = end
            back = SPLITTER_BACK + max(0., track.planned - track.position(estimated))
            end = self.mover.start_sequence([slot], [(-back, self.logic.speed, self.accel)],
                                            begin=begin, finish=False)
            track.pieces, track.end, track.speed = [], end, 0.
            track.planned -= back
            logging.info("bambu_rfid: slot%d reached the splitter sensor, back by %.0f mm", slot, back)
            gcode.respond_raw("!! Filament of slot %d reached the splitter, moved back %.0f mm" % (slot, back))
        # The search ends here: the next feed or reread of the pair starts afresh.
        self.logic = self._new_logic()
        return True

    def _run_on_to(self, slot, target, estimated):
        """Take a spool straight to target: it stops first if still turning at the end of what is
        planned, then one plain move."""
        track = self.track[slot]
        begin = track.end if track.end - estimated >= PAIR_JOIN else None
        if track.speed and begin is not None:
            v = abs(track.speed)
            times = piece(track.speed * v / (2. * self.accel), v, v, 0., self.accel)
            start, end = self.mover.start_piece(slot, begin, times)
            track.add(start, end, times)
            begin = end
        distance = target - track.planned
        if abs(distance) < 1.:
            return
        end = self.mover.start_sequence([slot], [(distance, self.logic.speed, self.accel)],
                                        begin=begin, finish=False)
        track.pieces, track.end, track.speed = [], end, 0.
        track.planned = target

    def _reread_speed(self):
        """Slow enough that the tag is seen as it passes, with the reads as slow as of late."""
        top = self.mover.mode()[1]["feed"] / 2.
        if not self.gaps:
            return top
        return max(REREAD_MIN_SPEED, min(top, REREAD_ZONE / (REREAD_LOOKS * max(self.gaps))))

    def _held_at_cap(self):
        """A spool the logic wants further forward than its cap, standing there."""
        cap = getattr(self.mover, "forward_cap", None)
        if cap is None:
            return None
        for slot in sorted(self.active):
            spool = self._spool(slot)
            limit = cap(slot)
            # A run with its own end at the cap, as the way back of a reread, simply ends there.
            if not spool.out and spool.dir > 0 and not spool.moving \
                    and self.track[slot].planned >= limit - 0.5 \
                    and (spool.target is None or spool.target > limit + 0.5):
                return slot
        return None

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
        if spool.dir and self._not_movable(slot):
            # The logic may ask it to back off from the antenna; this motor is not ours now.
            spool.dir = 0
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
            cap = getattr(self.mover, "forward_cap", None)
            if want > 0 and cap is not None:
                # Never past the safe point, or past where the splitter check parked it.
                stops.append(cap(slot))
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
        # A spool moved for the reread of the other one belongs to this run until it ends.
        self.active.add(slot)
        if spool.target is not None and abs(track.planned - spool.target) < 0.01:
            track.planned = spool.target
        return True

    def _save_tags(self):
        """Write a tag the logic has just settled; the details come from a reading of that tag."""
        gcode = self.mover.printer.lookup_object("gcode")
        for slot in self.slots:
            spool = self._spool(slot)
            details = self.tags_seen.get(spool.tag)
            written = self.written[slot]
            other = self._spool(self.slots[1] if slot == self.slots[0] else self.slots[0])
            if written and not spool.tag and spool.no_tag and other.tag != written \
                    and written not in self.unconfirmed:
                # A reread missing a tag says little with slow reads: the tag stays.
                self.unconfirmed.add(written)
                logging.info("bambu_rfid: slot%d tag %s not seen by the reread, kept", slot, written)
            if written and not spool.tag and other.tag == written:
                # The tag written here is the neighbour's: the slot is left without a tag, as the
                # panel's «forget» leaves it.
                gcode.run_script("SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"{'empty': True}\"" % (slot,))
                logging.info("bambu_rfid: slot%d tag %s taken back, no tag", slot, written)
                self.written[slot] = self.saved[slot] = None
                continue
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
            self.written[slot] = spool.tag
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
            feed_done = getattr(self.mover, "feed_done", None)
            if feed_done is not None:
                feed_done(ready)
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
