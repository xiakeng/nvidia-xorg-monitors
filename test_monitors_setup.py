#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path

import monitors_setup as setup


PROJECT = Path(__file__).resolve().parent


class MonitorsSetupTests(unittest.TestCase):
    def test_example_config_renders_idempotently(self) -> None:
        config = setup.load_config(PROJECT / "config.toml")
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            rendered_first = setup.render(config, Path(first))
            rendered_second = setup.render(config, Path(second))
            self.assertEqual(rendered_first.keys(), rendered_second.keys())
            for key in rendered_first:
                self.assertEqual(
                    rendered_first[key].read_bytes(), rendered_second[key].read_bytes()
                )

    def test_example_edids_have_valid_block_checksums(self) -> None:
        config = setup.load_config(PROJECT / "config.toml")
        for monitor in config.monitors:
            edid = setup.generate_edid(monitor)
            self.assertEqual(len(edid), 256)
            for offset in range(0, len(edid), 128):
                self.assertEqual(sum(edid[offset : offset + 128]) % 256, 0)

    def test_xorg_config_is_isolated_from_xrdp(self) -> None:
        config = setup.load_config(PROJECT / "config.toml")
        xorg = setup.render_xorg(config)
        self.assertIn('Driver "nvidia"', xorg)
        self.assertIn('ConnectedMonitor" "DFP-1,DFP-3', xorg)
        self.assertNotIn("xrdpdev", xorg)
        self.assertNotIn("/etc/X11/xrdp", xorg)

    def test_all_requested_modes_are_present_in_model(self) -> None:
        config = setup.load_config(PROJECT / "config.toml")
        expected = {
            "1920x1080@60",
            "1920x1080@120",
            "1920x1200@60",
            "1920x1200@120",
            "2560x1440@60",
            "2560x1440@120",
            "3840x2160@60",
            "3840x2160@120",
        }
        for monitor in config.monitors:
            self.assertEqual({mode.label for mode in monitor.modes}, expected)


if __name__ == "__main__":
    unittest.main()

