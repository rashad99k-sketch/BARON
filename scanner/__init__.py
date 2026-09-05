"""Scanner package.

Heavy exchange modules are imported explicitly by runtime code. Keeping the
package initializer lightweight lets pure universe/classification tests run
without requiring exchange SDKs.
"""
