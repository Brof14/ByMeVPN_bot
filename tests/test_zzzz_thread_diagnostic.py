"""Temporary diagnostic for hung CI Python process after suite prints OK."""
import gc
import os
import sys
import threading
import unittest

class ThreadDiagnostic(unittest.TestCase):
    def test_threads_at_end_of_suite(self):
        import aiosqlite
        conn = [x for x in gc.get_objects() if isinstance(x, aiosqlite.Connection)]
        lines = [f"[CI-THREADS] {t.name} alive={t.is_alive()} daemon={t.daemon}" for t in threading.enumerate()]
        import database
        lines.append(f"[CI-STATE] DB_FILE={database.DB_FILE} pool={id(database._db_pool)}")
        for x in conn:
            lines.append(f"[CI-DB] id={id(x)} alive={x.is_alive()} repr={repr(x)}")
            for ref in gc.get_referrers(x):
                if isinstance(ref, dict):
                    matches = [str(k) for k,v in ref.items() if v is x]
                    if matches:
                        lines.append(f"[CI-REF] id={id(x)} module={ref.get(chr(95)*2 + 'name' + chr(95)*2)} keys={matches[:6]}")
        sys.stderr.write("\n"+"\n".join(lines)+"\n")
        sys.stderr.flush()
        os._exit(1)  # Diagnostic only: publish live threads immediately
