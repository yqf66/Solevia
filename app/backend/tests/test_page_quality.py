from planner_api.research.page_quality import assess_page_quality
from planner_api.research.schemas import PageDocument


def _document(content: str, **kwargs) -> PageDocument:
    final_url = kwargs.pop("finalUrl", "https://example.com/article")
    return PageDocument(
        url="https://example.com/article",
        finalUrl=final_url,
        title=kwargs.pop("title", "Article"),
        content=content,
        contentType=kwargs.pop("content_type", "text/html"),
        readabilityApplied=kwargs.pop("readability", True),
        **kwargs,
    )


def test_normal_article_is_usable() -> None:
    assessment = assess_page_quality(_document("正文内容 " * 100))

    assert assessment.usable is True
    assert assessment.score >= 90
    assert "LOW_TEXT" not in assessment.flags


def test_login_and_challenge_pages_are_rejected() -> None:
    login = assess_page_quality(_document("请先登录后继续访问", title="登录"))
    challenge = assess_page_quality(
        _document("Verify you are human. CAPTCHA required.", title="Security check")
    )

    assert login.usable is False
    assert "AUTH_REQUIRED" in login.flags
    assert challenge.usable is False
    assert "BOT_CHALLENGE" in challenge.flags


def test_community_app_wall_and_script_shell_are_rejected() -> None:
    app_wall = assess_page_quality(
        _document("打开APP，登录查看更多完整内容", title="社区笔记", readability=False)
    )
    script_shell = assess_page_quality(
        _document("请开启JavaScript后继续访问", title="社区文章", readability=False)
    )

    assert app_wall.usable is False
    assert "APP_WALL" in app_wall.flags
    assert script_shell.usable is False
    assert "SCRIPT_SHELL" in script_shell.flags


def test_login_redirect_with_enough_text_is_not_mistaken_for_an_article() -> None:
    login_page = assess_page_quality(
        _document(
            "登录后推荐更懂你的笔记 手机号登录 获取验证码 " + "用户协议 隐私政策 " * 30,
            finalUrl="https://www.xiaohongshu.com/login?redirectPath=%2Fexplore%2Fnote",
            title="小红书 - 你的生活兴趣社区",
            readability=False,
        )
    )
    zhihu_block = assess_page_quality(
        _document(
            '{"error":{"message":"您当前请求存在异常，暂时限制本次访问"}}',
            finalUrl="https://zhuanlan.zhihu.com/p/1",
            readability=False,
        )
    )

    assert login_page.usable is False
    assert "AUTH_REQUIRED" in login_page.flags
    assert zhihu_block.usable is False
    assert "BOT_CHALLENGE" in zhihu_block.flags


def test_navigation_only_shell_is_rejected_but_long_article_is_not() -> None:
    shell = assess_page_quality(
        _document(
            "首页 登录 注册 下载客户端 打开APP 关注 消息 搜索",
            readability=False,
        )
    )
    article = assess_page_quality(
        _document(
            "文章讨论社区客户端的登录、注册和搜索功能。" + "正文分析 " * 300,
            readability=False,
        )
    )

    assert shell.usable is False
    assert "NAVIGATION_SHELL" in shell.flags
    assert article.usable is True
    assert "NAVIGATION_SHELL" not in article.flags


def test_error_and_binary_pages_are_rejected() -> None:
    error = assess_page_quality(_document("404 page not found", title="404 Not Found"))
    binary = assess_page_quality(
        _document("raw bytes", content_type="image/png")
    )

    assert error.usable is False
    assert "ERROR_PAGE" in error.flags
    assert binary.usable is False
    assert "UNSUPPORTED_CONTENT_TYPE" in binary.flags


def test_normal_articles_that_mention_error_terms_are_not_rejected() -> None:
    error_guide = assess_page_quality(
        _document(
            "This guide explains how to handle HTTP 404 responses and recover safely. "
            "details " * 100,
            title="404 handling guide",
        )
    )
    login_reference = assess_page_quality(
        _document(
            "The login endpoint returns a token for authenticated API clients. "
            "details " * 100,
            title="Login API reference",
        )
    )

    assert error_guide.usable is True
    assert "ERROR_PAGE" not in error_guide.flags
    assert login_reference.usable is True
    assert "AUTH_REQUIRED" not in login_reference.flags
