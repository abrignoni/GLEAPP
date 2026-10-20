"""Content Credentials signed in a test, with a chain made in the test.

``make_chain`` makes a throwaway ES256 root and a signer valid for a day either side,
the signer carrying only the C2PA claim-signing EKU (c2pa-kp-claimSigning,
1.3.6.1.4.1.62558.2.1, C2PA 2.2 §14.4.1); ``sign`` signs bytes with c2pa-python's
Builder. Both need the optional extra: import this module after
``pytest.importorskip("c2pa")``. Nothing made here is on a trust list GLEAPP carries.
"""

import datetime
import io

# pylint: disable=import-error
import c2pa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from c2pafake import DST  # noqa: F401  (re-exported for the signed tests)
C2PA_CLAIM_SIGNING = ObjectIdentifier("1.3.6.1.4.1.62558.2.1")
# What the Builder checks its own signature with. Its default policy leaves the C2PA
# EKU out, the same gap provenance.reader_settings closes for reading.
_SIGN_SETTINGS = {"verify": {"remote_manifest_fetch": False, "ocsp_fetch": False},
                  "trust": {"trust_config": "1.3.6.1.4.1.62558.2.1\n"}}


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic Test Org")])


def make_chain():
    """(signer, CA certificate PEM)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    day = datetime.timedelta(days=1)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("Synthetic Root"))
          .issuer_name(_name("Synthetic Root")).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - day).not_valid_after(now + day)
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True,
                                       False, False), True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(_name("Synthetic Signer"))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(2).not_valid_before(now - day).not_valid_after(now + day)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False,
                                         False, False), True)
            .add_extension(x509.ExtendedKeyUsage([C2PA_CLAIM_SIGNING]), False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), False)
            .sign(ca_key, hashes.SHA256()))

    def pem(cert):
        return cert.public_bytes(serialization.Encoding.PEM)

    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    signer = c2pa.Signer.from_info(c2pa.C2paSignerInfo(b"es256", pem(leaf) + pem(ca),
                                                       private, None))
    return signer, pem(ca)


def created(dst_term: str) -> list:
    """The actions of a file made from nothing, declaring ``dst_term``."""
    return [{"action": "c2pa.created", "digitalSourceType": DST + dst_term}]


def sign(chain, src: bytes, mime: str, actions: list, *, ingredient: bytes | None = None,
         generator: str = "Synthetic Generator", remote: str | None = None) -> bytes:
    """``src`` signed. With ``remote`` the store is left out of the file and only its
    address, ``remote``, is written in the file's XMP."""
    signer, _ca = chain
    manifest = {"claim_generator_info": [{"name": generator, "version": "1.0"}],
                "title": "synthetic", "assertions": [
                    {"label": "c2pa.actions", "data": {"actions": actions}}]}
    builder = c2pa.Builder(manifest, context=c2pa.Context(
        settings=c2pa.Settings.from_dict(_SIGN_SETTINGS)))
    if remote is not None:
        builder.set_no_embed()
        builder.set_remote_url(remote)
    if ingredient is not None:
        builder.add_ingredient({"title": "ingredient.jpg", "relationship": "parentOf",
                                "label": "ingredient-1"}, "image/jpeg", io.BytesIO(ingredient))
    out = io.BytesIO()
    builder.sign(signer, mime, io.BytesIO(src), out)
    return out.getvalue()
