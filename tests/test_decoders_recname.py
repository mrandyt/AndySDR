"""Two receivers recording the same pass must not write to one file."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RecordingNameTests(unittest.TestCase):
    def test_each_receiver_gets_its_own_name(self):
        from andysdr import decoders
        d = tempfile.mkdtemp()
        with mock.patch.object(decoders, "data_dir", lambda: d):
            os.makedirs(os.path.join(d, "recordings"), exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            names = []
            for tag in ("", "rx2"):
                suffix = f"_{tag}" if tag else ""
                path = os.path.join(d, "recordings", f"lrpt_{ts}_240k{suffix}.wav")
                n = 1
                while os.path.exists(path):
                    path = os.path.join(d, "recordings", f"lrpt_{ts}_240k{suffix}_{n}.wav")
                    n += 1
                open(path, "wb").close()
                names.append(path)
            self.assertNotEqual(names[0], names[1])
            self.assertIn("rx2", os.path.basename(names[1]))

    def test_a_second_recording_in_the_same_second_does_not_overwrite(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "recordings"), exist_ok=True)
        ts = "20261126_143710"
        made = []
        for _ in range(3):
            path = os.path.join(d, "recordings", f"lrpt_{ts}_240k_rx2.wav")
            n = 1
            while os.path.exists(path):
                path = os.path.join(d, "recordings", f"lrpt_{ts}_240k_rx2_{n}.wav")
                n += 1
            open(path, "wb").close()
            made.append(path)
        self.assertEqual(len(set(made)), 3)

    def test_the_manager_takes_a_tag(self):
        from andysdr.decoders import DecoderManager
        m = DecoderManager({}, lambda *a: None, tag="rx2")
        self.assertEqual(m.tag, "rx2")
        self.assertEqual(DecoderManager({}, lambda *a: None).tag, "")


if __name__ == "__main__":
    unittest.main(verbosity=1)
