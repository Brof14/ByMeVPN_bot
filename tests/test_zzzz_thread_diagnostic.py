"""Temporary diagnostic for hung CI Python process after suite prints OK."""
import gc
import sys
import threading
import unittest

class ThreadDiagnostic(unittest.TestCase):
    def test_threads_at_end_of_suite(self):
        import aiosqlite
        conn = [x for x in gc.get_objects() if isinstance(x, aiosqlite.Connection)]
        lines = [f"[CI-THREADS] {t.name} alive={t.is_alive()} daemon={t.daemon}" for t in threading.enumerate()]
        lines += [f"[CI-DB] id={id(x)} alive={x.is_alive()} repr={repr(x)}" for x in conn]
        sys.stderr.write("\n"+"\n".join(lines)+"\n")
        self.assertTrue(True)
