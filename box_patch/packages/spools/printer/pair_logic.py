# Finds whose tag is whose when two spools share one reader, and feeds them to the safe point.
# Tag logic of a reader pair, run as is by tools/feed_emulator. It knows no hardware: tick() takes what the reader
# sees and sets the wanted direction of each spool; the caller moves the motors.
TURN = 630.
READ_NONE, READ_TAG, READ_COLLISION = "none", "tag", "collision"
# How a tag was given to a spool, from the weakest evidence to the strongest.
SURE_GUESS, SURE_LEFT, SURE_APPEARED, SURE_REREAD = 1, 2, 3, 4
SURE_NAMES = {SURE_GUESS: "guess", SURE_LEFT: "left with the turning spool",
              SURE_APPEARED: "appeared while only it turned", SURE_REREAD: "reread"}


class Spool:
    def __init__(self, name):
        self.name = name
        self.pos = 0.
        self.rot = 0.
        self.dir = 0
        self.moving = False  # the caller says whether the motor has really stopped
        self.speed = 0.
        self.tag = None
        self.no_tag = False
        self.out = False
        self.out_said = False
        self.inserted = False
        self.inserted_at = None
        self.started = False
        self.clearing = False
        self.waiting = False
        self.paused = False
        self.swing = 0
        self.swing_paused = 0
        self.swing_from = 0.
        self.reread = 0
        self.reread_base = 0.
        self.at_antenna = False
        self.sure = 0


class PairLogic:
    def __init__(self, names, limit=1000., test=350., rock=20., rock_speed=30., speed=60.,
                 swing=1.04, start_delay=2., watchdog=60., min_pos=60.):
        self.spools = [Spool(name) for name in names]
        self.limit, self.test, self.rock_length = limit, test, rock
        self.rock_speed, self.speed, self.swing = rock_speed, speed, swing
        self.start_delay, self.watchdog, self.min_pos = start_delay, watchdog, min_pos
        self.now = 0.
        self.phase = "search"
        self.rock = 0
        self.rock_base = 0.
        self.tester = self.waiter = None
        self.candidate = None
        self.test_from = 0.
        self.gone = 0
        self.swapped = False
        self.done = False
        self.guard = False
        self.progress = 0.
        self.signature = None
        self.last_none_at = -1.
        self.reread_queue = []
        self.last_kind = None  # no reading yet: a tag seen first is not known to have just come
        self.log = []

    def spool(self, name):
        return next(s for s in self.spools if s.name == name)

    def _say(self, text):
        self.log.append("%.1f %s" % (self.now, text))

    def _run(self, spool, direction, speed=None):
        spool.dir, spool.speed = direction, speed or self.speed

    def _halt(self, spool):
        spool.dir, spool.speed = 0, 0.

    def insert(self, name, now):
        spool = self.spool(name)
        if spool.inserted:
            return
        spool.inserted, spool.inserted_at = True, now
        self.done = False
        self.progress = now

    def start_reread(self, name, now):
        spool = self.spool(name)
        if spool.dir or spool.reread or not spool.started:
            return False
        spool.reread, spool.reread_base = 1, spool.pos
        spool.tag, spool.no_tag = None, False
        self.done = self.guard = False
        self.progress = now
        self._run(spool, -1)
        return True

    def _put(self, spool, uid, sure=SURE_APPEARED, reread=False):
        """Write a tag to a spool with how sure the logic is about it. One tag cannot sit on
        two spools: then only the less sure one is read again. A reread is the strongest
        evidence, as only that spool turns: if it finds the same tag, the other spool is
        the doubtful one and is read again next."""
        other = self._other(spool)
        if other.tag == uid:
            if reread or sure > other.sure:
                self._say("tag %s on both spools, reading %s again" % (uid, other.name))
                other.tag = None
                self._queue_reread(other)
            else:
                self._say("tag %s on both spools, reading %s again" % (uid, spool.name))
                self._queue_reread(spool)
                return
        spool.tag, spool.sure = uid, sure
        self._say("tag %s is %s (%s)" % (uid, spool.name, SURE_NAMES[sure]))

    def _queue_reread(self, spool):
        if spool.name not in self.reread_queue:
            self.reread_queue.append(spool.name)
        self.done = False

    def _other(self, spool):
        return self.spools[1] if spool is self.spools[0] else self.spools[0]

    def _owner(self, uid):
        return next((s for s in self.spools if s.tag == uid), None)

    @staticmethod
    def _open(spool):
        return spool.started and not spool.tag and not spool.no_tag and not spool.out

    def _tick_reread(self, spool, kind, uid):
        other, base = self._other(spool), spool.reread_base
        if other.dir > 0:
            self._halt(other)
        if spool.out:
            spool.reread = 0
            self._say("reread %s: filament left" % (spool.name,))
            self.done = not self.reread_queue
            return
        if kind == READ_COLLISION and other.tag and other.at_antenna:
            self._run(other, 1)
            return
        if kind == READ_TAG and (not self._owner(uid) or self._owner(uid) is other):
            self._put(spool, uid, SURE_REREAD, reread=True)
            spool.no_tag = False
            spool.reread = 3
        half = TURN * 0.52
        if spool.reread == 1 and (spool.pos <= base - half or spool.pos <= self.min_pos):
            spool.reread = 2
            self._run(spool, 1)
        elif spool.reread == 2 and spool.pos >= base:
            spool.reread = 4
            self._run(spool, 1)
        elif spool.reread == 4 and spool.pos >= base + half:
            spool.reread = 5
            self._run(spool, -1)
        elif spool.reread == 5 and spool.pos <= base:
            self._halt(spool)
            spool.reread = 0
            if not spool.tag:
                spool.no_tag = True
                self._say("reread %s: no tag" % (spool.name,))
            self.done = not self.reread_queue
        elif spool.reread == 3:
            if spool.pos > base and spool.dir >= 0:
                self._run(spool, -1)
            elif spool.pos < base and spool.dir <= 0:
                self._run(spool, 1)
            if abs(spool.pos - base) < 15.:
                self._halt(spool)
                spool.reread = 0
                self.done = not self.reread_queue

    def tick(self, now, kind, uid=None):
        if self.reread_queue and not any(s.reread for s in self.spools):
            name = self.reread_queue[0]
            spool = self.spool(name)
            if not spool.dir and not spool.moving and spool.started and self.phase == "search" \
                    and not self._other(spool).moving:
                self.reread_queue.pop(0)
                self.start_reread(name, now)
        self._tick(now, kind, uid)
        self.last_kind = kind
        if kind == READ_NONE:
            self.last_none_at = now

    def _tick(self, now, kind, uid=None):
        self.now = now
        both = self.spools
        for s in both:
            if s.inserted and not s.started and now - s.inserted_at >= self.start_delay:
                s.started = True
        busy = next((s for s in both if s.reread), None)
        if busy is not None:
            self._tick_reread(busy, kind, uid)
            return
        for s in both:
            if s.out and not s.out_said:
                s.out_said = True
                s.swing, s.clearing, s.waiting = 0, False, False
                self._say("filament of %s left the sensor" % (s.name,))
                for o in both:
                    o.waiting = False
                if self.phase != "search":
                    self.phase, self.rock = "search", 0
        active = any(s.started and (s.dir or s.waiting or s.swing or self._open(s)) for s in both)
        if active and now - self.progress > self.watchdog and not self.guard:
            self.guard = self.done = True
            self._say("no progress, stopped")
            for s in both:
                self._halt(s)
            return
        signature = (tuple((round(s.pos / 50.), s.tag, s.no_tag, s.out, s.swing) for s in both), self.phase)
        if signature != self.signature:
            self.signature, self.progress = signature, now
        for s in both:
            if s.started and s.dir > 0 and s.pos >= self.limit and not s.swing and not s.clearing \
                    and not (s.tag and s.at_antenna):
                self._halt(s)
            if s.swing and s.tag:
                s.swing = 0
                self._halt(s)
            if s.clearing and not s.at_antenna:
                s.clearing = False
                self._halt(s)
        if kind == READ_COLLISION:
            for s in both:
                if s.tag and s.at_antenna and not s.clearing:
                    s.clearing = True
                    self._run(s, 1)
                    other = self._other(s)
                    if self._open(other) and other.dir > 0:
                        self._halt(other)
                        other.paused = True
        for s in both:
            if s.paused and kind != READ_COLLISION:
                s.paused = False
                if kind == READ_TAG and not self._owner(uid) and not s.tag:
                    self._put(s, uid, SURE_LEFT)
        if self.phase == "search" and self._tick_search(kind, uid):
            return
        if self.phase == "rock":
            self._tick_rock(kind, uid)
        elif self.phase == "test":
            if self._tick_test(kind, uid):
                return
        if self.phase == "split" and not self.tester.moving and self._tick_split(kind, uid):
            return
        self._tick_run_all()

    def _tick_search(self, kind, uid):
        both = self.spools
        for s in both:
            if s.swing_paused:
                if not s.tag and not s.out:
                    s.swing = s.swing_paused
                    self._halt(s)
                s.swing_paused = 0
        if kind == READ_TAG and self._owner(uid) and self.last_kind == READ_NONE:
            # A settled tag that comes out of an empty antenna while only the other spool
            # turns belongs to that other spool.
            owner = self._owner(uid)
            other = self._other(owner)
            if not owner.dir and not owner.moving and other.dir and not other.tag:
                self._say("tag %s is really on %s" % (uid, other.name))
                owner.tag, other.tag = None, uid
        moving = [s for s in both if self._open(s) and not s.waiting]
        if len(moving) == 2 and (kind == READ_COLLISION or (kind == READ_TAG and not self._owner(uid))):
            for s in both:
                if s.swing:
                    s.swing_paused, s.swing = s.swing, 0
                    self._halt(s)
            tester = both[0] if both[0].pos <= both[1].pos else both[1]
            self.tester, self.waiter = tester, self._other(tester)
            self._halt(self.waiter)
            self.waiter.waiting = True
            self.phase, self.rock = "rock", 1
            self.candidate = uid if kind == READ_TAG else None
            self.swapped = False
            return True
        if len(moving) == 1 and kind == READ_TAG and not self._owner(uid):
            other = self._other(moving[0])
            # A spool is put in place before its filament reaches the sensor, so one inserted
            # before the antenna was last seen empty had no tag there then.
            came = self.last_kind == READ_NONE and not other.dir and not other.moving and (
                not other.inserted or other.inserted_at < self.last_none_at)
            if other.inserted and not other.tag and not other.no_tag and not other.out and not came:
                # The other spool stands but is new: its tag may be the one at the antenna.
                self.tester, self.waiter = moving[0], other
                other.waiting = True
                self.phase, self.rock, self.gone = "test", 0, 0
                self.candidate, self.swapped = uid, False
                self.test_from = moving[0].rot
                return True
            self._put(moving[0], uid)
            for s in both:
                if s.waiting and not s.tag:
                    s.waiting = False
            return True
        for s in both:
            if self._open(s) and s.pos >= self.limit and not s.dir and not s.moving and not s.swing and not s.waiting:
                s.swing, s.swing_from = 1, s.pos
                self._run(s, -1)
        for s in both:
            if s.swing and not s.dir and not s.moving and not s.waiting:
                self._run(s, -1 if s.swing == 1 else 1)
        for s in both:
            if s.swing == 1 and (s.pos <= s.swing_from - TURN * self.swing or s.pos <= self.min_pos):
                s.swing = 2
                self._run(s, 1)
            elif s.swing == 2 and s.pos >= s.swing_from:
                self._halt(s)
                s.swing = 0
                if not s.tag:
                    s.no_tag = True
                    self._say("%s: no tag" % (s.name,))
                for o in both:
                    o.waiting = False
        return False

    def _tick_rock(self, kind, uid):
        waiter = self.waiter
        if self.rock == 1 and not waiter.dir and not waiter.moving and waiter.pos < self.min_pos + self.rock_length:
            # Too close to the slot sensor: going back could pull the filament out of the gear.
            self.rock = 4
        elif self.rock == 1 and not waiter.dir and not waiter.moving:
            self.rock_base = waiter.pos
            self._run(waiter, -1, self.rock_speed)
            self.rock = 2
        elif self.rock == 2 and waiter.pos <= self.rock_base - self.rock_length:
            self._run(waiter, 1, self.rock_speed)
            self.rock = 3
        elif self.rock == 3 and (waiter.pos >= self.rock_base or not waiter.dir):
            self._halt(waiter)
            self.rock = 4
        elif self.rock == 4 and not waiter.moving:
            # The decision is only valid once the stopped spool has really stopped.
            self.rock = 0
            self.phase = "test"
            self.test_from = self.tester.rot
            self.gone = 0
            self.candidate = uid if kind == READ_TAG else None

    def _tick_test(self, kind, uid):
        tester, waiter = self.tester, self.waiter
        if kind == READ_COLLISION and self.candidate:
            self._halt(tester)
            tester.waiting, waiter.waiting = True, False
            self._run(waiter, 1)
            self.phase = "split"
            return True
        rolled = tester.rot - self.test_from >= self.test
        if kind == READ_TAG and not self._owner(uid):
            self.candidate = uid
        if kind == READ_NONE:
            self.gone += 1
            if self.gone >= 2:
                if self.candidate:
                    self._put(tester, self.candidate, SURE_LEFT)
                    waiter.waiting = False
                self.phase = "search"
                return True
        else:
            self.gone = 0
        if rolled and kind == READ_TAG:
            self._put(waiter, uid, SURE_LEFT)
            waiter.clearing, waiter.waiting = True, False
            self._run(waiter, 1)
            self.phase = "search"
            return True
        if tester.pos >= self.limit and not tester.dir and not tester.moving and kind != READ_NONE and not rolled:
            if self.swapped:
                waiter.waiting = False
                self.phase = "search"
                return True
            self.swapped = True
            self.tester, self.waiter = waiter, tester
            tester.waiting, waiter.waiting = True, False
            self.test_from = waiter.rot
        return False

    def _tick_split(self, kind, uid):
        tester, waiter = self.tester, self.waiter
        if waiter.pos >= self.limit and not waiter.dir and not waiter.moving and kind != READ_NONE:
            # The spool that was to take its tag away has no length left. Nothing is decided:
            # both go back to the search, where the swing at the limit finds each tag alone.
            tester.waiting = waiter.waiting = False
            self.phase = "search"
            return True
        if kind == READ_TAG and not self._owner(uid) and uid != self.candidate:
            self._put(tester, uid, SURE_GUESS)
            self._put(waiter, self.candidate, SURE_GUESS)
        elif kind == READ_TAG and uid == self.candidate:
            self._put(waiter, self.candidate, SURE_GUESS)
        elif kind == READ_NONE:
            if self.candidate:
                self._put(waiter, self.candidate, SURE_GUESS)
        else:
            return False
        tester.waiting = False
        self.phase = "search"
        return True

    def _tick_run_all(self):
        both = self.spools
        for s in both:
            if s.out:
                continue
            other = self._other(s)
            hold = s.tag and not s.at_antenna and (self._open(other) or (other.inserted and not other.started))
            if s.started and not s.dir and not s.moving and not s.waiting and not s.swing and not s.paused \
                    and s.pos < self.limit and not hold:
                self._run(s, 1)
            if hold and s.dir > 0 and not s.clearing:
                self._halt(s)
        inserted = [s for s in both if s.inserted]
        if inserted and self.phase == "search" and not self.done and all(
                s.started and (s.out or (s.pos >= self.limit and not s.dir and not s.moving and (s.tag or s.no_tag)))
                for s in inserted):
            self.done = True
            self._say("ready")


def note_reading(logic, kind, uid):
    """The printer learns that a settled tag stands at the antenna only from the reader: it reads
    that tag, or two tags at once while that tag was there a moment ago."""
    for spool in logic.spools:
        if not spool.tag:
            spool.at_antenna = False
        elif kind == READ_TAG:
            spool.at_antenna = uid == spool.tag
        elif kind == READ_NONE:
            spool.at_antenna = False
        elif kind == READ_COLLISION and not spool.dir:
            # A spool that stands keeps its tag where it was; a turning one may have moved it away.
            pass
