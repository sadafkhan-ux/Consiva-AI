"""How many jobs this worker should be running right now, decided from the machine and not from a number someone typed.

A worker used to run a fixed count of jobs at once (WORKER_CONCURRENCY=4). Under load that is wrong in both directions: on a big idle machine most of the CPU sat unused
while jobs waited in the queue (100 scans requested at once: median 86 s waiting for a slot while the CPU was 32% busy), and on a small or busy one four browsers can already
be too many (four real scans were measured at 790% of eight cores). Whether another scan fits depends on what the machine is doing at that moment, so the worker asks it.

The rule, applied every time the worker is about to take more work:
  * never take work while the machine's CPU is at or above the target (default 85%) or memory is short of what another job needs. When nothing is running, one job is always
    allowed, so a loaded machine still makes progress and a job is never starved;
  * take work in small steps (more when there is plenty of room, one at a time near the target) with a pause between steps long enough for the load of the last one to show
    up. A browser's cost arrives seconds after it starts, so a worker that looks at the CPU once and then takes eight jobs overshoots;
  * never run more jobs than the most the machine is worth (the CPU count, unless set) or than the database connections this process holds can serve.

The CPU figure is the whole machine's busy share (what other containers and processes are using counts, because a scan is slowed by them equally), or, when the container has a
CPU limit, its own usage as a share of that limit. Memory is the smaller of what the machine has available and what the container's limit leaves. Nothing here is a capacity
claim about any particular machine: a machine that cannot be read (no /proc, no cgroup) is not throttled, and says so once in the log.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Between two steps of taking work, long enough for the CPU figure to reflect the last step.
RAMP_INTERVAL_SECONDS = 1.0
#: A CPU reading is an average over about this long, so a moment of idleness between two browsers' bursts does not read as "room".
SMOOTHING_SECONDS = 4.0
#: Database connections this process keeps for itself (the pump's claim, the maintenance loop, stage writes) when it works out how many jobs it can serve.
RESERVED_DB_CONNECTIONS = 3


# ── reading the machine ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read()
    except OSError:
        return None


def parse_proc_stat(text: str | None) -> tuple[int, int] | None:
    """(total, idle) CPU ticks from the first line of /proc/stat (idle includes iowait: a CPU waiting for the disk is not busy)."""
    if not text:
        return None
    try:
        parts = [int(x) for x in text.splitlines()[0].split()[1:]]
        return sum(parts[:8]), parts[3] + (parts[4] if len(parts) > 4 else 0)
    except (ValueError, IndexError):
        return None


def parse_cpu_max(text: str | None) -> float | None:
    """The CPUs a cgroup may use (quota / period), or None when it has no limit."""
    if not text:
        return None
    try:
        quota, period = text.split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (ValueError, IndexError):
        return None


def parse_cpu_usage_usec(text: str | None) -> int | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("usage_usec"):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def parse_meminfo_available_mb(text: str | None) -> float | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1]) / 1024
            except (ValueError, IndexError):
                return None
    return None


def parse_cgroup_memory_left_mb(limit: str | None, current: str | None) -> float | None:
    """What a memory limit on this container leaves, or None when there is no limit."""
    if not limit or not current or limit.strip() == "max":
        return None
    try:
        return (int(limit) - int(current)) / (1024 * 1024)
    except ValueError:
        return None


class HostLoad:
    """CPU busy share (smoothed) and memory available, read from /proc and the cgroup. Readers are injectable so the logic is tested without a machine."""

    def __init__(self, read: Callable[[str], str | None] = _read, clock: Callable[[], float] = time.monotonic):
        self._read, self._clock = read, clock
        self._cpu_limit = parse_cpu_max(read("/sys/fs/cgroup/cpu.max"))
        self._last: tuple[float, float] | None = None       # (clock, ticks or usec at that time)
        self._last_total_idle: tuple[int, int] | None = None
        self._ewma: float | None = None

    def _sample_cpu(self) -> float | None:
        now = self._clock()
        if self._cpu_limit is not None:                      # the container has a CPU limit: its own usage against that limit
            usage = parse_cpu_usage_usec(self._read("/sys/fs/cgroup/cpu.stat"))
            if usage is None:
                return None
            prev, self._last = self._last, (now, float(usage))
            if prev is None or now - prev[0] <= 0:
                return None
            return 100.0 * ((usage - prev[1]) / 1e6) / ((now - prev[0]) * self._cpu_limit)
        cur = parse_proc_stat(self._read("/proc/stat"))
        if cur is None:
            return None
        prev_ti, self._last_total_idle = self._last_total_idle, cur
        if prev_ti is None or cur[0] - prev_ti[0] <= 0:
            return None
        return 100.0 * (1 - (cur[1] - prev_ti[1]) / (cur[0] - prev_ti[0]))

    def cpu_busy_percent(self) -> float | None:
        """Smoothed busy share, or None when it cannot be read yet or at all."""
        sample = self._sample_cpu()
        if sample is None:
            return self._ewma
        sample = max(0.0, min(100.0, sample))
        self._ewma = sample if self._ewma is None else self._ewma + 0.5 * (sample - self._ewma)
        return self._ewma

    def memory_available_mb(self) -> float | None:
        host = parse_meminfo_available_mb(self._read("/proc/meminfo"))
        cgroup = parse_cgroup_memory_left_mb(self._read("/sys/fs/cgroup/memory.max"), self._read("/sys/fs/cgroup/memory.current"))
        known = [v for v in (host, cgroup) if v is not None]
        return min(known) if known else None


# ── the decision ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def max_slots(configured: int, *, cpu_count: int | None, db_pool_total: int) -> int:
    """The most jobs this process will ever run at once. `configured` (WORKER_CONCURRENCY) is used when it is above 0; otherwise the CPU count. Either way it is bounded by the
    database connections this process holds, because a job holding no connection cannot record its progress."""
    wanted = configured if configured > 0 else max(2, cpu_count or 2)
    return max(1, min(wanted, db_pool_total - RESERVED_DB_CONNECTIONS))


@dataclass
class Governor:
    slots: int
    target_cpu: float
    memory_per_job_mb: float
    min_free_memory_mb: float
    load: HostLoad
    clock: Callable[[], float] = time.monotonic
    _last_claim: float = -1e9
    last_reason: str = "starting"

    def claimable(self, in_flight: int) -> int:
        """How many more jobs to take right now (0 to slots - in_flight)."""
        room = self.slots - in_flight
        if room <= 0:
            self.last_reason = "all slots busy"
            return 0
        now = self.clock()
        cpu = self.load.cpu_busy_percent()
        mem = self.load.memory_available_mb()

        allowed = room
        reason = "room"
        if cpu is not None and cpu >= self.target_cpu:
            allowed, reason = 0, f"CPU {cpu:.0f}% is at the {self.target_cpu:.0f}% target"
        elif mem is not None:
            by_memory = int((mem - self.min_free_memory_mb) // self.memory_per_job_mb)
            if by_memory < allowed:
                allowed, reason = max(0, by_memory), f"memory: {mem:.0f} MB available"
        if allowed > 0 and in_flight > 0 and now - self._last_claim < RAMP_INTERVAL_SECONDS:
            allowed, reason = 0, "pausing between steps so the last job's load shows"
        if allowed > 0:
            headroom = (self.target_cpu - cpu) if cpu is not None else self.target_cpu
            allowed = min(allowed, 1 + int(max(0.0, headroom - 10) // 20))      # one near the target, up to four when the machine is mostly idle
        if in_flight == 0:
            allowed = max(1, allowed)                                # a loaded machine still makes progress: an idle worker always takes one job
        self.last_reason = reason
        if allowed > 0:
            self._last_claim = now
        return min(allowed, room)


class FixedSlots:
    """A plain count of jobs and no throttling: what the pump accepts when given a number instead of a Governor (the way it was driven before the machine was consulted)."""

    class _NoLoad:
        def cpu_busy_percent(self):
            return None

        def memory_available_mb(self):
            return None

    def __init__(self, slots: int):
        self.slots = max(1, slots)
        self.load = self._NoLoad()
        self.last_reason = "fixed number of slots"

    def claimable(self, in_flight: int) -> int:
        return max(0, self.slots - in_flight)


def governor_for(settings, *, cpu_count: int | None = None) -> Governor:
    slots = max_slots(settings.worker_concurrency, cpu_count=cpu_count or os.cpu_count(), db_pool_total=settings.db_pool_size + settings.db_max_overflow)
    load = HostLoad()
    if load.cpu_busy_percent() is None and load.memory_available_mb() is None:
        logger.warning("The load of this machine cannot be read (/proc and cgroup unavailable): the worker runs up to %d jobs without CPU or memory throttling", slots)
    return Governor(slots=slots, target_cpu=float(settings.worker_target_cpu_percent), memory_per_job_mb=float(settings.worker_memory_per_job_mb),
                    min_free_memory_mb=float(settings.worker_min_free_memory_mb), load=load)
