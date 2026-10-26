from planner_api.materials.chunking import chunk_material


def test_long_unbroken_text_is_split_with_bounded_size_and_stable_offsets() -> None:
    text = "# Overview\n\n" + ("A" * 1500) + "\n\n## Detail\n\n" + ("B" * 1300)

    chunks = chunk_material(text, max_chars=400, overlap_chars=40)

    assert len(chunks) > 5
    assert all(len(chunk.text) <= 400 for chunk in chunks)
    assert all(text[chunk.char_start : chunk.char_end] == chunk.text for chunk in chunks)
    assert chunks[0].section_title == "Overview"
    assert any(chunk.section_title == "Detail" for chunk in chunks)
    assert all(
        current.char_start < previous.char_end
        for previous, current in zip(chunks, chunks[1:], strict=False)
    )


def test_chunker_prefers_sentence_or_paragraph_boundaries() -> None:
    text = ("第一段内容。 " * 30) + "\n\n" + ("第二段内容。 " * 30)

    chunks = chunk_material(text, max_chars=100, overlap_chars=10)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    assert all(text[chunk.char_start : chunk.char_end] == chunk.text for chunk in chunks)
