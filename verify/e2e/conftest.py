import os

import pytest
from playwright.sync_api import sync_playwright

WEB_BASE = os.environ.get("WEB_BASE", "http://localhost:8080")
API_BASE = os.environ.get("API_BASE", "http://localhost:8000")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    # 每个用例独立浏览器上下文：localStorage 互不污染
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()
