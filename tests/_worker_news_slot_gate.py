"""Subprocess worker for the order-sensitive NewsSlotGateTest."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    import tests.test_news_slot_production as N
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(N.NewsSlotGateTest)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())