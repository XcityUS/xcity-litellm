"""
Ark's signed video download URLs can carry non-ASCII characters. Handing one
straight to httpx raises `UnicodeEncodeError: 'ascii' codec can't encode
characters ...`, which surfaced in production as an opaque 500 on
GET /v1/videos/{id}/content — the only way to fetch the bytes, since the
proxy's OpenAI-shaped VideoObject response drops `output_url`.
"""

from litellm.llms.byteplus.videos.transformation import BytePlusVideoConfig

encode = BytePlusVideoConfig._encode_download_url


def test_non_ascii_path_is_percent_encoded():
    url = "https://ark-content.example.com/videos/红灯笼-clip.mp4?sig=abc123"
    encoded = encode(url)
    assert encoded.isascii()
    assert "%E7%BA%A2" in encoded  # 红
    assert encoded.startswith("https://ark-content.example.com/videos/")
    assert encoded.endswith("?sig=abc123")


def test_non_ascii_query_is_percent_encoded():
    url = "https://ark.example.com/v/clip.mp4?name=灯笼&sig=xyz"
    encoded = encode(url)
    assert encoded.isascii()
    assert "sig=xyz" in encoded


def test_plain_ascii_url_is_unchanged():
    url = "https://ark.example.com/v/clip.mp4?x-tos-credential=AK%2F20260726&sig=a+b"
    assert encode(url) == url


def test_encoding_is_idempotent():
    url = "https://ark.example.com/v/红.mp4?sig=abc"
    once = encode(url)
    assert encode(once) == once


def test_signed_url_reserved_characters_survive():
    # Ark signatures rely on these staying literal.
    url = (
        "https://ark.example.com/v/clip.mp4"
        "?X-Tos-Algorithm=TOS4-HMAC-SHA256&X-Tos-Credential=AK/20260726/ap/tos"
        "&X-Tos-Signature=deadbeef&X-Tos-SignedHeaders=host"
    )
    assert encode(url) == url
