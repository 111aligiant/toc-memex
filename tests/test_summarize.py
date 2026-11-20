from app.ingest.summarize import extractive_summary, extract_keywords


def test_extractive_summary_cuts_at_sentence():
    text = "第一句。" * 20
    summary = extractive_summary(text, 30)
    assert summary.endswith("。")
    assert len(summary) <= 30


def test_extractive_summary_returns_short_text_unchanged():
    assert extractive_summary("短文本。", 300) == "短文本。"


def test_extract_keywords_returns_terms():
    keywords = extract_keywords("温度试验包括高温试验和低温试验。", 5)
    assert keywords
    assert "试验" in keywords or "温度" in keywords