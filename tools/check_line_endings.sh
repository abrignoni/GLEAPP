#!/usr/bin/env bash
# Fail if any tracked text file is stored with CRLF or mixed endings.
#
# .gitattributes normalizes text to LF on commit for any client that honors it, which
# is all of them. This is the backstop for what slips past that: a text file caught by
# a -text rule, or a client with attributes switched off. git's own classification,
# git ls-files --eol, is the authority, so a byte grep never has to re-derive which
# files are text and which are binary.
set -u
bad=$(git ls-files --eol | grep -E '^i/(crlf|mixed)' || true)
if [ -n "$bad" ]; then
  echo "These tracked files are not LF in the repository:"
  echo "$bad"
  echo "Fix: git add --renormalize <file>   (.gitattributes does the rest), then commit."
  exit 1
fi
echo "all $(git ls-files --eol | grep -c '^i/lf') tracked text files are LF"
