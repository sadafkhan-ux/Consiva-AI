"""The worker sizes itself from the machine: how much it takes, when it holds back, and that it can never starve a job."""

import pytest

from app.config import get_settings
from app.jobs import capacity
from app.jobs.capacity import Governor, HostLoad, max_slots


class Machine:
    """A machine whose load the test controls, and a clock it advances."""

    def __init__(self, cpu=10.0, mem=6000.0):
        self.cpu, self.mem, self.now = cpu, mem, 1000.0

    def load(self):
        outer = self

        class Load:
            def cpu_busy_percent(self):
                return outer.cpu

            def memory_available_mb(self):
                return outer.mem
        return Load()

    def governor(self, slots=8, target=85.0):
        return Governor(slots=slots, target_cpu=target, memory_per_job_mb=700.0, min_free_memory_mb=768.0, load=self.load(), clock=lambda: self.now)


class TestWhatItTakes:
    def test_an_idle_machine_fills_its_slots_in_a_few_steps_not_all_at_once(self):
        m = Machine(cpu=5)
        g = m.governor(slots=8)
        running, steps = 0, 0
        while running < 8 and steps < 10:
            take = g.claimable(running)
            assert take <= 4, "a step never takes more than four"
            running += take
            steps += 1
            m.now += 1.1
        assert running == 8 and steps >= 2        # four, then four, a second apart

    def test_it_never_takes_two_steps_within_the_pause_that_lets_the_last_jobs_load_show(self):
        m = Machine(cpu=5)
        g = m.governor()
        assert g.claimable(1) > 0
        assert g.claimable(2) == 0 and "pausing" in g.last_reason       # no time has passed
        m.now += 1.1
        assert g.claimable(2) > 0

    def test_near_the_target_it_takes_one_at_a_time(self):
        m = Machine(cpu=78)
        g = m.governor()
        m.now += 2
        assert g.claimable(3) == 1

    def test_at_or_above_the_target_it_takes_nothing_more(self):
        for cpu in (85, 93, 100):
            m = Machine(cpu=cpu)
            g = m.governor()
            m.now += 2
            assert g.claimable(3) == 0, cpu
            assert "target" in g.last_reason

    def test_it_never_runs_more_than_its_slots(self):
        m = Machine(cpu=1)
        g = m.governor(slots=3)
        m.now += 2
        assert g.claimable(3) == 0
        assert g.claimable(2) == 1


class TestMemory:
    def test_memory_limits_how_many_more(self):
        m = Machine(cpu=5, mem=768 + 700 * 2 + 10)           # room for two more, no more
        g = m.governor(slots=8)
        m.now += 2
        assert g.claimable(1) == 2
        assert "memory" in g.last_reason

    def test_with_no_memory_to_spare_it_takes_nothing_while_something_runs(self):
        m = Machine(cpu=5, mem=900)
        g = m.governor()
        m.now += 2
        assert g.claimable(2) == 0


class TestItCanNeverStarveAJob:
    @pytest.mark.parametrize("cpu,mem", [(100, 5000), (5, 100), (100, 100)])
    def test_an_idle_worker_always_takes_one_job_however_loaded_the_machine(self, cpu, mem):
        g = Machine(cpu=cpu, mem=mem).governor()
        assert g.claimable(0) == 1

    def test_a_machine_that_cannot_be_read_is_not_throttled(self):
        class Blind:
            def cpu_busy_percent(self):
                return None

            def memory_available_mb(self):
                return None
        m = Machine()
        g = Governor(slots=4, target_cpu=85, memory_per_job_mb=700, min_free_memory_mb=768, load=Blind(), clock=lambda: m.now)
        m.now += 2
        assert g.claimable(0) >= 1
        m.now += 2
        assert g.claimable(1) > 0


class TestSlots:
    def test_the_cpu_count_when_not_set(self):
        assert max_slots(0, cpu_count=8, db_pool_total=100) == 8
        assert max_slots(0, cpu_count=64, db_pool_total=100) == 64
        assert max_slots(0, cpu_count=None, db_pool_total=100) == 2
        assert max_slots(0, cpu_count=1, db_pool_total=100) == 2        # always worth two: a job waiting on the network leaves the CPU free

    def test_a_configured_number_wins_over_the_cpu_count(self):
        assert max_slots(3, cpu_count=64, db_pool_total=100) == 3

    def test_the_database_connections_this_process_holds_bound_it_either_way(self):
        assert max_slots(0, cpu_count=64, db_pool_total=15) == 15 - capacity.RESERVED_DB_CONNECTIONS
        assert max_slots(40, cpu_count=64, db_pool_total=15) == 12
        assert max_slots(0, cpu_count=64, db_pool_total=2) == 1

    def test_the_governor_is_built_from_the_settings(self):
        s = get_settings().model_copy(update={"worker_concurrency": 0, "db_pool_size": 10, "db_max_overflow": 5})
        g = capacity.governor_for(s, cpu_count=6)
        assert g.slots == 6
        assert g.target_cpu == 85.0


class TestReadingTheMachine:
    PROC1 = "cpu  1000 0 500 8000 100 0 0 0 0 0\ncpu0 1 1 1 1 1\n"
    PROC2 = "cpu  1600 0 800 8100 100 0 0 0 0 0\ncpu0 1 1 1 1 1\n"       # +900 busy, +100 idle

    def files(self, mapping):
        return lambda path: mapping.get(path)

    def test_the_whole_machines_busy_share_comes_from_proc_stat(self):
        state = {"/proc/stat": self.PROC1}
        t = [0.0]
        load = HostLoad(read=self.files(state), clock=lambda: t[0])
        assert load.cpu_busy_percent() is None                   # the first reading has nothing to compare with
        state["/proc/stat"] = self.PROC2
        t[0] = 1
        assert load.cpu_busy_percent() == pytest.approx(90.0)

    def test_the_reading_is_smoothed_so_one_quiet_second_between_bursts_is_not_room(self):
        state = {"/proc/stat": self.PROC1}
        load = HostLoad(read=self.files(state), clock=lambda: 0.0)
        load.cpu_busy_percent()
        state["/proc/stat"] = self.PROC2
        high = load.cpu_busy_percent()
        state["/proc/stat"] = "cpu  1600 0 800 9100 100 0 0 0 0 0\n"          # then a second of pure idle
        after = load.cpu_busy_percent()
        assert high == pytest.approx(90.0)
        assert 40 < after < 90

    def test_a_container_with_a_cpu_limit_is_measured_against_its_own_limit(self):
        state = {"/sys/fs/cgroup/cpu.max": "200000 100000", "/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n"}
        t = [0.0]
        load = HostLoad(read=self.files(state), clock=lambda: t[0])
        load.cpu_busy_percent()
        state["/sys/fs/cgroup/cpu.stat"] = "usage_usec 2500000\n"            # 1.5 CPU-seconds in 1 s, against a limit of 2 CPUs
        t[0] = 1.0
        assert load.cpu_busy_percent() == pytest.approx(75.0)

    def test_memory_is_the_smaller_of_the_machine_and_the_container_limit(self):
        state = {"/proc/meminfo": "MemTotal: 8000000 kB\nMemAvailable: 6144000 kB\n", "/sys/fs/cgroup/memory.max": str(2048 * 1024 * 1024),
                 "/sys/fs/cgroup/memory.current": str(1024 * 1024 * 1024)}
        assert HostLoad(read=self.files(state)).memory_available_mb() == pytest.approx(1024.0)
        state["/sys/fs/cgroup/memory.max"] = "max"
        assert HostLoad(read=self.files(state)).memory_available_mb() == pytest.approx(6000.0)

    def test_unreadable_files_give_nothing_not_an_error(self):
        load = HostLoad(read=lambda p: None)
        assert load.cpu_busy_percent() is None
        assert load.memory_available_mb() is None
        assert capacity.parse_proc_stat("garbage") is None
        assert capacity.parse_meminfo_available_mb("nothing") is None


class TestThePump:
    def test_the_worker_asks_the_governor_instead_of_using_a_fixed_count(self):
        import inspect

        from app.jobs import worker
        src = inspect.getsource(worker._job_pump)
        assert "governor.claimable(len(in_flight))" in src
        assert "governor_for" in inspect.getsource(worker.run_forever)
