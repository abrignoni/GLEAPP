# Vendored wheels

These exist so `pip install -r requirements.txt` never needs a C compiler on
Windows x64. `requirements.txt` selects one of them by Python version through
environment markers and falls through to the PyPI sdist everywhere else, which
is why Linux, Windows on ARM, and Python 3.14 on macOS still compile.

| file | origin | sha256 |
|---|---|---|
| `pyliblzfse-0.4.1-cp310-cp310-win_amd64.whl` | PyPI release file | `de002191a8e9335b6e6b469f30dd4e1d7a400047274c23694b695c2f1f5d8d38` |
| `pyliblzfse-0.4.1-cp311-cp311-win_amd64.whl` | PyPI release file | `2b49aa4ea96c3feb1324c066c7ed637739a0a2c2f300ef6f9bfafb16af9d5cb2` |
| `pyliblzfse-0.4.1-cp312-cp312-win_amd64.whl` | PyPI release file | `01a5971e95cddac54c2a1e5783625a510d77a319ab537902ad4aeaade4bbe2ee` |
| `pyliblzfse-0.4.1-cp313-cp313-win_amd64.whl` | PyPI release file | `e971ba2720b7a143f82bb47cd19f7efa5ccab84d9b770cef1acbcc4676380223` |
| `pyliblzfse-0.4.1-cp314-cp314-win_amd64.whl` | iLEAPP whl_files/ (setuptools 83.0.0; PyPI has no cp314) | `cc105996b5ab4440e37afc2f08fb97a574ab5c576ccb84599d6270b790ddd05c` |

The four cp310 to cp313 files are PyPI's own release artifacts for pyliblzfse
0.4.1 and verify byte for byte against the digests PyPI publishes. PyPI has no
cp314 wheel at all; that one was built for iLEAPP and is copied from its
`whl_files/` directory unchanged.

To re-verify, sha256 each file and compare with this table, and for the first
four with https://pypi.org/project/pyliblzfse/0.4.1/#files.
