"""Desktop smoke tests; skipped on machines without Tk or a display."""

import socket
import time
import unittest

from quake.engine import Listener
from quake.transport import Endpoint


class DesktopTest(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:
            self.skipTest(f"Tk / desktop unavailable: {exc}")
        from quake.gui import MirthQuakeApp
        self.app = MirthQuakeApp(self.root)
        self.addCleanup(self.root.destroy)

    def test_editor_inspector_and_single_send(self):
        self.app._inspect()
        self.assertIn("MSH[1]-10", self.app.inspector.get("1.0", "end"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        listener = Listener(Endpoint(port=port))
        listener.start()
        try:
            self.app.vars["port"].set(str(port))
            self.app._send(True)
            deadline = time.monotonic() + 3
            while self.app.active and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertIsNone(self.app.active)
            self.assertEqual(self.app.capture.summary()["success"], 1)
            self.assertEqual(len(self.app.table.get_children()), 1)
            self.app.table.selection_set("1")
            self.app._show_record()
            detail = self.app.details.get("1.0", "end")
            self.assertIn("INBOUND", detail)
            self.assertIn("WIRE HEX", detail)
        finally:
            listener.stop()
            listener.thread.join(3)


if __name__ == "__main__":
    unittest.main()
