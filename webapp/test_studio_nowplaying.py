"""Now Playing: locating the current playlist item from supervisor state."""
import os
import tempfile
import unittest

from webapp.studio import nowplaying as np_


class LocateTests(unittest.TestCase):
    def test_locate_walks_items_and_loops(self):
        d = [10.0, 20.0, 5.0]
        self.assertEqual(np_.locate(d, 0)[0], 0)
        self.assertEqual(np_.locate(d, 9.9)[0], 0)
        self.assertEqual(np_.locate(d, 10.0)[0], 1)
        self.assertEqual(np_.locate(d, 34.9)[0], 2)
        self.assertEqual(np_.locate(d, 35.0)[0], 0)           # playlist loops
        self.assertEqual(np_.locate(d, 35.0 * 100 + 12)[0], 1)  # long uptime
        self.assertIsNone(np_.locate([], 5))
        self.assertIsNone(np_.locate([10, 0], 5))


class CurrentTitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = self.tmp.name

    def write(self, name, text):
        with open(os.path.join(self.run, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def setup_playlist(self, state="LIVE", since=1000, titles=("Intro", "Main show", "Outro")):
        self.write("pl.concat.txt", "".join("file '/cache/%d.mp4'\n" % i for i in range(len(titles))))
        self.write("pl.titles", "".join(t + "\n" for t in titles))
        self.write("playlist-pl.status", "STATE=%s\nUPDATED=%d\n" % (state, since))

    DUR = {"/cache/0.mp4": 10.0, "/cache/1.mp4": 20.0, "/cache/2.mp4": 5.0}

    def title(self, now):
        return np_.current_title(self.run, "pl", now, duration_fn=self.DUR.get)

    def test_updates_as_playlist_advances(self):
        self.setup_playlist(since=1000)
        self.assertEqual(self.title(1003), "Intro")
        self.assertEqual(self.title(1015), "Main show")
        self.assertEqual(self.title(1032), "Outro")
        self.assertEqual(self.title(1037), "Intro")  # looped

    def test_restart_resets_position(self):
        self.setup_playlist(since=1000)
        self.assertEqual(self.title(1015), "Main show")
        self.write("playlist-pl.status", "STATE=LIVE\nUPDATED=1020\n")  # supervisor restarted ffmpeg
        self.assertEqual(self.title(1022), "Intro")

    def test_unavailable_metadata_is_empty_never_a_guess(self):
        self.setup_playlist(state="RECONNECTING")
        self.assertEqual(self.title(1003), "")
        self.setup_playlist(state="STOPPED")
        self.assertEqual(self.title(1003), "")
        self.setup_playlist()
        self.write("pl.titles", "only one\n")  # mismatched
        self.assertEqual(self.title(1003), "")
        self.setup_playlist()
        self.assertEqual(np_.current_title(self.run, "pl", 1003, duration_fn=lambda f: None), "")
        self.assertEqual(np_.current_title(self.run, "missing", 1003, duration_fn=self.DUR.get), "")

    def test_widget_renders_title(self):
        from datetime import datetime, timezone
        from webapp.studio import widgets
        from webapp.studio.model import Overlay
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        o = Overlay("nowplaying", text="Now:")
        self.assertEqual(widgets.render_text(o, now, "Main show"), "Now: Main show")
        self.assertEqual(widgets.render_text(o, now, "").strip(), "Now:")


if __name__ == "__main__":
    unittest.main()
