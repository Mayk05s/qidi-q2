"""Feeding the two spools of one reader together (spools package): pair_logic decides how each
spool turns and which tag is whose, this module turns the motors through box_feed and reads the
reader of bambu_rfid."""
import logging
import math

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
PAIR_PIECE = 0.4
PAIR_LEAD = 0.6
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
    except bambu_rfid.RfidError:
        return pair_logic.READ_NONE, None
    finally:
        if proxy is not None:
            proxy.busy = False


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
            reader = self.mover.readers.get(self.reader)
            kind, tag = read_pair(reader) if reader is not None else ("none", None)
            uid = tag["uid"] if tag else None
            if tag:
                self.tags_seen[uid] = tag
            pair_logic.note_reading(self.logic, kind, uid)
            self.logic.tick(eventtime, kind, uid)
            self._save_tags()
            for slot in self.slots:
                self._plan(slot, estimated)
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

    def _plan(self, slot, estimated):
        """Give the motor its next piece when what is planned runs out soon."""
        spool, track = self._spool(slot), self.track[slot]
        left = track.end - estimated
        if left > PAIR_LEAD or spool.out:
            return
        if track.speed and left < PAIR_JOIN:
            # Too late to join at speed: the run ends where it is planned to and starts again.
            if left > 0.:
                return
            logging.warning("bambu_rfid: slot%d piece came late, the motor stopped", slot)
            track.speed = 0.
        want = spool.dir * spool.speed
        if not track.speed and not want:
            return
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
                    return
                times = piece(room if want > 0 else -room, abs(track.speed), v, 0., self.accel)
            else:
                times = piece(ahead, abs(track.speed), v, v, self.accel)
        start, end = self.mover.start_piece(slot, begin, times)
        track.add(start, end, times)
        if spool.target is not None and abs(track.planned - spool.target) < 0.01:
            track.planned = spool.target

    def _save_tags(self):
        """Write a tag the logic has just settled; the details come from a reading of that tag."""
        gcode = self.mover.printer.lookup_object("gcode")
        for slot in self.slots:
            spool = self._spool(slot)
            details = self.tags_seen.get(spool.tag)
            if not spool.tag or spool.tag == self.saved[slot] or details is None:
                continue
            filament = details.get("profile") or legacy_filament_id(details.get("material"))
            for script in tag_save_scripts(slot, details, filament):
                gcode.run_script(script)
            self.saved[slot] = spool.tag
            logging.info("bambu_rfid: slot%d has tag %s (%s)", slot, spool.tag,
                         pair_logic.SURE_NAMES.get(spool.sure, ""))

    def _finish(self):
        active, self.active = sorted(self.active), set()
        ready = [slot for slot in active if not self._spool(slot).out]
        for slot in ready:
            # An eject can then run fast over this length.
            self.mover.lengths[slot] = self.track[slot].planned
        if ready:
            self.mover._mark_ready(ready)
        gcode = self.mover.printer.lookup_object("gcode")
        for slot in active:
            if self._spool(slot).out:
                gcode.respond_raw("!! Filament of slot %d left the sensor, insert it again" % (slot,))
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
        # Pieces that ended long ago do not change the position any more.
        while len(self.pieces) > 1 and self.pieces[1][0] < start:
            self.pieces.pop(0)

    def position(self, t):
        if not self.pieces:
            return self.planned
        for start, times, base in reversed(self.pieces):
            if t >= start:
                return base + piece_position(start, times, t)
        return self.pieces[0][2]
