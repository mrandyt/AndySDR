"""Putting together the frames two receivers heard of the same pass."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from andysdr import satmerge

ASM = satmerge.ASM


def frame(vcid, counter, fill=b"\x00"):
    body = bytearray(1024)
    body[0:4] = ASM
    body[4] = 0x40
    body[5] = vcid & 0x3F
    body[6:9] = counter.to_bytes(3, "big")
    body[9:] = (fill * ((1024 - 9) // len(fill) + 1))[:1024 - 9]
    return bytes(body)


def write(path, frames):
    with open(path, "wb") as fh:
        fh.write(b"".join(frames))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def p(self, name):
        return os.path.join(self.d, name)

    def test_union_of_what_each_receiver_heard(self):
        a = [frame(5, n) for n in (1, 2, 3, 10, 11)]
        b = [frame(5, n) for n in (3, 4, 5, 10, 20)]
        write(self.p("a.cadu"), a)
        write(self.p("b.cadu"), b)
        rep = satmerge.merge([self.p("a.cadu"), self.p("b.cadu")], self.p("m.cadu"))
        self.assertEqual(rep["frames"], 8)          # 1,2,3,4,5,10,11,20
        self.assertEqual(rep["best_single"], 5)
        self.assertEqual(rep["gained"], 3)
        out = open(self.p("m.cadu"), "rb").read()
        self.assertEqual(len(out), 8 * 1024)
        nums = [int.from_bytes(out[i + 6:i + 9], "big") for i in range(0, len(out), 1024)]
        self.assertEqual(nums, sorted(nums))

    def test_channels_are_kept_apart(self):
        write(self.p("a.cadu"), [frame(5, 1), frame(6, 1)])
        write(self.p("b.cadu"), [frame(5, 1), frame(7, 1)])
        rep = satmerge.merge([self.p("a.cadu"), self.p("b.cadu")], self.p("m.cadu"))
        self.assertEqual(rep["frames"], 3)          # same number, three different channels

    def test_a_counter_that_rolls_over_mid_pass(self):
        top = satmerge.COUNTER_WRAP
        a = [frame(5, top - 3), frame(5, top - 2)]
        b = [frame(5, 0), frame(5, 1)]
        write(self.p("a.cadu"), a)
        write(self.p("b.cadu"), b)
        satmerge.merge([self.p("a.cadu"), self.p("b.cadu")], self.p("m.cadu"))
        out = open(self.p("m.cadu"), "rb").read()
        nums = [int.from_bytes(out[i + 6:i + 9], "big") for i in range(0, len(out), 1024)]
        self.assertEqual(nums, [top - 3, top - 2, 0, 1])   # the high numbers came first

    def test_rubbish_in_a_file_is_ignored(self):
        write(self.p("a.cadu"), [frame(5, 1)])
        with open(self.p("b.cadu"), "wb") as fh:
            fh.write(b"this is not a frame" * 80)
        self.assertEqual(satmerge.read_frames(self.p("b.cadu")), {})
        self.assertIsNone(satmerge.merge([self.p("a.cadu"), self.p("b.cadu")], self.p("m.cadu")))

    def test_one_receiver_alone_is_not_a_merge(self):
        write(self.p("a.cadu"), [frame(5, 1), frame(5, 2)])
        self.assertIsNone(satmerge.merge([self.p("a.cadu")], self.p("m.cadu")))
        self.assertFalse(os.path.exists(self.p("m.cadu")))

    def test_a_missing_file_does_not_stop_it(self):
        self.assertEqual(satmerge.read_frames(self.p("nope.cadu")), {})

    def test_nothing_is_written_when_there_is_nothing_to_write(self):
        self.assertIsNone(satmerge.merge([], self.p("m.cadu")))
        self.assertFalse(os.path.exists(self.p("m.cadu")))

    def test_the_line_for_the_pass_log(self):
        self.assertEqual(satmerge.describe(None), "")
        line = satmerge.describe({"frames": 100, "gained": 25, "gained_pct": 33.3})
        self.assertIn("100 frames", line)
        self.assertIn("25 more", line)


class MergerTests(unittest.TestCase):
    """The part that waits for both receivers before it merges."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.logs = []
        self.m = satmerge.Merger(self.d, lambda *a, **k: None, {}, log_cb=self.logs.append)

    def _dir(self, name, frames):
        d = os.path.join(self.d, name)
        os.makedirs(d, exist_ok=True)
        write(os.path.join(d, "meteor_m2-x_lrpt.cadu"), frames)
        return d

    def test_it_waits_for_the_second_receiver(self):
        self.m.expect("pass1")
        d1 = self._dir("lrpt_a", [frame(5, 1)])
        self.m.report("pass1", d1, "rx1")
        self.assertIn("pass1", self.m._waiting)             # still waiting for the other one

    def test_a_pass_it_was_not_told_about_is_left_alone(self):
        self.m.report("other", self._dir("lrpt_c", [frame(5, 1)]), "rx1")
        self.assertEqual(self.logs, [])

    def test_merging_happens_once_both_have_reported(self):
        self.m.expect("pass2")
        d1 = self._dir("lrpt_d", [frame(5, n) for n in (1, 2)])
        d2 = self._dir("lrpt_e", [frame(5, n) for n in (2, 3)])
        self.m.report("pass2", d1, "rx1")
        self.m.report("pass2", d2, "rx2")
        self.assertNotIn("pass2", self.m._waiting)          # it is being worked on, not waiting
        import time
        for _ in range(50):
            if self.logs:
                break
            time.sleep(0.05)
        self.assertTrue(any("3 frames" in t for t in self.logs), self.logs)

    def test_both_receivers_reporting_one_folder_is_not_a_merge(self):
        self.m.expect("pass4")
        d = self._dir("lrpt_same", [frame(5, 1), frame(5, 2)])
        self.m.report("pass4", d, "rx1")
        self.m.report("pass4", d, "rx2")
        import time
        for _ in range(40):
            if self.logs:
                break
            time.sleep(0.05)
        self.assertTrue(any("same file" in t for t in self.logs), self.logs)
        self.assertFalse(os.path.exists(os.path.join(self.d, "lrpt_same_merged")))

    def _wait(self, n=50):
        import time
        for _ in range(n):
            if self.logs:
                break
            time.sleep(0.05)

    def test_a_receiver_that_heard_nothing_leaves_no_empty_folder(self):
        """The real case: the second receiver decoded a file, but it came out empty.

        Every merged folder on Andy's machine was one of these - created, then left empty with nothing in it
        to say why.  The pass should say plainly which receiver heard nothing and make no folder at all.
        """
        self.m.expect("pass5")
        d1 = self._dir("lrpt_f", [frame(5, n) for n in (1, 2, 3)])
        d2 = self._dir("lrpt_f_rx2", [])                    # decoded, but not one frame in it
        self.m.report("pass5", d1, "rx1")
        self.m.report("pass5", d2, "rx2")
        self._wait()
        self.assertFalse(os.path.exists(os.path.join(self.d, "lrpt_f_merged")), "an empty merged folder was left")
        self.assertTrue(any("Nothing to merge" in t for t in self.logs), self.logs)
        self.assertTrue(any("second receiver" in t for t in self.logs), self.logs)

    def test_neither_receiver_heard_anything(self):
        self.m.expect("pass6")
        d1 = self._dir("lrpt_g", [])
        d2 = self._dir("lrpt_g_rx2", [])
        self.m.report("pass6", d1, "rx1")
        self.m.report("pass6", d2, "rx2")
        self._wait()
        self.assertFalse(os.path.exists(os.path.join(self.d, "lrpt_g_merged")))
        self.assertTrue(any("main receiver" in t and "second receiver" in t for t in self.logs), self.logs)

    def test_a_folder_with_no_decoded_file_at_all(self):
        self.m.expect("pass7")
        d1 = self._dir("lrpt_h", [frame(5, 1)])
        d2 = os.path.join(self.d, "lrpt_h_rx2")             # the decode never ran
        os.makedirs(d2, exist_ok=True)
        self.m.report("pass7", d1, "rx1")
        self.m.report("pass7", d2, "rx2")
        self._wait()
        self.assertFalse(os.path.exists(os.path.join(self.d, "lrpt_h_merged")))
        self.assertTrue(any("Nothing to merge" in t for t in self.logs), self.logs)

    def test_two_receivers_that_both_heard_something_still_merge(self):
        self.m.expect("pass8")
        d1 = self._dir("lrpt_i", [frame(5, n) for n in (1, 2)])
        d2 = self._dir("lrpt_i_rx2", [frame(5, n) for n in (2, 3, 4)])
        self.m.report("pass8", d1, "rx1")
        self.m.report("pass8", d2, "rx2")
        self._wait()
        self.assertTrue(any("4 frames" in t for t in self.logs), self.logs)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "lrpt_i_merged", "meteor_m2-x_lrpt.cadu")))

    def test_the_wait_is_measured_from_the_first_receiver_not_the_pass(self):
        """A long pass plus a slow decode used to use up the whole wait before either receiver reported."""
        self.m.expect("pass9")
        self.m._waiting["pass9"]["started"] = 0.0           # as if the pass began hours ago
        d1 = self._dir("lrpt_j", [frame(5, 1), frame(5, 2)])
        d2 = self._dir("lrpt_j_rx2", [frame(5, 3)])
        self.m.report("pass9", d1, "rx1")
        self.assertIn("pass9", self.m._waiting, "it gave up before the second receiver had a chance")
        self.m.report("pass9", d2, "rx2")
        self._wait()
        self.assertTrue(any("3 frames" in t for t in self.logs), self.logs)

    def test_it_still_gives_up_on_a_receiver_that_takes_too_long(self):
        self.m.expect("pass10")
        d1 = self._dir("lrpt_k", [frame(5, 1)])
        self.m.report("pass10", d1, "rx1")
        self.m._waiting["pass10"]["first"] = 0.0            # as if the first receiver finished hours ago
        d2 = self._dir("lrpt_k_rx2", [frame(5, 2)])
        self.m.report("pass10", d2, "rx2")
        self.assertNotIn("pass10", self.m._waiting)
        self.assertFalse(os.path.exists(os.path.join(self.d, "lrpt_k_merged")))

    def test_drop_forgets_a_pass(self):
        self.m.expect("pass3")
        self.m.drop("pass3")
        self.assertNotIn("pass3", self.m._waiting)


if __name__ == "__main__":
    unittest.main(verbosity=1)
