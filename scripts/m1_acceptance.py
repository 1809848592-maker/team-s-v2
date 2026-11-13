"""M1 browser acceptance. Run only against an isolated test database/storage volume."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import secrets
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from zipfile import ZipFile, is_zipfile

from playwright.sync_api import Browser, Locator, Page, expect, sync_playwright
from playwright.sync_api import Error as PlaywrightError


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def submission_snapshot(delivery: dict[str, Any]) -> dict[str, Any]:
    """Keep submission fields while the independently running review advances."""
    return {
        key: value
        for key, value in delivery.items()
        if key not in {"review_status", "review_progress"}
    }


class Acceptance:
    def __init__(self, browser: Browser, base_url: str, output: Path):
        self.browser = browser
        self.base_url = base_url.rstrip("/")
        self.output = output
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        self.password = "M1-" + secrets.token_urlsafe(24)
        self.pages: dict[str, Page] = {}
        self.accounts: dict[str, dict] = {}
        self.console: list[dict] = []
        self.page_errors: list[dict] = []
        self.injecting_logout_failure = False
        self.checks: list[str] = []
        self.project: dict = {}
        self.plan: dict = {}
        self.spec: dict = {}
        self.request: dict = {}
        self.deliveries: list[dict] = []
        self.expected_font = "文稿中文正文使用宋体14磅，英文正文使用 Times New Roman 14磅。"
        self.module_names = ["文献与概念", "数据与分析", "统稿与展示"]
        self.files = output / "files"
        self.files.mkdir()

    def checked(self, message: str) -> None:
        self.checks.append(message)
        print(f"PASS {message}", flush=True)

    def api(self, role: str, path: str) -> Any:
        """Only GET is permitted here; all business writes happen through visible UI."""
        response = self.pages[role].context.request.get(path)
        assert response.ok, f"GET {path}: {response.status} {response.text()}"
        return response.json()

    def ui_post(self, page: Page, path: str, action) -> Any:
        with page.expect_response(
            lambda response: (
                urlsplit(response.url).path == path and response.request.method == "POST"
            )
        ) as pending:
            action()
        response = pending.value
        assert response.ok, f"UI POST {path}: {response.status} {response.text()}"
        result = None if response.status == 204 else response.json()
        page.wait_for_load_state("networkidle")
        return result

    def tab(self, role: str, label: str, *, reload: bool = False) -> Page:
        page = self.pages[role]
        if reload:
            page.reload(wait_until="networkidle")
        nav = page.get_by_role("navigation", name="项目页面", exact=True)
        nav.get_by_role("button", name=re.compile(r"^" + re.escape(label))).click()
        page.wait_for_load_state("networkidle")
        return page

    def download(self, page: Page, link: Locator, name: str) -> Path:
        with page.expect_download() as pending:
            link.click()
        download = pending.value
        assert download.failure() is None, download.failure()
        target = self.files / name
        download.save_as(target)
        assert target.is_file() and target.stat().st_size, f"Empty download: {name}"
        return target

    def register(self) -> None:
        credentials = {
            "run_id": self.run_id,
            "base_url": self.base_url,
            "password": self.password,
            "accounts": {},
        }
        credential_path = self.output / "credentials.json"
        write_json(credential_path, credentials)
        credential_path.chmod(0o600)
        for role in "ABC":
            context = self.browser.new_context(
                base_url=self.base_url,
                viewport={"width": 1440, "height": 1000},
                accept_downloads=True,
                locale="zh-CN",
            )
            context.set_default_timeout(20_000)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on(
                "console",
                lambda message, who=role: self.console.append(
                    {
                        "account": who,
                        "type": message.type,
                        "text": message.text,
                        "location": message.location,
                        "intentional_logout_failure": self.injecting_logout_failure,
                    }
                ),
            )
            page.on(
                "pageerror",
                lambda error, who=role: self.page_errors.append(
                    {"account": who, "message": str(error)}
                ),
            )
            self.pages[role] = page
            username = f"m1_{self.run_id}_{role}"
            credentials["accounts"][role] = {"username": username}
            write_json(credential_path, credentials)
            page.goto("/", wait_until="networkidle")
            page.get_by_role("button", name="创建账号", exact=True).click()
            page.get_by_label("用户名或邮箱", exact=True).fill(username)
            page.get_by_label("密码", exact=True).fill(self.password)
            account = self.ui_post(
                page,
                "/api/auth/register",
                page.get_by_role("button", name="注册并登录", exact=True).click,
            )
            self.accounts[role] = account
            credentials["accounts"][role] = account
            write_json(credential_path, credentials)
            expect(page.get_by_role("button", name="退出登录", exact=True)).to_be_visible()
        self.checked("A/B/C use separate browser contexts and UI registration")

    def create_project_and_invite(self) -> None:
        page = self.pages["A"]
        name = f"M1语言景观验收-{self.run_id}"
        page.get_by_label("新建项目", exact=True).fill(name)
        self.project = self.ui_post(
            page, "/api/projects", page.get_by_role("button", name="创建项目", exact=True).click
        )
        self.path = f"/api/projects/{self.project['id']}"
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile("^成员与邀请")).click()
        for role in "BC":
            self.ui_post(
                page,
                self.path + "/invitations",
                page.get_by_role("button", name="生成一次性邀请", exact=True).click,
            )
            invite = page.get_by_label("邀请链接（单次使用）", exact=True).input_value()
            member = self.pages[role]
            member.goto(invite, wait_until="networkidle")
            self.ui_post(
                member,
                "/api/invitations/accept",
                member.get_by_role("button", name="接受邀请并加入", exact=True).click,
            )
            expect(member.get_by_role("heading", name=name, exact=True)).to_be_visible()
        page.get_by_role("button", name="刷新", exact=True).click()
        page.wait_for_load_state("networkidle")
        members = self.api("A", self.path + "/members")
        assert {member["user_id"] for member in members} == {
            account["id"] for account in self.accounts.values()
        }
        self.checked("A creates project; B/C accept their own one-use invitations through UI")

    def publish_plan(self) -> None:
        page = self.tab("A", "方案与分工")
        page.get_by_role("button", name="创建方案", exact=True).click()
        page.get_by_label(re.compile("^研究问题 / 作业目标")).fill(
            "城市语言景观中的语言选择如何反映社区空间与身份？"
        )
        page.get_by_label(re.compile("^最终交付物")).fill("研究报告与汇报 PPT")
        deadline = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%dT18:00")
        page.get_by_label("总截止时间（本地时间）", exact=True).fill(deadline)
        page.get_by_label("整体论证主线", exact=True).fill(
            "统一概念与采样口径，分析语言分布，再整合为有证据支持的社区解释。"
        )
        for index, term in enumerate(["语言景观", "分析单位", "社区身份"], start=1):
            page.get_by_role("button", name="添加术语", exact=True).click()
            group = page.get_by_role("group", name=f"术语 {index}", exact=True)
            group.get_by_label(re.compile("^术语")).fill(term)
            group.get_by_label(re.compile("^定义")).fill(f"本项目的{term}工作定义。")
            group.get_by_label("出处", exact=True).fill("团队研究方案中的工作定义")
        for index, variable in enumerate(["标牌语言", "采样街区"], start=1):
            page.get_by_role("button", name="添加数据口径", exact=True).click()
            group = page.get_by_role("group", name=f"数据口径 {index}", exact=True)
            group.get_by_label(re.compile("^变量")).fill(variable)
            group.get_by_label("来源", exact=True).fill("独立验收模拟材料")
            group.get_by_label("处理方式", exact=True).fill("逐条编码并保留原始编号。")
        for _ in range(3):
            page.get_by_role("button", name="添加分工模块", exact=True).click()
        owners = ["B", "C", "A"]
        for index, module in enumerate(page.locator(".module-editor").all()):
            module.get_by_label(re.compile("^模块名称")).fill(self.module_names[index])
            module.get_by_label(re.compile("^模块负责人")).select_option(
                self.accounts[owners[index]]["id"]
            )
            module.get_by_label(re.compile("^模块截止时间")).fill(deadline)
            module.get_by_label(re.compile("^交付形式")).select_option("md")
            module.get_by_label(re.compile("^模块目标")).fill(
                f"完成{self.module_names[index]}的证据整理。"
            )
            module.get_by_label("核心论点 / 预期结论", exact=True).fill("论点需与证据一一对应。")
            module.get_by_label("内容边界：写什么", exact=True).fill("本模块内的概念、数据及解释。")
            module.get_by_label("内容边界：不写什么", exact=True).fill("不重复其他模块的完整正文。")
            module.get_by_label("要交给下游什么", exact=True).fill("结论摘要、材料编号与已知问题。")
        for index in (1, 2):
            module = page.locator(".module-editor").nth(index)
            module.get_by_role("button", name="添加上游依赖", exact=True).click()
            module.get_by_label(re.compile("^上游模块")).select_option(
                label=self.module_names[index - 1]
            )
            module.get_by_label(re.compile("^需要上游提供的内容")).fill("提供统一口径和结论摘要。")
        page.get_by_label(re.compile("^变更说明")).fill(
            "首版：明确目标、3个术语、2项口径及3模块依赖。"
        )
        self.plan = self.ui_post(
            page,
            self.path + "/plan/versions",
            page.get_by_role("button", name="发布方案新版本", exact=True).click,
        )
        assert len(self.plan["content"]["terms"]) == 3
        assert len(self.plan["content"]["data_definitions"]) == 2
        modules = self.plan["content"]["modules"]
        assert len(modules) == 3
        assert modules[1]["upstream"][0]["module_id"] == modules[0]["id"]
        assert modules[2]["upstream"][0]["module_id"] == modules[1]["id"]
        self.module_b = modules[0]["id"]
        expect(page.locator(".plan-editor")).to_have_count(0)
        page.screenshot(path=str(self.output / "plan-desktop.png"), full_page=True)
        graph = page.get_by_role("region", name="模块依赖关系", exact=True)
        graph.screenshot(path=str(self.output / "plan-dependencies.png"))
        self.checked(
            "UI publishes structured plan: 3 terms, 2 data definitions, 3 owned DAG modules"
        )

    def publish_spec_and_templates(self) -> None:
        page = self.tab("A", "规范与模板")
        for name in ["GB/T 7714-2015", "潜学成智-上外PPT模板", "常用中英文字体与字号"]:
            page.get_by_role("checkbox", name=name).check()
        page.get_by_role("button", name="将所选范例加入草稿", exact=True).click()
        # Locate by actual input value without coupling to Vue-generated IDs.
        for item in page.locator(".item-editor").all():
            if item.get_by_label("编号", exact=True).input_value() == "FMT-FONT-01":
                item.get_by_label("要求", exact=True).fill(self.expected_font)
                break
        else:
            raise AssertionError("Font preset did not create FMT-FONT-01")
        page.get_by_label("本次变更说明", exact=True).fill(
            "选用 GB/T、上外模板与字体，调整中文正文字号。"
        )
        self.spec = self.ui_post(
            page,
            self.path + "/spec/versions",
            page.get_by_role("button", name="发布规范新版本", exact=True).click,
        )
        assert len(self.spec["items"]) == 4
        assert any(item["requirement"] == self.expected_font for item in self.spec["items"])
        template_rule = next(item for item in self.spec["items"] if item["category"] == "template")
        assert template_rule["template_resource_id"]
        downloaded = self.download(
            page,
            page.get_by_role("link", name="潜学成智-上外PPT模板.pptx", exact=True),
            "builtin-sisu.pptx",
        )
        assert is_zipfile(downloaded), "Builtin PPT must be an Office ZIP file"
        with ZipFile(downloaded) as archive:
            assert "ppt/presentation.xml" in archive.namelist()
            assert archive.testzip() is None
        resources = self.api("A", self.path + "/resources")
        template = next(
            item for item in resources if item["id"] == template_rule["template_resource_id"]
        )
        assert hashlib.sha256(downloaded.read_bytes()).hexdigest() == template["sha256"]
        own_template = self.files / "team-handoff-template.md"
        own_template.write_text(
            "# 交接模板\n结论摘要：\n数据来源：\n下游说明：\n", encoding="utf-8"
        )
        page.get_by_label("上传自有模板", exact=True).set_input_files(own_template)
        self.ui_post(
            page,
            self.path + "/resources",
            page.get_by_role("button", name="上传模板", exact=True).click,
        )
        saved = self.download(
            page,
            page.get_by_role("link", name=own_template.name, exact=True),
            "project-template-downloaded.md",
        )
        assert saved.read_bytes() == own_template.read_bytes()
        page.screenshot(path=str(self.output / "spec-desktop.png"), full_page=True)
        self.checked(
            "UI presets edited/published; builtin PPT ZIP/hash and own template download verified"
        )

    def progress_and_request(self) -> None:
        page = self.tab("B", "任务与交付", reload=True)
        page.get_by_label("当前任务", exact=True).select_option(self.module_b)
        page.get_by_label("当前阶段", exact=True).select_option("writing")
        page.get_by_label("一句摘要", exact=True).fill("已整理概念与文献，等待下游核对编码样例。")
        report = self.ui_post(
            page,
            self.path + f"/modules/{self.module_b}/progress",
            page.get_by_role("button", name="记录进度", exact=True).click,
        )
        assert report["source"] == "human" and report["user_id"] == self.accounts["B"]["id"]
        page = self.tab("B", "协作需求")
        page.locator("details.create-request > summary").click()
        title = f"核对编码样例-{self.run_id}"
        page.get_by_label("需求标题", exact=True).fill(title)
        page.get_by_label("需求内容", exact=True).fill("请提供两条编码样例和对应材料编号。")
        page.get_by_label("需求对象类型", exact=True).select_option("person")
        page.get_by_label("指定成员", exact=True).select_option(self.accounts["C"]["id"])
        self.request = self.ui_post(
            page,
            self.path + "/requests",
            page.get_by_role("button", name="发起需求", exact=True).click,
        )
        request_path = self.path + f"/requests/{self.request['id']}"
        page = self.tab("C", "协作需求", reload=True)
        page.get_by_role("navigation", name="需求列表", exact=True).get_by_role(
            "button", name=re.compile(re.escape(title))
        ).click()
        self.ui_post(
            page, request_path + "/accept", page.get_by_role("button", name="接手需求").click
        )
        expect(page.get_by_role("button", name="确认解决", exact=True)).to_have_count(0)
        page.get_by_label("回应内容", exact=True).fill("已提供两条样例及材料编号 S01、S02。")
        self.ui_post(
            page, request_path + "/respond", page.get_by_role("button", name="发送回应").click
        )
        page = self.tab("B", "协作需求", reload=True)
        page.get_by_label("补充需求", exact=True).fill("请再补充 S02 的编码依据。")
        self.ui_post(
            page, request_path + "/supplement", page.get_by_role("button", name="添加补充").click
        )
        page = self.tab("C", "协作需求", reload=True)
        page.get_by_label("回应内容", exact=True).fill("S02 依据首版方案的分析单位定义，核对完成。")
        self.ui_post(
            page, request_path + "/respond", page.get_by_role("button", name="发送回应").click
        )
        expect(page.get_by_role("button", name="确认解决", exact=True)).to_have_count(0)
        page = self.tab("B", "协作需求", reload=True)
        self.request = self.ui_post(
            page, request_path + "/resolve", page.get_by_role("button", name="确认解决").click
        )
        assert self.request["status"] == "resolved"
        assert [message["kind"] for message in self.request["messages"]] == [
            "response",
            "supplement",
            "response",
        ]
        assert [message["author_id"] for message in self.request["messages"]] == [
            self.accounts[role]["id"] for role in "CBC"
        ]
        expect(page.get_by_label("补充需求", exact=True)).to_have_count(0)
        self.checked(
            "B human progress; C accepts/responds; original thread supplement; "
            "creator-only resolution"
        )

    def submit_deliveries(self) -> None:
        page = self.tab("B", "任务与交付")
        page.get_by_label("当前任务", exact=True).select_option(self.module_b)
        for version in (1, 2):
            source = self.files / f"literature-v{version}.md"
            source.write_text(
                f"# 文献与概念\n版本 {version}\n证据与术语定义已核对。\n", encoding="utf-8"
            )
            page.get_by_label("交付文件", exact=True).set_input_files(source)
            page.get_by_label("AI 参与声明", exact=True).select_option(
                "assisted" if version == 1 else "none"
            )
            page.get_by_label("AI 参与说明（选填）", exact=True).fill(
                "AI 协助整理结构，提交人逐条核对。" if version == 1 else "本次修订由本人完成。"
            )
            page.get_by_label("交接说明（选填）", exact=True).fill(
                "结论、来源与材料编号齐全；已知未解决问题：无。"
            )
            delivery = self.ui_post(
                page,
                self.path + f"/modules/{self.module_b}/deliveries",
                page.get_by_role("button", name="提交正式交付", exact=True).click,
            )
            assert delivery["version"] == version
            assert delivery["plan_version_id"] == self.plan["id"]
            assert delivery["spec_version_id"] == self.spec["id"]
            assert delivery["submitted_by"] == self.accounts["B"]["id"]
            assert delivery["status"] == "submitted"
            assert delivery["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            self.deliveries.append(delivery)
        old = self.download(
            page,
            page.get_by_role("link", name="literature-v1.md", exact=True),
            "old-v1-downloaded.md",
        )
        assert old.read_bytes() == (self.files / "literature-v1.md").read_bytes()
        progress = self.api("B", self.path + "/progress")
        assert (
            next(item for item in progress if item["module_id"] == self.module_b)["stage"]
            == "submitted"
        )
        page = self.tab("C", "任务与交付")
        material = self.files / "shared-notes.md"
        material.write_text("# 共享材料\n不属于正式交付版本。\n", encoding="utf-8")
        page.get_by_label("上传共享材料", exact=True).set_input_files(material)
        self.ui_post(
            page, self.path + "/materials", page.get_by_role("button", name="上传材料").click
        )
        shared = self.download(
            page, page.get_by_role("link", name=material.name, exact=True), "shared-downloaded.md"
        )
        assert shared.read_bytes() == material.read_bytes()
        self.checked(
            "Formal v1/v2 retain AI declarations and bound versions; "
            "old bytes and shared material verified"
        )

    def history_permissions_and_notifications(self) -> None:
        page = self.tab("A", "规范与模板")
        new_requirement = self.expected_font.replace("14磅", "16磅")
        for item in page.locator(".item-editor").all():
            if item.get_by_label("编号", exact=True).input_value() == "FMT-FONT-01":
                item.get_by_label("要求", exact=True).fill(new_requirement)
        page.get_by_label("本次变更说明", exact=True).fill("第二版：调整字号，保留首版快照。")
        second = self.ui_post(
            page,
            self.path + "/spec/versions",
            page.get_by_role("button", name="发布规范新版本", exact=True).click,
        )
        assert second["version"] == 2
        page.get_by_label("查看规范版本", exact=True).select_option(self.spec["id"])
        published = page.get_by_role("region", name="已发布规范", exact=True)
        expect(published.get_by_text(self.expected_font, exact=True)).to_be_visible()
        expect(published.locator("input, textarea")).to_have_count(0)
        font_draft = next(
            item
            for item in page.locator(".item-editor").all()
            if item.get_by_label("编号", exact=True).input_value() == "FMT-FONT-01"
        )
        expect(font_draft.get_by_label("要求", exact=True)).to_have_value(new_requirement)
        assert (
            self.api("A", self.path + "/spec/versions/" + self.spec["id"])["items"]
            == self.spec["items"]
        )
        for role in "BC":
            member = self.tab(role, "规范与模板", reload=True)
            expect(member.get_by_role("button", name="发布规范新版本", exact=True)).to_have_count(0)
            expect(member.get_by_label("上传自有模板", exact=True)).to_have_count(0)
            self.tab(role, "方案与分工")
            expect(member.get_by_role("button", name="基于最新版本编辑", exact=True)).to_have_count(
                0
            )
            expect(member.locator(".plan-editor")).to_have_count(0)
            self.tab(role, "时间线")
            expect(member.get_by_role("link", name="导出 JSON", exact=True)).to_have_count(0)
        for role in "ABC":
            page = self.pages[role]
            page.get_by_role("button", name=re.compile("^站内通知")).click()
            notifications = self.api(role, f"/api/notifications?project_id={self.project['id']}")
            unread = next(item for item in notifications if item["read_at"] is None)
            panel = page.get_by_role("region", name="站内通知", exact=True)
            self.ui_post(
                page,
                f"/api/notifications/{unread['id']}/read",
                panel.get_by_role("button", name="标为已读", exact=True).first.click,
            )
            updated = self.api(role, f"/api/notifications?project_id={self.project['id']}")
            assert next(item for item in updated if item["id"] == unread["id"])["read_at"]
            page.get_by_role("button", name="收起通知", exact=True).click()
            page.reload(wait_until="networkidle")
            account = self.api(role, "/api/auth/me")
            assert account["id"] == self.accounts[role]["id"]
        self.checked(
            "Frozen history/draft separation; member read-only UI; "
            "notifications read; sessions survive refresh"
        )

    def timelines_exports_and_screenshots(self) -> None:
        # M3 adds background audit writes. Compare exports only after this run's
        # jobs settle; queued/retrying work must not race the JSON/CSV snapshot.
        deadline = monotonic() + 90
        submitted_ids = {item["id"] for item in self.deliveries}
        while True:
            deliveries = self.api("B", self.path + "/deliveries")
            progress = [
                item.get("review_progress")
                for item in deliveries
                if item["id"] in submitted_ids
            ]
            assert len(progress) == len(submitted_ids)
            if all(
                item is None or item["status"] in {"succeeded", "failed", "paused"}
                for item in progress
            ):
                break
            assert monotonic() < deadline, "Background review jobs did not settle before export"
            self.pages["B"].wait_for_timeout(500)
        page = self.tab("A", "时间线")
        events = self.api("A", self.path + "/events?limit=500")
        expected_actors = {
            "project_created": "A",
            "invitation_created": "A",
            "plan_published": "A",
            "module_updated": "A",
            "spec_published": "A",
            "template_uploaded": "A",
            "progress_reported": "B",
            "delivery_submitted": "B",
            "material_uploaded": "C",
            "request_created": "B",
            "request_accepted": "C",
            "request_responded": "C",
            "request_supplemented": "B",
            "request_resolved": "B",
        }
        counts = Counter(event["event_type"] for event in events)
        assert set(expected_actors) <= counts.keys()
        assert counts["request_responded"] == 2 and counts["delivery_submitted"] == 2
        assert counts["member_joined"] == 2 and counts["notification_read"] == 3
        names = {account["id"]: account["username"] for account in self.accounts.values()}
        for event in events:
            assert event["project_id"] == self.project["id"]
            assert event["represented_user_id"] is None
            if event["actor_type"] == "human":
                assert event["actor_id"] in names
                actor_label = names[event["actor_id"]]
            else:
                assert event["actor_type"] == "agent"
                assert event["actor_id"] == "platform-review-worker"
                assert event["details"]["execution_origin"] == "platform_worker"
                assert event["object_id"] in submitted_ids
                actor_label = "平台审核后台"
            if event["event_type"] in expected_actors:
                assert event["actor_type"] == "human"
                assert (
                    event["actor_id"] == self.accounts[expected_actors[event["event_type"]]]["id"]
                )
            row = page.locator(".timeline-list > li").filter(
                has=page.get_by_text(f"记录详情 · #{event['id']}", exact=True)
            )
            expect(row).to_have_count(1)
            expect(row.locator(".timeline-body > .toolbar > strong")).to_have_text(
                actor_label
            )
        page.screenshot(path=str(self.output / "timeline-project.png"), full_page=True)
        for format_name in ("JSON", "CSV"):
            self.download(
                page,
                page.get_by_role("link", name=f"导出 {format_name}", exact=True),
                f"events.{format_name.lower()}",
            )
        exported = json.loads((self.files / "events.json").read_text(encoding="utf-8"))
        with (self.files / "events.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        nullable = {
            "project_id",
            "represented_user_id",
            "plan_version_id",
            "spec_version_id",
            "task_id",
        }
        normalized = []
        for row in rows:
            normalized.append(
                {
                    key: int(value)
                    if key == "id"
                    else json.loads(value)
                    if key == "details"
                    else None
                    if key in nullable and value == ""
                    else value
                    for key, value in row.items()
                }
            )
        assert normalized == exported, "CSV and JSON exports differ after normalizing types/nulls"
        assert [event["id"] for event in exported] == [event["id"] for event in events]
        page.get_by_label("按任务查看", exact=True).select_option(self.module_b)
        page.wait_for_load_state("networkidle")
        task_events = self.api("A", self.path + f"/events?task_id={self.module_b}&limit=500")
        assert task_events and all(event["task_id"] == self.module_b for event in task_events)
        assert {"progress_reported", "delivery_submitted"} <= {
            event["event_type"] for event in task_events
        }
        expect(page.locator(".timeline-list > li")).to_have_count(len(task_events))
        page.screenshot(path=str(self.output / "timeline-task.png"), full_page=True)
        page = self.tab("A", "项目看板")
        expect(page.locator(".module-card")).to_have_count(3)
        card = page.locator(".module-card").filter(
            has=page.get_by_role("heading", name=self.module_names[0])
        )
        expect(card.get_by_text("已提交", exact=True)).to_be_visible()
        page.screenshot(path=str(self.output / "board-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(self.output / "board-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            "Mobile horizontal overflow"
        )
        page.set_viewport_size({"width": 1440, "height": 1000})
        write_json(self.output / "events-verified.json", exported)
        self.checked(
            "Project/task timelines show correct actors; "
            "complete UI JSON/CSV exports match field by field"
        )
        self.checked("Desktop board/plan/DAG/spec/timelines and mobile board screenshots saved")

    def logout_failure_and_recovery(self) -> None:
        page = self.pages["A"]
        self.injecting_logout_failure = True
        page.route(
            "**/api/auth/logout",
            lambda route: route.fulfill(status=500, json={"detail": "验收注入的退出失败"}),
        )
        try:
            with page.expect_response(
                lambda response: (
                    urlsplit(response.url).path == "/api/auth/logout"
                    and response.request.method == "POST"
                )
            ) as pending:
                page.get_by_role("button", name="退出登录", exact=True).click()
            assert pending.value.status == 500
            page.wait_for_load_state("networkidle")
            expect(page.get_by_role("alert").filter(has_text="操作未完成")).to_be_visible()
            expect(page.get_by_role("button", name="退出登录", exact=True)).to_be_visible()
            assert self.api("A", "/api/auth/me")["id"] == self.accounts["A"]["id"]
            page.screenshot(path=str(self.output / "logout-failure.png"), full_page=True)
        finally:
            page.unroute("**/api/auth/logout")
            self.injecting_logout_failure = False
        self.ui_post(
            page, "/api/auth/logout", page.get_by_role("button", name="退出登录", exact=True).click
        )
        expect(page.get_by_role("heading", name="登录工作台", exact=True)).to_be_visible()
        page.get_by_label("用户名或邮箱", exact=True).fill(self.accounts["A"]["username"])
        page.get_by_label("密码", exact=True).fill(self.password)
        logged_in = self.ui_post(
            page, "/api/auth/login", page.get_by_role("button", name="登录", exact=True).click
        )
        assert logged_in["id"] == self.accounts["A"]["id"]
        expect(page.get_by_role("button", name="退出登录", exact=True)).to_be_visible()
        self.checked(
            "Injected logout 500 remains visible and logged in; normal logout/login recovers"
        )

    def finish(self, failure: str | None) -> None:
        for role, page in self.pages.items():
            if failure:
                try:
                    page.screenshot(path=str(self.output / f"failure-{role}.png"), full_page=True)
                    (self.output / f"failure-{role}.html").write_text(
                        page.content(), encoding="utf-8"
                    )
                except (OSError, PlaywrightError) as error:
                    print(f"Could not save failure page for {role}: {error}", file=sys.stderr)
            try:
                page.context.tracing.stop(path=str(self.output / f"trace-{role}.zip"))
            except PlaywrightError as error:
                print(f"Could not save trace for {role}: {error}", file=sys.stderr)
        write_json(self.output / "console.json", self.console)
        write_json(self.output / "pageerrors.json", self.page_errors)
        # A fresh visitor receives an expected 401 while the login form loads.
        unexpected_console = [
            message
            for message in self.console
            if message["type"] == "error"
            and not (
                "/api/auth/me" in message["location"].get("url", "") and "401" in message["text"]
            )
            and not (
                message.get("intentional_logout_failure")
                and "/api/auth/logout" in message["location"].get("url", "")
                and "500" in message["text"]
            )
        ]
        write_json(self.output / "unexpected-console-errors.json", unexpected_console)
        write_json(
            self.output / "report.json",
            {
                "run_id": self.run_id,
                "base_url": self.base_url,
                "passed": failure is None and not self.page_errors and not unexpected_console,
                "checks": self.checks,
                "failure": failure,
                "project_id": self.project.get("id"),
                "accounts": self.accounts,
                "page_errors": self.page_errors,
                "unexpected_console_errors": unexpected_console,
                "formal_delivery_ids": [item["id"] for item in self.deliveries],
            },
        )
        if failure is None:
            assert not self.page_errors, f"Page errors recorded: {self.page_errors}"
            assert not unexpected_console, f"Unexpected console errors: {unexpected_console}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--output-dir", type=Path, default=Path(".verification/M1"))
    parser.add_argument("--browser-executable", help="Optional local Chrome/Chromium executable")
    args = parser.parse_args()
    output = args.output_dir.resolve() / (
        "run-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    )
    output.mkdir(parents=True)
    output.chmod(0o700)
    print(f"Artifacts: {output}", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.browser_executable)
        acceptance = Acceptance(browser, args.base_url, output)
        failure = None
        try:
            acceptance.register()
            acceptance.create_project_and_invite()
            acceptance.publish_plan()
            acceptance.publish_spec_and_templates()
            acceptance.progress_and_request()
            acceptance.submit_deliveries()
            acceptance.history_permissions_and_notifications()
            acceptance.timelines_exports_and_screenshots()
            acceptance.logout_failure_and_recovery()
        except Exception:  # noqa: BLE001 - preserve diagnostics for any failed acceptance step
            failure = traceback.format_exc()
            print(failure, file=sys.stderr)
        finally:
            try:
                acceptance.finish(failure)
            except Exception:  # noqa: BLE001 - preserve a nonzero exit if diagnostics fail
                failure = failure or traceback.format_exc()
                print(failure, file=sys.stderr)
            browser.close()
        if failure:
            return 1
    print("PASS M1 acceptance completed; review screenshots and report.json.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
