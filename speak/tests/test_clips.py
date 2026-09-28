import pytest

from speak_mcp.clips import (
    MAX_CLIP_TEXT,
    MAX_CLIPS,
    MAX_NOTE,
    MAX_TEXT,
    Clip,
    chunk_text,
    label_error,
    text_error,
)


def test_constants():
    assert MAX_TEXT == 12000
    assert MAX_CLIP_TEXT == 3000
    assert MAX_CLIPS == 20
    assert MAX_NOTE == 3000


def test_clip_defaults_caption_to_none():
    clip = Clip(text="olá")
    assert clip.text == "olá" and clip.caption is None


def test_clip_accepts_caption():
    clip = Clip(text="olá", caption="oi")
    assert clip.caption == "oi"


# -- text_error --------------------------------------------------------


def test_text_error_accepts_within_range():
    assert text_error("olá mundo", 12000) is None


def test_text_error_rejects_empty():
    assert text_error("", 12000) is not None


def test_text_error_rejects_too_long():
    assert text_error("x" * 3001, 3000) is not None


def test_text_error_accepts_max_len_exactly():
    assert text_error("x" * 3000, 3000) is None


def test_text_error_rejects_whitespace_only():
    """Regression M4: text that is nothing but whitespace (spaces, tabs,
    newlines) passes the length check but has nothing for Kokoro to speak,
    so it must be its own validation failure, not a silent no-op clip."""
    assert text_error("   ", 12000) is not None
    assert text_error("\n\n\t", 12000) is not None


def test_text_error_accepts_text_with_surrounding_whitespace():
    assert text_error("  olá mundo  ", 12000) is None


# -- label_error ---------------------------------------------------------


def test_label_error_none_is_allowed():
    assert label_error(None, "title") is None


def test_label_error_accepts_valid_label():
    assert label_error("Edição de hoje", "title") is None


def test_label_error_rejects_empty_string():
    assert label_error("", "title") is not None


def test_label_error_rejects_too_long():
    err = label_error("x" * 301, "title")
    assert err is not None and "title" in err


def test_label_error_respects_custom_max_len():
    assert label_error("x" * 3000, "note", max_len=3000) is None
    assert label_error("x" * 3001, "note", max_len=3000) is not None


@pytest.mark.parametrize("value", ["veja https://evil.example", "acesse www.evil.example",
                                    "ftp://x", "veja HTTPS://X.COM"])
def test_label_error_rejects_links(value):
    err = label_error(value, "caption")
    assert err is not None and "link" in err


def test_label_error_allows_bare_http_word():
    assert label_error("HTTP/3 chega ao Cloudflare", "caption") is None


def test_label_error_caption_rejects_newline():
    err = label_error("Título\nfalso item extra", "caption", allow_newlines=False)
    assert err is not None


def test_label_error_note_allows_newline():
    assert label_error("linha 1\nlinha 2", "note", max_len=3000, allow_newlines=True) is None


def test_label_error_note_still_rejects_other_control_chars():
    err = label_error("Título\x07com controle", "note", max_len=3000, allow_newlines=True)
    assert err is not None


# -- chunk_text ------------------------------------------------------------


def test_chunk_text_short_text_is_one_chunk():
    assert chunk_text("Olá, tudo bem?", max_chars=400) == ["Olá, tudo bem?"]


def test_chunk_text_merges_short_sentences_under_limit():
    text = "Frase um. Frase dois. Frase três."
    chunks = chunk_text(text, max_chars=400)
    assert chunks == ["Frase um. Frase dois. Frase três."]


def test_chunk_text_hard_splits_long_sentence_at_space():
    word = "palavra"
    sentence = " ".join([word] * 100) + "."
    chunks = chunk_text(sentence, max_chars=50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 50
        assert not chunk.startswith(" ") and not chunk.endswith(" ")


def test_chunk_text_never_splits_mid_word_unless_single_word_too_long():
    word = "palavra"
    sentence = " ".join([word] * 20) + "."
    chunks = chunk_text(sentence, max_chars=30)
    for chunk in chunks:
        for token in chunk.rstrip(".").split(" "):
            assert token == "" or token == word or token == "palavra."


def test_chunk_text_hard_splits_single_overlong_word_mid_word():
    long_word = "a" * 900
    chunks = chunk_text(long_word, max_chars=400)
    assert chunks == ["a" * 400, "a" * 400, "a" * 100]
    for chunk in chunks:
        assert len(chunk) <= 400


def test_chunk_text_splits_on_blank_line_paragraphs():
    text = "Primeiro parágrafo com uma frase.\n\nSegundo parágrafo, outra frase."
    chunks = chunk_text(text, max_chars=400)
    assert chunks == ["Primeiro parágrafo com uma frase.", "Segundo parágrafo, outra frase."]


def test_chunk_text_handles_pt_br_ellipsis():
    text = "Isso é incrível… Você não vai acreditar."
    chunks = chunk_text(text, max_chars=400)
    assert chunks == ["Isso é incrível… Você não vai acreditar."]


def test_chunk_text_ellipsis_forces_split_when_over_limit():
    text = "Primeira parte da frase que é razoavelmente longa…\nSegunda parte também longa o bastante."
    chunks = chunk_text(text, max_chars=55)
    assert len(chunks) == 2
    assert chunks[0].endswith("…")
    for chunk in chunks:
        assert len(chunk) <= 55


def test_chunk_text_drops_empty_chunks():
    text = "Frase um.\n\n\n\nFrase dois."
    chunks = chunk_text(text, max_chars=400)
    assert "" not in chunks
    assert all(chunk.strip() == chunk for chunk in chunks)


def test_chunk_text_preserves_content_modulo_whitespace():
    text = "Frase um. Frase dois é bem mais longa para forçar quebra de linha em algum ponto do texto."
    chunks = chunk_text(text, max_chars=40)
    joined = " ".join(chunks)
    original_words = text.split()
    joined_words = joined.split()
    assert joined_words == original_words


def test_chunk_text_empty_input_returns_no_chunks():
    assert chunk_text("", max_chars=400) == []
    assert chunk_text("   \n\n  ", max_chars=400) == []
