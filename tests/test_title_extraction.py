from graph_loader import _extract_title


def extract_title(content):
    """Exercise graph_loader.py's title extraction logic."""
    return _extract_title(content)


def test_double_quoted():
    assert extract_title('title: "Hello World"') == 'Hello World'


def test_single_quoted():
    assert extract_title("title: 'Hello World'") == 'Hello World'


def test_unquoted():
    assert extract_title('title: Hello World') == 'Hello World'


def test_chinese_title():
    assert extract_title('title: "权力悖论"') == '权力悖论'


def test_empty_title():
    assert extract_title('title: ""') == ''
