import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "nmea_reader.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("telematics_edge_nmea_reader", MODULE_PATH)
nmea_reader = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(nmea_reader)


class TcpTargetParsingTests(unittest.TestCase):
    def test_valid_tcp_host_and_port_remains_unchanged(self) -> None:
        reader = nmea_reader.NMEAReader(port="tcp://gps-multiplexer:2947")
        host, port = reader._parse_tcp_target(reader.port)
        self.assertEqual(host, "gps-multiplexer")
        self.assertEqual(port, 2947)

    def test_missing_port_after_colon_falls_back_to_default(self) -> None:
        reader = nmea_reader.NMEAReader(port="tcp://gps-multiplexer:")
        with self.assertLogs(nmea_reader.logger, level="WARNING") as captured:
            host, port = reader._parse_tcp_target(reader.port)
        self.assertEqual(host, "gps-multiplexer")
        self.assertEqual(port, 2947)
        self.assertIn("tcp://gps-multiplexer:", captured.output[0])

    def test_slash_in_port_value_falls_back_to_default(self) -> None:
        reader = nmea_reader.NMEAReader(port="tcp://gps-multiplexer:2947/")
        with self.assertLogs(nmea_reader.logger, level="WARNING") as captured:
            host, port = reader._parse_tcp_target(reader.port)
        self.assertEqual(host, "gps-multiplexer")
        self.assertEqual(port, 2947)
        self.assertIn("tcp://gps-multiplexer:2947/", captured.output[0])

    def test_non_numeric_port_value_falls_back_to_default(self) -> None:
        reader = nmea_reader.NMEAReader(port="tcp://gps-multiplexer:notaport")
        with self.assertLogs(nmea_reader.logger, level="WARNING") as captured:
            host, port = reader._parse_tcp_target(reader.port)
        self.assertEqual(host, "gps-multiplexer")
        self.assertEqual(port, 2947)
        self.assertIn("tcp://gps-multiplexer:notaport", captured.output[0])


if __name__ == "__main__":
    unittest.main()
