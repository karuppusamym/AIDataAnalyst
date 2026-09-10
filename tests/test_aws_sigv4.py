"""Known-answer tests for `aida.aws_sigv4`.

A signer with no known-answer test is a signer nobody can trust: it either
works against the service or it does not, and when it does not there is no
way to tell a wrong canonical request from a wrong scope from a wrong key
derivation. So this file does not check that the signer is self-consistent
-- it checks it against constants AWS published, which were produced by
AWS's own implementation and which this code cannot influence.

Four vectors from the "Examples of the complete version 4 signing process
(Python)" / "Authenticating Requests: Using the Authorization Header"
worked examples in the Amazon S3 API reference, all against the same
fixture (`examplebucket`, `20130524T000000Z`, `us-east-1`, the published
`AKIAIOSFODNN7EXAMPLE` credential pair):

* **GET Object** with a `Range` header -- the basic path, and the one whose
  intermediate values (canonical request, string to sign) AWS also
  publishes, so this vector pins all three stages, not just the answer.
* **PUT Object** with a body, a `Date` header and `x-amz-storage-class` --
  pins the non-empty payload hash, and the path escaping of `test$file.text`
  (`$` must become `%24`, exactly once).
* **GET Bucket Lifecycle** -- pins a valueless query subresource
  (`?lifecycle`), which must still be canonicalized as `lifecycle=`. The S3
  Object Lock calls this signer actually issues (`?legal-hold`,
  `?object-lock`) are that same shape.
* **GET Bucket (List Objects)** with `max-keys` and `prefix` -- pins
  multi-parameter query sorting.

If a change to the signer breaks any stage, one of these fails with a
concrete diff rather than a 403 from a service.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aida import aws_sigv4

# The credential pair AWS publishes with its own worked examples. It has
# never been valid against any account; it exists to make signatures
# reproducible.
VECTOR_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
VECTOR_CREDENTIAL_MATERIAL = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
VECTOR_MOMENT = datetime(2013, 5, 24, 0, 0, 0, tzinfo=UTC)
VECTOR_HOST = "examplebucket.s3.amazonaws.com"
VECTOR_REGION = "us-east-1"

# An STS session token is not a credential this project holds; this is a
# placeholder used only to assert the header is signed when one is present.
PLACEHOLDER_STS_SESSION_VALUE = "placeholder-session-value"


def _authorization(**kwargs: object) -> str:
    signed = aws_sigv4.signed_headers_for(
        host=VECTOR_HOST,
        moment=VECTOR_MOMENT,
        region=VECTOR_REGION,
        access_key=VECTOR_ACCESS_KEY,
        secret_key=VECTOR_CREDENTIAL_MATERIAL,
        **kwargs,  # type: ignore[arg-type]
    )
    return signed["Authorization"]


def _signature(authorization: str) -> str:
    return authorization.rpartition("Signature=")[2]


# --- the vectors -----------------------------------------------------------


def test_get_object_matches_the_published_signature() -> None:
    """AWS's GET Object example, signature and both intermediate stages."""
    authorization = _authorization(
        method="GET",
        path="/test.txt",
        headers={"range": "bytes=0-9"},
        payload=b"",
    )
    assert _signature(authorization) == (
        "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in authorization
    assert (
        f"Credential={VECTOR_ACCESS_KEY}/20130524/us-east-1/s3/aws4_request" in authorization
    )


def test_get_object_canonical_request_matches_the_published_text() -> None:
    """The canonical request is pinned byte for byte, not just its signature.

    A wrong canonical request that happens to produce the right signature is
    impossible, but a *right* signature reached through a canonical request
    this test cannot see would leave the failure mode -- a header signed but
    not listed, say -- undiagnosable. AWS publishes this text; pin it.
    """
    request, signed_headers = aws_sigv4.canonical_request(
        method="GET",
        path="/test.txt",
        query=None,
        headers={
            "host": VECTOR_HOST,
            "range": "bytes=0-9",
            "x-amz-content-sha256": aws_sigv4.EMPTY_PAYLOAD_SHA256,
            "x-amz-date": "20130524T000000Z",
        },
        payload_hash=aws_sigv4.EMPTY_PAYLOAD_SHA256,
    )
    assert request == (
        "GET\n"
        "/test.txt\n"
        "\n"
        "host:examplebucket.s3.amazonaws.com\n"
        "range:bytes=0-9\n"
        f"x-amz-content-sha256:{aws_sigv4.EMPTY_PAYLOAD_SHA256}\n"
        "x-amz-date:20130524T000000Z\n"
        "\n"
        "host;range;x-amz-content-sha256;x-amz-date\n"
        f"{aws_sigv4.EMPTY_PAYLOAD_SHA256}"
    )
    assert signed_headers == "host;range;x-amz-content-sha256;x-amz-date"

    # AWS publishes the hash of that canonical request as the last line of
    # the string to sign, which is what makes this an independent check of
    # the canonicalization rather than a restatement of it.
    assert aws_sigv4.string_to_sign(
        request=request, moment=VECTOR_MOMENT, region=VECTOR_REGION, service="s3"
    ) == (
        "AWS4-HMAC-SHA256\n"
        "20130524T000000Z\n"
        "20130524/us-east-1/s3/aws4_request\n"
        "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
    )


def test_put_object_with_a_body_matches_the_published_signature() -> None:
    """Pins the non-empty payload hash and single-pass path escaping."""
    authorization = _authorization(
        method="PUT",
        path="/test$file.text",
        headers={
            "date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        payload=b"Welcome to Amazon S3.",
    )
    assert _signature(authorization) == (
        "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )
    assert aws_sigv4.canonical_uri("/test$file.text") == "/test%24file.text"


def test_valueless_query_subresource_matches_the_published_signature() -> None:
    """`?lifecycle` -- the shape every Object Lock subresource call uses."""
    authorization = _authorization(method="GET", path="/", query={"lifecycle": ""}, payload=b"")
    assert _signature(authorization) == (
        "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"
    )
    assert aws_sigv4.canonical_query_string({"lifecycle": ""}) == "lifecycle="


def test_multi_parameter_query_matches_the_published_signature() -> None:
    """Two parameters, and they must be sorted by name however they arrive."""
    authorization = _authorization(
        method="GET", path="/", query={"prefix": "J", "max-keys": "2"}, payload=b""
    )
    assert _signature(authorization) == (
        "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )
    assert aws_sigv4.canonical_query_string({"prefix": "J", "max-keys": "2"}) == (
        "max-keys=2&prefix=J"
    )


# --- properties the vectors alone do not pin -------------------------------


def test_signed_header_list_and_header_block_cannot_disagree() -> None:
    """Every header handed in is signed, and every signed header is listed.

    The most common SigV4 defect is a header that reaches the wire inside
    the signature but outside `SignedHeaders` (or the reverse); the service
    answers 403 with no hint which. Both halves come from one sorted
    sequence here, and this asserts that.
    """
    headers = {"X-Amz-Meta-Zed": "1", "content-type": "application/json", "Host": "example.com"}
    block, signed = aws_sigv4.canonical_headers(headers)
    assert signed == "content-type;host;x-amz-meta-zed"
    assert block == "content-type:application/json\nhost:example.com\nx-amz-meta-zed:1\n"
    assert [line.split(":", 1)[0] for line in block.splitlines()] == signed.split(";")


def test_every_supplied_header_appears_in_the_signed_list() -> None:
    """`signed_headers_for` may add headers, but never drop one it was given."""
    signed = aws_sigv4.signed_headers_for(
        method="PUT",
        host="example.com",
        path="/bucket/key",
        headers={
            "content-md5": "1B2M2Y8AsgTpgAmY7PhCfg==",
            "x-amz-object-lock-mode": "COMPLIANCE",
            "x-amz-object-lock-retain-until-date": "2030-01-01T00:00:00Z",
        },
        payload=b"body",
        moment=VECTOR_MOMENT,
        region=VECTOR_REGION,
        access_key=VECTOR_ACCESS_KEY,
        secret_key=VECTOR_CREDENTIAL_MATERIAL,
    )
    listed = signed["Authorization"].partition("SignedHeaders=")[2].partition(",")[0].split(";")
    for name in (
        "content-md5",
        "host",
        "x-amz-content-sha256",
        "x-amz-date",
        "x-amz-object-lock-mode",
        "x-amz-object-lock-retain-until-date",
    ):
        assert name in listed


def test_session_token_is_signed_only_when_present() -> None:
    without = aws_sigv4.signed_headers_for(
        method="GET",
        host="example.com",
        path="/",
        moment=VECTOR_MOMENT,
        region=VECTOR_REGION,
        access_key=VECTOR_ACCESS_KEY,
        secret_key=VECTOR_CREDENTIAL_MATERIAL,
    )
    assert "x-amz-security-token" not in without

    with_token = aws_sigv4.signed_headers_for(
        method="GET",
        host="example.com",
        path="/",
        moment=VECTOR_MOMENT,
        region=VECTOR_REGION,
        access_key=VECTOR_ACCESS_KEY,
        secret_key=VECTOR_CREDENTIAL_MATERIAL,
        session_token=PLACEHOLDER_STS_SESSION_VALUE,
    )
    assert with_token["x-amz-security-token"] == PLACEHOLDER_STS_SESSION_VALUE
    assert "x-amz-security-token" in with_token["Authorization"]


def test_the_secret_never_appears_in_the_authorization_header() -> None:
    """The header carries the access key id, the scope and a signature -- no more."""
    authorization = _authorization(method="GET", path="/test.txt", payload=b"")
    assert VECTOR_CREDENTIAL_MATERIAL not in authorization
    assert VECTOR_ACCESS_KEY in authorization


def test_signature_is_scoped_to_date_region_and_service() -> None:
    """Changing any scope component changes the signature.

    Not a published vector -- a property. A signer that ignored the region
    would still pass every us-east-1 vector above and then fail against any
    other region with a 403 nobody could localize.
    """
    base = _authorization(method="GET", path="/test.txt", payload=b"")
    other_region = aws_sigv4.signed_headers_for(
        method="GET",
        host=VECTOR_HOST,
        path="/test.txt",
        moment=VECTOR_MOMENT,
        region="eu-west-1",
        access_key=VECTOR_ACCESS_KEY,
        secret_key=VECTOR_CREDENTIAL_MATERIAL,
    )["Authorization"]
    other_day = aws_sigv4.signed_headers_for(
        method="GET",
        host=VECTOR_HOST,
        path="/test.txt",
        moment=datetime(2013, 5, 25, 0, 0, 0, tzinfo=UTC),
        region=VECTOR_REGION,
        access_key=VECTOR_ACCESS_KEY,
        secret_key=VECTOR_CREDENTIAL_MATERIAL,
    )["Authorization"]
    assert _signature(base) != _signature(other_region)
    assert _signature(base) != _signature(other_day)
    assert "eu-west-1" in other_region
    assert "20130525/" in other_day


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", "/"),
        ("/plain/key.json", "/plain/key.json"),
        ("/space here", "/space%20here"),
        ("/unreserved-_.~", "/unreserved-_.~"),
        ("/percent%20already", "/percent%2520already"),
    ],
)
def test_path_encoding_is_single_pass_and_preserves_separators(path: str, expected: str) -> None:
    """S3 encodes a path exactly once -- an already-encoded `%` is escaped again."""
    assert aws_sigv4.canonical_uri(path) == expected
