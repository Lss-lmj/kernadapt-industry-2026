#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import signal
import socket
import subprocess
import tempfile
import threading
import unittest


CLIENT = pathlib.Path("/tmp/flowevent_v2-static")


class FlowEventV2Test(unittest.TestCase):
    def test_reports_one_command_and_preserves_returncode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="flowevent-v2-") as directory:
            socket_path = pathlib.Path(directory) / "flowd.sock"
            received: list[str] = []
            ready = threading.Event()

            def server() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(socket_path))
                    listener.listen(1)
                    ready.set()
                    connection, _ = listener.accept()
                    with connection:
                        stream = connection.makefile("r", encoding="utf-8")
                        for _ in range(3):
                            received.append(stream.readline().rstrip("\n"))
                            connection.sendall(b"OK\n")

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(2))
            result = subprocess.run(
                [
                    str(CLIENT),
                    "--socket",
                    str(socket_path),
                    "--session-id",
                    "11",
                    "--tool-id",
                    "22",
                    "--generation",
                    "33",
                    "--sequence",
                    "41",
                    "--execution-mode",
                    "synchronous",
                    "--stage",
                    "unknown",
                    "--",
                    "sh",
                    "-c",
                    "exit 7",
                ],
                timeout=10,
                check=False,
            )
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result.returncode, 7)
            self.assertEqual(len(received), 3)
            self.assertEqual(received[0].split()[1], "tool-begin")
            self.assertEqual(received[1].split()[1], "tool-update")
            self.assertGreater(int(received[1].split()[9]), 0)
            self.assertEqual(received[2].split()[1], "tool-end")
            self.assertEqual([int(line.split()[6]) for line in received], [41, 42, 43])

    def test_hold_mode_keeps_connection_until_signalled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="flowevent-v2-hold-") as directory:
            socket_path = pathlib.Path(directory) / "flowd.sock"
            ready_path = pathlib.Path(directory) / "ready"
            received: list[str] = []
            ready = threading.Event()

            def server() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(socket_path))
                    listener.listen(1)
                    ready.set()
                    connection, _ = listener.accept()
                    with connection:
                        stream = connection.makefile("r", encoding="utf-8")
                        for _ in range(2):
                            received.append(stream.readline().rstrip("\n"))
                            connection.sendall(b"OK\n")

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(2))
            process = subprocess.Popen(
                [
                    str(CLIENT),
                    "--socket",
                    str(socket_path),
                    "--session-id",
                    "11",
                    "--tool-id",
                    "23",
                    "--generation",
                    "34",
                    "--sequence",
                    "51",
                    "--hold",
                    "--ready-file",
                    str(ready_path),
                ]
            )
            for _ in range(100):
                if ready_path.exists():
                    break
                threading.Event().wait(0.01)
            self.assertTrue(ready_path.exists())
            process.send_signal(signal.SIGUSR1)
            self.assertEqual(process.wait(timeout=2), 0)
            thread.join(timeout=2)
            self.assertEqual([line.split()[1] for line in received], ["tool-begin", "tool-end"])
            self.assertEqual([int(line.split()[6]) for line in received], [51, 53])


if __name__ == "__main__":
    unittest.main()
