"""Unit tests for ANSICleaner and WeChatSplitter in gateway/terminal_cleaner.py."""
from gateway.terminal_cleaner import ANSICleaner, WeChatSplitter


def test_ansi_cleaner_strip():
    raw = "\x1b[31;1mError:\x1b[0m \x1b[32mSuccess\x1b[0m\nLine 2"
    assert ANSICleaner.strip_ansi(raw) == "Error: Success\nLine 2"

    raw_osc = "\x1b]0;agy-terminal\x07Working on code"
    assert ANSICleaner.strip_ansi(raw_osc) == "Working on code"


def test_ansi_cleaner_filter_spinners():
    raw = "⠋ Thinking...\n⠙ Still thinking...\nFinished"
    cleaned = ANSICleaner.filter_spinners(raw)
    assert "Thinking..." in cleaned
    assert "⠋" not in cleaned
    assert "Finished" in cleaned


def test_ansi_cleaner_resolve_carriage_returns():
    raw = "Progress 10%\rProgress 50%\rProgress 100%"
    assert ANSICleaner.resolve_carriage_returns(raw) == "Progress 100%"


def test_ansi_cleaner_dedup_consecutive_lines():
    repeated = "Generating...\n\nGenerating...\n\nGenerating...\nDone!"
    assert ANSICleaner.dedup_consecutive_lines(repeated) == "Generating...\n\nDone!"

    code_text = "```python\nprint(1)\nprint(1)\n```"
    assert "print(1)\nprint(1)" in ANSICleaner.dedup_consecutive_lines(code_text)


def test_ansi_cleaner_full_pipeline():
    cleaner = ANSICleaner()
    raw = "\x1b[33m⠋ Loading...\r\x1b[32mReady!\x1b[0m"
    assert cleaner.clean_text(raw) == "Ready!"


def test_wechat_splitter_short_text():
    splitter = WeChatSplitter(max_length=50)
    assert splitter.split("Short message") == ["Short message"]


def test_wechat_splitter_markdown_code_block():
    splitter = WeChatSplitter(max_length=60)
    code = (
        "Here is the code:\n"
        "```python\n"
        "def hello():\n"
        "    print('world')\n"
        "    return True\n"
        "```\n"
        "Done!"
    )
    chunks = splitter.split(code)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 75
    assert "```" in chunks[0]
    assert "def hello" in chunks[0] or "def hello" in chunks[1]
