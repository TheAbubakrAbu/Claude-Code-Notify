"""Repository hygiene, checked on every push.

Nothing in the repository may hold an em dash (the project's writing rule), a path into one
person's home folder (/Users/<name>, which belongs in ~ or $HOME), or a 40-character code
signing identity.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "build", ".build", "node_modules"}

# Built from pieces so this file does not trip its own check.
EM_DASH = re.compile(chr(0x2014) + "|&" + "mdash;|&#" + "8212;|&#x" + "2014;", re.IGNORECASE)
HOME_PATH = re.compile("/" + "Users/[A-Za-z0-9._-]")
SIGNING_ID = re.compile(r"\b[0-9A-F]{40}\b")


def text_files():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as fh:
                data = fh.read()
            if b"\0" in data:
                continue  # binary
            try:
                yield os.path.relpath(path, ROOT), data.decode("utf-8")
            except UnicodeDecodeError:
                continue


class Hygiene(unittest.TestCase):
    def check(self, pattern, what):
        hits = []
        for rel, text in text_files():
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append("%s:%d: %s" % (rel, number, line.strip()[:120]))
        self.assertEqual(hits, [], "%s found:\n%s" % (what, "\n".join(hits)))

    def test_no_em_dashes(self):
        self.check(EM_DASH, "em dash")

    def test_no_home_folder_paths(self):
        self.check(HOME_PATH, "a path into a home folder (use ~ or $HOME)")

    def test_no_signing_identities(self):
        self.check(SIGNING_ID, "a code signing identity")


if __name__ == "__main__":
    unittest.main()
