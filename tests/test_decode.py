import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import temper as tp  # noqa: E402


class TestDecode(unittest.TestCase):
    def test_temperhum(self):
        # recorded on hardware (TEMPERHUM1V1.0); reference hygrometer ~23.8 %RH
        q = tp.decode(0x7402, bytes.fromhex("800409f202d7a0a4"))
        self.assertAlmostEqual(q["temperature"], 25.46)
        self.assertAlmostEqual(q["humidity"], 23.8, delta=0.1)
        q = tp.decode(0x7402, bytes.fromhex("80040a03063e0000"))   # breathed on
        self.assertGreater(q["humidity"], 50)

    def test_temper(self):
        # TEMPerV1.4: 1/256 degC (warmed by hand 25.25 -> 26.88 during the check)
        self.assertAlmostEqual(tp.decode(0x7401, bytes.fromhex("80021900092 9a403".replace(" ", "")))["temperature"], 25.0)
        self.assertAlmostEqual(tp.decode(0x7401, bytes.fromhex("8002ff80000000 00".replace(" ", "")))["temperature"], -0.5)

    def test_th1000_without_probe(self):
        q = tp.decode(0x7403, bytes.fromhex("800616500fff0fff"))
        self.assertAlmostEqual(q["temperature"], 22.31, places=2)
        self.assertNotIn("tc1_raw", q)
        q = tp.decode(0x7403, bytes.fromhex("8006165001230fff"))
        self.assertEqual(q["tc1_raw"], 0x0123)

    def test_humidity_limits(self):
        self.assertEqual(tp.sht1x_humidity(0, 25), 0.0)
        self.assertEqual(tp.sht1x_humidity(4095, 25), 100.0)


if __name__ == "__main__":
    unittest.main()
