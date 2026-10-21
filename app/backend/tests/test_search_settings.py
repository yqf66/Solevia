from planner_api.config import Settings


def test_search_provider_settings_support_local_open_websearch() -> None:
    settings = Settings(
        _env_file=None,
        web_search_provider="open_websearch",
        web_search_base_url="http://127.0.0.1:3211",
    )

    assert settings.web_search_provider == "open_websearch"
    assert settings.web_search_base_url == "http://127.0.0.1:3211"


def test_search_provider_defaults_to_disabled() -> None:
    settings = Settings(_env_file=None)

    assert settings.web_search_provider == "disabled"
    assert settings.web_research_enabled is False
    assert settings.research_max_subqueries == 12
    assert settings.research_max_queries_per_round == 3
    assert settings.research_max_rounds == 5
    assert settings.research_max_queries == 12
    assert settings.web_search_max_results == 15
    assert settings.research_results_per_query == 12
    assert settings.research_max_read_pages == 8
    assert settings.research_max_page_chars == 8000
    assert settings.research_max_total_page_chars == 30000
