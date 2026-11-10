"""Browser regressions for error handling and a project switch during an invitation."""

import argparse
import re
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--browser-executable")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.browser_executable)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base, wait_until="networkidle")
        page.get_by_role("button", name="创建账号", exact=True).click()
        page.get_by_label("用户名或邮箱").fill(f"ui-regression-{uuid4().hex[:8]}")
        page.get_by_label("密码", exact=True).fill("isolated-browser-regression-password")
        page.get_by_role("button", name="注册并登录", exact=True).click()
        expect(page.get_by_role("heading", name="开始一个研究项目。")).to_be_visible()
        projects = []
        for name in ("回归项目 A", "回归项目 B"):
            page.get_by_label("新建项目", exact=True).fill(name)
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/projects") and response.request.method == "POST"
                )
            ) as response:
                page.get_by_role("button", name="创建项目", exact=True).click()
            assert response.value.status == 201
            projects.append(response.value.json())
            page.wait_for_load_state("networkidle")
        page.get_by_label("切换项目").select_option(projects[0]["id"])
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="成员与邀请", exact=False).click()
        pending = []
        pattern = "**/api/projects/*/invitations"
        page.route(pattern, lambda route: pending.append(route))
        page.get_by_role("button", name="生成一次性邀请").click()
        expect(page.get_by_label("切换项目")).to_be_disabled()
        assert len(pending) == 1
        pending[0].continue_()
        expect(page.get_by_label("邀请链接（单次使用）")).to_have_value(
            re.compile(r".*\?invite=.+")
        )
        expect(page.get_by_label("切换项目")).to_be_enabled()
        page.unroute(pattern)
        page.get_by_role("navigation", name="项目页面").get_by_role(
            "button", name="时间线", exact=True
        ).click()
        expect(page.locator("[data-event-type]")).to_have_count(2)
        event_pattern = "**/api/projects/*/events?*"
        page.route(
            event_pattern,
            lambda route: route.fulfill(status=500, json={"detail": "模拟事件读取失败"}),
        )
        account = context.request.get(base + "/api/auth/me").json()
        page.get_by_label("按操作者查看").select_option(account["id"])
        expect(page.get_by_role("alert")).to_contain_text("模拟事件读取失败")
        expect(page.locator("[data-event-type]")).to_have_count(0)
        page.unroute(event_pattern)
        page.get_by_role("button", name="刷新时间线").click()
        expect(page.locator("[data-event-type]")).to_have_count(2)
        page.route(
            "**/api/auth/logout",
            lambda route: route.fulfill(status=500, json={"detail": "模拟退出失败"}),
        )
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("操作未完成")
        assert context.request.get(base + "/api/auth/me").status == 200
        page.unroute("**/api/auth/logout")
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(page.get_by_role("heading", name="登录工作台")).to_be_visible()
        assert not errors, errors
        browser.close()
    print(
        "Invitation switch guard, filtered timeline failure/recovery, "
        "logout failure/recovery: passed"
    )


if __name__ == "__main__":
    main()
