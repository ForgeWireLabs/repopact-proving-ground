import unittest

from unitconv import convert_length, convert_temperature


class TemperatureTests(unittest.TestCase):
    def test_celsius_to_fahrenheit(self):
        self.assertAlmostEqual(convert_temperature(100.0, "C", "F"), 212.0)

    def test_fahrenheit_to_celsius(self):
        self.assertAlmostEqual(convert_temperature(32.0, "F", "C"), 0.0)

    def test_celsius_to_kelvin(self):
        self.assertAlmostEqual(convert_temperature(0.0, "C", "K"), 273.15)

    def test_roundtrip(self):
        self.assertAlmostEqual(convert_temperature(convert_temperature(37.0, "C", "F"), "F", "C"), 37.0)

    def test_unknown_unit(self):
        with self.assertRaises(ValueError):
            convert_temperature(1.0, "C", "Z")


class LengthTests(unittest.TestCase):
    def test_metres_to_feet(self):
        self.assertAlmostEqual(convert_length(1.0, "m", "ft"), 3.28084, places=4)

    def test_mile_to_km(self):
        self.assertAlmostEqual(convert_length(1.0, "mi", "km"), 1.609344, places=6)

    def test_roundtrip(self):
        self.assertAlmostEqual(convert_length(convert_length(5.0, "m", "ft"), "ft", "m"), 5.0)


if __name__ == "__main__":
    unittest.main()
