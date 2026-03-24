from app.darknet.adapters import list_darknet_adapters, suggest_adapter_for_detected
from app.darknet.adapters.phpbb_like import PhpbbLikeAdapter
from app.darknet.adapters.xenforo_like import XenForoLikeAdapter


def test_extract_thread_links_same_host_only():
    adapter = XenForoLikeAdapter()
    html = """
    <html><body>
      <a href='/threads/abc.1/'>A</a>
      <a href='https://example.onion/threads/abc.1/'>A2</a>
      <a href='https://evil.onion/threads/zzz.2/'>B</a>
      <a href='/forums/general'>C</a>
    </body></html>
    """
    links = adapter._extract_thread_links(
        html,
        page_url="https://example.onion/forums/general",
        thread_url_contains="/threads/",
        base_host="example.onion",
    )

    assert "https://example.onion/threads/abc.1/" in links
    assert all("evil.onion" not in item for item in links)


def test_parse_posts_from_html_basic():
    adapter = XenForoLikeAdapter()
    html = """
    <html>
      <h1 class='p-title-value'>Thread title</h1>
      <article class='message' id='post-1'>
        <a class='username'>alice</a>
        <time datetime='2026-01-01T10:00:00+00:00'></time>
        <div class='message-body'>Hello world</div>
      </article>
      <article class='message' id='post-2'>
        <a class='username'>bob</a>
        <time datetime='2026-01-01T10:05:00+00:00'></time>
        <div class='message-body'>Second post</div>
      </article>
    </html>
    """

    title, posts, users = adapter._parse_posts_from_html(html)

    assert title == "Thread title"
    assert len(posts) == 2
    assert posts[0].author == "alice"
    assert posts[0].text == "Hello world"
    assert len(users) == 2


def test_phpbb_extract_thread_links_same_host_only():
    adapter = PhpbbLikeAdapter()
    html = """
    <html><body>
      <a href='/viewtopic.php?t=10'>A</a>
      <a href='https://forum.onion/viewtopic.php?f=2&t=11#p11'>B</a>
      <a href='https://evil.onion/viewtopic.php?t=99'>C</a>
    </body></html>
    """
    links = adapter._extract_thread_links(
        html,
        page_url="https://forum.onion/viewforum.php?f=2",
        thread_url_contains="/viewtopic.php",
        base_host="forum.onion",
    )

    assert "https://forum.onion/viewtopic.php?t=10" in links
    assert "https://forum.onion/viewtopic.php?f=2&t=11" in links
    assert all("evil.onion" not in item for item in links)


def test_phpbb_parse_posts_from_html_basic():
    adapter = PhpbbLikeAdapter()
    html = """
    <html>
      <h2 class='topic-title'>Forum thread</h2>
      <div class='post' id='p100'>
        <a class='username'>neo</a>
        <p class='author'>2026-02-01T11:00:00+00:00</p>
        <div class='content'>First post</div>
      </div>
      <div class='post' id='p101'>
        <a class='username-coloured'>trinity</a>
        <p class='author'>2026-02-01T11:05:00+00:00</p>
        <div class='content'>Second post</div>
      </div>
    </html>
    """
    title, posts, users = adapter._parse_posts_from_html(html)

    assert title == "Forum thread"
    assert len(posts) == 2
    assert posts[0].post_id == "p100"
    assert posts[1].author == "trinity"
    assert len(users) == 2


def test_darknet_adapter_registry_and_suggestion():
    adapters = list_darknet_adapters()
    assert "xenforo_like" in adapters
    assert "phpbb_like" in adapters
    assert suggest_adapter_for_detected("vbulletin_like") == "xenforo_like"
