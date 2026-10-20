# C2PA trust lists

GLEAPP records a Content Credentials signature as **Valid, signer on a trust list** (stored
as `trusted`) only when the signer's certificate chains to an anchor on a list it holds.
C2PA 2.2 §14.4.1 and §14.4.2 say a validator's anchors include the C2PA Trust List (for
claim signers) and the C2PA TSA Trust List (for time-stamping authorities). These are the
copies GLEAPP carries:

| file | what it anchors | SHA-256 | certificates | last changed in |
| --- | --- | --- | ---: | --- |
| `C2PA-TRUST-LIST.pem` | claim signers | `75cacc98b79ecac33713c7ecfb58d4a0ef383f3c1f886e7409f9e37e8664aea5` | 30 | `70ec46e16962b81e70795fcb50a05e0128329a20` (2026-08-13) |
| `C2PA-TSA-TRUST-LIST.pem` | time-stamping authorities | `c688d3555f4a2f1f8d663472bbd37888ff234abdd234c25934c0f9292e4eb5c9` | 22 | `4cb1f317aa4c7ce9cca557dc0cecee18bd784bb7` (2026-08-13) |

Both are published by the Coalition for Content Provenance and Authenticity (C2PA) in
[c2pa-org/conformance-public](https://github.com/c2pa-org/conformance-public) and were
copied unmodified from its `trust-list/` folder at commit
`43a0a6f09091a062083a4ba33e3acc19dd721282`, fetched on 2026-10-06. That repository is
licensed under the Creative Commons Attribution 4.0 International licence (CC BY 4.0),
whose text is reproduced in `LICENSE-c2pa-trust-lists`. The `Subject` line above each
certificate is the publisher's own annotation.

`trust_lists.json` records the same facts for the code, and
`tests/test_provenance_trust.py` pins the hashes and the counts, so a refreshed list
arrives as a reviewed change. To refresh: copy both files from a newer commit, then
update this table, `trust_lists.json` and the test, and say in the pull request what
changed.

## What an examiner can change

The same sections say a validator should let the user add anchors of their own. So the
examiner's anchors are **added** to these lists, never put in their place, and the only
thing that can stand in for a copy here is a newer copy of the same C2PA list. Both are
given for a run of `gleapp provenance` on the command line; the gallery reads with these
lists alone. Every record lists the anchors it was read with, by label and SHA-256 and never
by the file's path on the examiner's machine, and names the one a listed signer
chained to (`trusted_by`), which the reader reports as the list's `trust_uri`.

The TSA list vouches for time-stamping authorities, not for signers. c2pa-rs 0.91.0
still reports Trusted for a signer that chains only to a TSA anchor; GLEAPP records that
as Valid, signer not on a trust list (`valid_untrusted`), in `_state` of
`gleapp/provenance.py`.
